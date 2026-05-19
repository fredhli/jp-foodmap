"""
Supplemental scraper that tops up '正餐' (main-meal) coverage for regions
where audit_main_meal_coverage.py flagged a shortfall.

Three hard rules — every card is checked against all three before being kept:
  1. Price-ceiling gate: drop anything where dinner_upper or lunch_upper
     reaches ¥20,000+ (no fine-dining).
  2. Price-floor gate: drop anything whose effective price (dinner_upper if
     present, otherwise lunch_upper) is ≤ ¥3,000 (too cheap to count as a
     proper sit-down meal).
  3. Genre gate: the bucket returned by categorize_genre() must belong to
     MEAL_GROUPS["正餐"]. Coffee shops, izakayas, sweets, omiyage stores —
     all skipped.

For each short region we resume from max(source_page)+1, paginate, apply
the two gates to every card, and stop when min(deficit, --hard-cap) cards
have been kept (or the list runs out). Detail pages are fetched, the
reservation_policy is optionally translated to zh-CN, and the new rows are
appended to tabelog.csv (dedupe by detail_url, keep-last).

Deficits are recomputed live from tabelog.csv + the totals cache at
data/cache/region_totals.json — refresh that cache via
audit_main_meal_coverage.py if the region list has changed.

Usage:
  uv run python src/tabelog/scrape/scrape_topup.py
  uv run python src/tabelog/scrape/scrape_topup.py tokyo nagano   # subset
  uv run python src/tabelog/scrape/scrape_topup.py --hard-cap 500
  uv run python src/tabelog/scrape/scrape_topup.py --dry-run
  uv run python src/tabelog/scrape/scrape_topup.py --no-translate
"""

import argparse
import asyncio
import csv
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright

from tabelog.paths import INTERMEDIATE_DIR, TABELOG_CSV
from tabelog.scrape.audit_main_meal_coverage import (
    TOTALS_CACHE,
    is_main_meal,
    load_region_stats,
)
from tabelog.scrape.scrape_all import (
    CHECKPOINT_EVERY,
    FINE_DINE_THRESHOLD_YEN,
    Session,
    _is_fine_dine,
    append_and_dedupe,
    fetch_detail,
    scrape_list_page,
    translate_reservation_policy,
    write_intermediate,
)

MAIN_MEAL_RATIO = 0.008
HARD_CAP_DEFAULT = 300
CHEAP_EATS_THRESHOLD_YEN = 3000


def _int_or_zero(v) -> int:
    try:
        return int(v) if v not in (None, "", "None") else 0
    except (TypeError, ValueError):
        return 0


def _is_cheap_eats(row: dict) -> bool:
    """Effective price (dinner first, lunch fallback) <= ¥3,000. Mirrors
    map.price_bucket()'s convention — dinner is the primary signal of how
    expensive a sit-down meal here actually is."""
    n = row.get("dinner_upper")
    if n is None or n == "":
        n = row.get("lunch_upper")
    if n is None or n == "":
        return False
    try:
        return int(n) <= CHEAP_EATS_THRESHOLD_YEN
    except (TypeError, ValueError):
        return False


def scan_existing(csv_path: Path) -> dict[str, dict]:
    """{region: {"last_page": int, "urls": set[str]}}"""
    out: dict[str, dict] = {}
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            region = (r.get("region") or "").strip().lower()
            if not region:
                continue
            d = out.setdefault(region, {"last_page": 0, "urls": set()})
            d["last_page"] = max(d["last_page"], _int_or_zero(r.get("source_page")))
            url = (r.get("detail_url") or "").strip()
            if url:
                d["urls"].add(url)
    return out


def load_totals_cache() -> dict[str, int]:
    if not TOTALS_CACHE.exists():
        sys.exit(
            f"missing {TOTALS_CACHE}; run audit_main_meal_coverage.py first"
        )
    return json.loads(TOTALS_CACHE.read_text(encoding="utf-8"))


async def collect_topup(
    session: Session,
    region: str,
    start_page: int,
    target: int,
    seen_urls: set[str],
) -> list[dict]:
    """Paginate from start_page. Keep a card iff:
       (a) detail_url not already in seen_urls,
       (b) neither price >= ¥20,000 (drop fine-dining),
       (c) effective price > ¥3,000 (drop cheap eats),
       (d) its genre bucket is in MEAL_GROUPS["正餐"].
    Stop at `target` kept rows, or when the list is exhausted."""
    kept: list[dict] = []
    page_num = start_page
    while len(kept) < target:
        try:
            rows, _total = await scrape_list_page(session, region, page_num)
        except Exception as e:
            print(f"[{region} p{page_num}] gave up: {e}")
            break
        if not rows:
            print(f"[{region} p{page_num}] no cards; list exhausted")
            break

        p_kept = p_dup = p_fd = p_cheap = p_non_main = 0
        for row in rows:
            if len(kept) >= target:
                break
            url = (row.get("detail_url") or "").strip()
            if url and url in seen_urls:
                p_dup += 1
                continue
            if _is_fine_dine(row):
                p_fd += 1
                continue
            if _is_cheap_eats(row):
                p_cheap += 1
                continue
            if not is_main_meal(row.get("genre") or ""):
                p_non_main += 1
                continue
            kept.append(row)
            if url:
                seen_urls.add(url)
            p_kept += 1

        print(
            f"[{region} p{page_num}] {len(rows)} cards: kept {p_kept}, "
            f"skipped fd={p_fd} cheap={p_cheap} non-main={p_non_main} dup={p_dup}; "
            f"running {len(kept)}/{target}"
        )
        if len(kept) >= target:
            print(f"[{region} p{page_num}] hit target {target}; stopping")
            break
        page_num += 1

    print(f"[{region}] topup done: kept {len(kept)} main-meal rows "
          f"(all >¥{CHEAP_EATS_THRESHOLD_YEN:,} and <¥{FINE_DINE_THRESHOLD_YEN:,})")
    return kept


def plan_topup(
    stats: dict[str, dict[str, int]],
    totals: dict[str, int],
    existing: dict[str, dict],
    only: list[str] | None,
    hard_cap: int,
    ratio: float,
) -> list[dict]:
    plans: list[dict] = []
    candidates = sorted(only) if only else sorted(stats)
    for region in candidates:
        s = stats.get(region)
        if s is None:
            print(f"[{region}] no rows in tabelog.csv; skipping")
            continue
        total = totals.get(region)
        if total is None:
            print(f"[{region}] no total in cache; skipping")
            continue
        threshold = math.ceil(total * ratio)
        deficit = threshold - s["main_meal"]
        if deficit <= 0:
            print(f"[{region}] already at threshold "
                  f"(main={s['main_meal']} >= {threshold}); skipping")
            continue
        target = min(deficit, hard_cap)
        last_page = existing.get(region, {}).get("last_page", 0)
        plans.append({
            "region": region,
            "total": total,
            "deficit": deficit,
            "target": target,
            "start_page": last_page + 1,
            "seen_urls": existing.get(region, {}).get("urls", set()),
        })
    return plans


def print_plan(plans: list[dict], hard_cap: int) -> None:
    if not plans:
        return
    header = (f"\n{'region':12s} {'total':>7s} {'deficit':>8s} "
              f"{'target':>7s} {'start_p':>8s}")
    print(header)
    print("-" * len(header))
    for p in plans:
        print(f"{p['region']:12s} {p['total']:>7d} {p['deficit']:>8d} "
              f"{p['target']:>7d} {p['start_page']:>8d}")
    clamped = [p["region"] for p in plans if p["target"] < p["deficit"]]
    print(f"\n{len(plans)} region(s) queued; "
          f"{len(clamped)} clamped to hard_cap={hard_cap}"
          + (f" ({', '.join(clamped)})" if clamped else ""))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "regions", nargs="*", type=str,
        help="optional: only top up these region slugs (default: all flagged)",
    )
    ap.add_argument(
        "--hard-cap", type=int, default=HARD_CAP_DEFAULT,
        help=f"max NEW main-meal rows added per region (default: {HARD_CAP_DEFAULT})",
    )
    ap.add_argument(
        "--ratio", type=float, default=MAIN_MEAL_RATIO,
        help=f"main-meal share target as a fraction (default: "
             f"{MAIN_MEAL_RATIO} = {MAIN_MEAL_RATIO * 100:.1f}%%)",
    )
    ap.add_argument(
        "--translate", action=argparse.BooleanOptionalAction, default=True,
        help="translate reservation_policy -> reservation_policy_chinese (default: on)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="print the per-region plan and exit without scraping",
    )
    return ap.parse_args(argv)


async def amain(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if not TABELOG_CSV.exists():
        sys.exit(f"missing {TABELOG_CSV}")

    print(f"Reading {TABELOG_CSV.name} ...")
    stats = load_region_stats(TABELOG_CSV)
    existing = scan_existing(TABELOG_CSV)
    totals = load_totals_cache()
    only = [r.strip().lower() for r in args.regions] if args.regions else None

    plans = plan_topup(stats, totals, existing, only, args.hard_cap, args.ratio)
    if not plans:
        print("\nNothing to top up.")
        return
    print_plan(plans, args.hard_cap)
    if args.dry_run:
        return

    INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)
    intermediate = INTERMEDIATE_DIR / "tabelog_topup_intermediate.csv"
    all_kept: list[dict] = []

    async with async_playwright() as p:
        session = Session(p)
        await session.connect()

        for plan in plans:
            print(f"\n=== {plan['region']} ===")
            print(f"  total={plan['total']}, deficit={plan['deficit']}, "
                  f"target={plan['target']} (hard_cap={args.hard_cap})")
            print(f"  resume from page {plan['start_page']}")
            kept = await collect_topup(
                session,
                plan["region"],
                plan["start_page"],
                plan["target"],
                plan["seen_urls"],
            )
            all_kept.extend(kept)
            write_intermediate(all_kept, intermediate)

        if not all_kept:
            print("\nNo new rows collected.")
            return

        print(f"\nCollected {len(all_kept)} new rows. Visiting detail pages ...")
        for n, row in enumerate(all_kept, 1):
            url = row.get("detail_url")
            if not url:
                continue
            try:
                d = await fetch_detail(session, url)
            except Exception as e:
                print(f"  [{n}/{len(all_kept)}] {row.get('name')!r}: gave up — {e}")
                continue
            row["seat_count"] = d.get("seat_count") or ""
            row["address"] = d.get("address") or ""
            row["reservation_policy"] = d.get("reservation_policy") or ""
            row["tabelog_bookable"] = "True" if d.get("tabelog_bookable") else "False"
            photos = d.get("photos") or []
            row["photo1_url"] = photos[0] if len(photos) > 0 else ""
            row["photo2_url"] = photos[1] if len(photos) > 1 else ""
            row["photo3_url"] = photos[2] if len(photos) > 2 else ""
            print(f"  [{n}/{len(all_kept)}] {row.get('name')!r}: "
                  f"seats={row['seat_count']!r}, bookable={row['tabelog_bookable']}, "
                  f"photos={len(photos)}")
            if n % CHECKPOINT_EVERY == 0:
                write_intermediate(all_kept, intermediate)
        write_intermediate(all_kept, intermediate)

    if args.translate:
        await translate_reservation_policy(all_kept, intermediate)

    append_and_dedupe(all_kept, TABELOG_CSV)


def main(argv: list[str] | None = None) -> None:
    asyncio.run(amain(argv))


if __name__ == "__main__":
    main()
