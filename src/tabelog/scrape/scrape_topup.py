"""
Legacy supplemental-only entry and reusable query helpers. The complete current
selection and region loop live in project main.py; ordinary main runs already
include both nationwide main-meal supplementation and Tokyo/Osaka filtered lists.

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

--tokyo-osaka-append starts from the filtered Tokyo and Osaka list URLs
(page 10 and page 8 by default), respectively. It scans every card on each
page and keeps scores of 3.50 or higher until a lower score appears or page
60 is processed. This mode ignores region deficits and the normal hard cap.
It keeps the same price and main-meal gates as the older top-up modes.

Legacy --tokyo mode (Tokyo can't be topped up the normal way: the plain list caps
at 60 pages = the top 1,200 by rating, and the original scrape already used
all 60, so max(source_page)+1 = page 61 returns nothing). Instead of the
plain list, --tokyo paginates Tabelog's own pre-filtered list —
`/tokyo/rstLst/RC/{page}/` with dinner budget ¥3,000–¥20,000 and the RC
("restaurant") category, which server-side drops ramen shops, cafés and the
like. That list has its own, deeper pagination, so we start from --start-page
(default 10, where the operator had already scrolled to) and walk onward.
The same three keep-gates still apply as a safety net, and dedupe against the
existing Tokyo rows means overlap with the original scrape is just skipped.
The `/cn/` UI-language prefix is deliberately dropped: the genre gate matches
Japanese genre tokens, so the list must come back in Japanese.

Usage:
  uv run python src/tabelog/scrape/scrape_topup.py
  uv run python src/tabelog/scrape/scrape_topup.py tokyo nagano   # subset
  uv run python src/tabelog/scrape/scrape_topup.py --hard-cap 500
  uv run python src/tabelog/scrape/scrape_topup.py --dry-run
  uv run python src/tabelog/scrape/scrape_topup.py --no-translate
  uv run python src/tabelog/scrape/scrape_topup.py --tokyo-osaka-append
  uv run python src/tabelog/scrape/scrape_topup.py --tokyo-osaka-append --dry-run
  uv run python src/tabelog/scrape/scrape_topup.py --tokyo               # legacy Tokyo deficit top-up
  uv run python src/tabelog/scrape/scrape_topup.py --tokyo --hard-cap 900   # fill the whole deficit in one run
"""

import argparse
import asyncio
import csv
import datetime
import json
import math
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

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

# --tokyo: Tabelog's own pre-filtered list. {region}/{page} are filled by
# scrape_list_page; {svd} (a reservation-date form value) is filled at run
# time. RC = the "restaurant" category (ramen / cafés / sweets shops sit in
# other categories and never appear here); LstCos=3..LstCosT=10 with
# RdoCosTp=2 = dinner budget ¥3,000-¥20,000; SrtT=rt = sort by rating.
# svd/svps/svt ride along inertly because vac_net=0 turns the
# seat-vacancy filter off — they are kept only so the URL matches the page
# the operator validated by hand.
TOKYO_LIST_TEMPLATE = (
    "https://tabelog.com/{region}/rstLst/RC/{page}/"
    "?LstCos=3&LstCosT=10&LstSmoking=0&RdoCosTp=2&SrtT=rt"
    "&svd={svd}&svps=2&svt=1900&vac_net=0"
)
TOKYO_DEFAULT_START_PAGE = 10

APPEND_SCORE_FLOOR = Decimal("3.50")
APPEND_PAGE_LIMIT = 60

TOKYO_APPEND_LIST_URL = TOKYO_LIST_TEMPLATE.format(
    region="tokyo", page=TOKYO_DEFAULT_START_PAGE, svd="{svd}"
)
OSAKA_APPEND_LIST_URL = (
    "https://tabelog.com/osaka/rstLst/RC/8/"
    "?Srt=D&SrtT=rt&LstSmoking=0"
    "&svd=%7B%E8%BF%90%E8%A1%8C%E6%97%A5%E6%9C%9FYYYYMMDD%7D"
    "&svt=1900&svps=2&vac_net=0&LstCos=3&LstCosT=10&RdoCosTp=2"
)


def resolve_list_url_date(list_url: str, today: datetime.date | None = None) -> str:
    """Replace an svd date placeholder while leaving all other query bytes alone."""
    parsed = urlsplit(list_url)
    date = (today or datetime.date.today()).strftime("%Y%m%d")
    fields = parsed.query.split("&")
    for i, field in enumerate(fields):
        name, sep, value = field.partition("=")
        if name != "svd" or not sep:
            continue
        decoded = unquote(value)
        if decoded in ("{svd}", "{运行日期YYYYMMDD}"):
            fields[i] = "svd=" + date
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "&".join(fields),
                       parsed.fragment))


@dataclass
class AppendResult:
    rows: list[dict]
    stop_reason: str
    last_page: int
    unknown_ratings: int = 0


class AppendCollectionError(RuntimeError):
    def __init__(self, message: str, rows: list[dict], page: int):
        super().__init__(message)
        self.rows = rows
        self.page = page


def append_list_page_url(list_url: str, region: str, page: int) -> str:
    """Replace a list URL's final page path component, keeping its query intact."""
    parsed = urlsplit(list_url)
    if (parsed.scheme != "https" or parsed.netloc != "tabelog.com"
            or parsed.fragment or parsed.username or parsed.password):
        raise ValueError("list URL must be an HTTPS tabelog.com URL")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    sort_values = [value for key, value in query if key == "SrtT"]
    directions = [value for key, value in query if key == "Srt"]
    sort_modes = [value for key, value in query if key == "sort_mode"]
    if (sort_values != ["rt"] or any(value != "D" for value in directions)
            or any(value != "1" for value in sort_modes)):
        raise ValueError("append list URL must sort by rating descending (SrtT=rt)")
    parts = parsed.path.strip("/").split("/")
    if (len(parts) < 2 or parts[0] != region or parts[1] != "rstLst"
            or any(
                not part or "/" in unquote(part) or "\\" in unquote(part)
                or unquote(part) in (".", "..")
                for part in parts
            )):
        raise ValueError(f"list URL must be a /{region}/rstLst/ path")
    if parts[-1].isdecimal():
        parts[-1] = str(page)
    else:
        parts.append(str(page))
    if not 1 <= page <= APPEND_PAGE_LIMIT:
        raise ValueError(f"page must be 1..{APPEND_PAGE_LIMIT}")
    return urlunsplit(("https", "tabelog.com", "/" + "/".join(parts) + "/",
                       parsed.query, ""))


def append_start_page(list_url: str, region: str, override: int | None) -> int:
    """Use an explicit start page, or the page already present in the supplied URL."""
    append_list_page_url(list_url, region, 1)
    original_last = urlsplit(list_url).path.rstrip("/").split("/")[-1]
    page = override if override is not None else (int(original_last) if original_last.isdecimal() else 1)
    if not 1 <= page <= APPEND_PAGE_LIMIT:
        raise ValueError(f"start page must be 1..{APPEND_PAGE_LIMIT}")
    return page


def _append_detail_url(value: object, region: str) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value.strip())
    parts = parsed.path.strip("/").split("/")
    if (parsed.scheme != "https" or parsed.netloc != "tabelog.com"
            or len(parts) != 4 or parts[0] != region
            or not re.fullmatch(r"A\d{4}", parts[1])
            or not re.fullmatch(r"A\d{6}", parts[2])
            or not parts[3].isdecimal()
            or any(
                not part or "/" in unquote(part) or "\\" in unquote(part)
                or unquote(part) in (".", "..")
                for part in parts
            )):
        return None
    return urlunsplit(("https", "tabelog.com", parsed.path.rstrip("/") + "/", "", ""))


def _restaurant_id_from_url(value: object) -> str | None:
    """Read a Tabelog restaurant ID without depending on its area path."""
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value.strip())
    parts = parsed.path.strip("/").split("/")
    if (parsed.scheme not in ("http", "https") or parsed.netloc != "tabelog.com"
            or len(parts) < 4
            or not re.fullmatch(r"A\d{4}", parts[-3])
            or not re.fullmatch(r"A\d{6}", parts[-2])
            or not parts[-1].isdecimal()):
        return None
    return parts[-1].lstrip("0") or "0"


def _append_rating(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not re.fullmatch(r"(?:[1-4](?:\.\d{1,2})?|5(?:\.0{1,2})?)", text):
        return None
    try:
        rating = Decimal(text)
    except InvalidOperation:
        return None
    return rating if rating.is_finite() else None


def append_detail_reject_reason(detail: dict) -> str | None:
    status = detail.get("operating_status")
    if status in ("closed", "temporarily_closed"):
        return status
    rating = _append_rating(detail.get("rating"))
    if rating is not None and rating < APPEND_SCORE_FLOOR:
        return f"detail rating {rating} below 3.50"
    return None


async def collect_append(
    session: Session,
    region: str,
    start_page: int,
    list_url: str,
    seen_urls: set[str],
    apply_legacy_gates: bool = True,
    cheap_floor: int = CHEAP_EATS_THRESHOLD_YEN,
) -> AppendResult:
    """Collect all rated cards through the 3.50 boundary or page 60."""
    append_list_page_url(list_url, region, start_page)
    kept: list[dict] = []
    seen_ids = {
        restaurant_id for url in seen_urls
        if (restaurant_id := _restaurant_id_from_url(url)) is not None
    }
    signatures: set[tuple[str, ...]] = set()
    unknown_ratings = 0
    last_rating: Decimal | None = None
    for page in range(start_page, APPEND_PAGE_LIMIT + 1):
        page_url = append_list_page_url(list_url, region, page)
        try:
            rows, _ = await scrape_list_page(
                session, region, page,
                page_url.replace("{", "{{").replace("}", "}}"),
            )
        except Exception as exc:
            raise AppendCollectionError(
                f"[{region} p{page}] list request failed: {exc}", kept, page
            ) from exc
        if not rows:
            raise AppendCollectionError(
                f"[{region} p{page}] no cards parsed before score floor/page limit",
                kept, page,
            )
        signature = tuple(sorted(
            _restaurant_id_from_url(row.get("detail_url"))
            or str(row.get("detail_url") or "")
            for row in rows
        ))
        if signature in signatures:
            raise AppendCollectionError(
                f"[{region} p{page}] repeated list page", kept, page
            )
        signatures.add(signature)

        valid_cards: list[tuple[dict, str, Decimal]] = []
        p_invalid = p_unknown = 0
        for row in rows:
            url = _append_detail_url(row.get("detail_url"), region)
            if url is None:
                p_invalid += 1
                continue
            rating = _append_rating(row.get("rating"))
            if rating is None:
                p_unknown += 1
                continue
            if last_rating is not None and rating > last_rating:
                raise AppendCollectionError(
                    f"[{region} p{page}] ratings out of descending order: "
                    f"{rating} after {last_rating}", kept, page
                )
            last_rating = rating
            valid_cards.append((row, url, rating))
        if not valid_cards:
            raise AppendCollectionError(
                f"[{region} p{page}] no cards with valid restaurant URL and rating",
                kept, page,
            )

        below_floor = False
        p_kept = p_dup = p_below = p_filtered = 0
        for row, url, rating in valid_cards:
            if rating < APPEND_SCORE_FLOOR:
                below_floor = True
                p_below += 1
                continue
            restaurant_id = _restaurant_id_from_url(url)
            if restaurant_id in seen_ids:
                p_dup += 1
                continue
            if apply_legacy_gates and (
                _is_fine_dine(row)
                or _is_cheap_eats(row, cheap_floor)
                or not is_main_meal(row.get("genre") or "")
            ):
                p_filtered += 1
                continue
            row["detail_url"] = url
            seen_ids.add(restaurant_id)
            seen_urls.add(url)
            kept.append(row)
            p_kept += 1
        unknown_ratings += p_unknown
        print(f"[{region} p{page}] {len(rows)} cards: kept={p_kept} "
              f"duplicate={p_dup} invalid_url={p_invalid} "
              f"unknown_rating={p_unknown} below_3.50={p_below} "
              f"legacy_filtered={p_filtered}")
        if below_floor:
            return AppendResult(kept, "score_floor", page, unknown_ratings)
    return AppendResult(kept, "page_limit", APPEND_PAGE_LIMIT, unknown_ratings)


def _int_or_zero(v) -> int:
    try:
        return int(v) if v not in (None, "", "None") else 0
    except (TypeError, ValueError):
        return 0


def _is_cheap_eats(row: dict, floor: int = CHEAP_EATS_THRESHOLD_YEN) -> bool:
    """Effective price (dinner first, lunch fallback) <= floor. Mirrors
    map.price_bucket()'s convention — dinner is the primary signal of how
    expensive a sit-down meal here actually is."""
    n = row.get("dinner_upper")
    if n is None or n == "":
        n = row.get("lunch_upper")
    if n is None or n == "":
        return False
    try:
        return int(n) <= floor
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
    url_template: str | None = None,
    cheap_floor: int = CHEAP_EATS_THRESHOLD_YEN,
) -> list[dict]:
    """Paginate from start_page. Keep a card iff:
       (a) detail_url not already in seen_urls,
       (b) neither price >= ¥20,000 (drop fine-dining),
       (c) effective price > cheap_floor (drop cheap eats),
       (d) its genre bucket is in MEAL_GROUPS["正餐"].
    Stop at `target` kept rows, or when the list is exhausted.

    url_template, when given, replaces the plain rating-sorted list with a
    server-side-filtered one (the --tokyo path). The four gates still run:
    Tabelog's filters and ours overlap but aren't identical, so the gates
    stay as a net and the per-page log shows how often each one fires."""
    kept: list[dict] = []
    page_num = start_page
    while len(kept) < target:
        try:
            if url_template:
                rows, _total = await scrape_list_page(
                    session, region, page_num, url_template)
            else:
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
            if _is_cheap_eats(row, cheap_floor):
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
          f"(all >¥{cheap_floor:,} and <¥{FINE_DINE_THRESHOLD_YEN:,})")
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


def plan_tokyo(
    stats: dict[str, dict[str, int]],
    totals: dict[str, int],
    existing: dict[str, dict],
    hard_cap: int,
    ratio: float,
    start_page: int,
    url_template: str,
    cheap_floor: int,
) -> list[dict]:
    """One plan for Tokyo off the filtered list. Same deficit math as
    plan_topup, but start_page comes from the operator (the filtered list has
    its own pagination, unrelated to the plain list's max source_page) and the
    filtered url_template + cheap_floor ride on the plan."""
    region = "tokyo"
    s = stats.get(region)
    total = totals.get(region)
    if s is None:
        print(f"[{region}] no rows in tabelog.csv; nothing to do")
        return []
    if total is None:
        print(f"[{region}] no total in cache; run audit_main_meal_coverage.py")
        return []
    threshold = math.ceil(total * ratio)
    deficit = threshold - s["main_meal"]
    if deficit <= 0:
        print(f"[{region}] already at threshold "
              f"(main={s['main_meal']} >= {threshold}); nothing to do")
        return []
    return [{
        "region": region,
        "total": total,
        "deficit": deficit,
        "target": min(deficit, hard_cap),
        "start_page": start_page,
        "seen_urls": existing.get(region, {}).get("urls", set()),
        "url_template": url_template,
        "cheap_floor": cheap_floor,
    }]


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
        help=f"normal top-up only: max new rows per region (default: {HARD_CAP_DEFAULT})",
    )
    ap.add_argument(
        "--ratio", type=float, default=MAIN_MEAL_RATIO,
        help=f"normal top-up only: main-meal share target as a fraction (default: "
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
    ap.add_argument(
        "--tokyo-osaka-append", action="store_true",
        help="continue the supplied Tokyo and Osaka ranking lists through score 3.50 or page 60",
    )
    ap.add_argument(
        "--tokyo-list-url", type=str,
        help="HTTPS Tokyo ranking URL override (default: existing filtered list, page 10)",
    )
    ap.add_argument(
        "--osaka-list-url", type=str,
        help="HTTPS Osaka ranking URL override (default: supplied filtered list, page 8)",
    )
    ap.add_argument(
        "--tokyo-start-page", type=int,
        help="Tokyo first page; defaults to the page in --tokyo-list-url, or 1",
    )
    ap.add_argument(
        "--osaka-start-page", type=int,
        help="Osaka first page; defaults to the page in --osaka-list-url, or 1",
    )
    ap.add_argument(
        "--tokyo", action="store_true",
        help="Tokyo-only, off Tabelog's own filtered list (dinner "
             "¥1,000-¥20,000, RC restaurant category). Use this because the "
             "plain list caps at 60 pages, which Tokyo already exhausted.",
    )
    ap.add_argument(
        "--start-page", type=int, default=TOKYO_DEFAULT_START_PAGE,
        help=f"--tokyo only: first page of the filtered list to fetch "
             f"(default: {TOKYO_DEFAULT_START_PAGE}). Earlier pages are the "
             f"highest-rated and mostly already collected; dedupe skips overlap.",
    )
    ap.add_argument(
        "--cheap-floor", type=int, default=CHEAP_EATS_THRESHOLD_YEN,
        help=f"drop cards whose effective price <= this (default: "
             f"{CHEAP_EATS_THRESHOLD_YEN}). --tokyo's URL already floors "
             f"dinner at ¥3,000, matching this default.",
    )
    ap.add_argument(
        "--svd", type=str, default=None,
        help="--tokyo only: the svd reservation-date form value in the URL "
             "(YYYYMMDD). Inert (vac_net=0) but kept to match the hand-checked "
             "page; defaults to today.",
    )
    return ap.parse_args(argv)


async def amain(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.tokyo_osaka_append and args.tokyo:
        sys.exit("--tokyo-osaka-append and legacy --tokyo are separate modes")
    if args.tokyo_osaka_append and args.regions:
        sys.exit("--tokyo-osaka-append always covers both Tokyo and Osaka; omit regions")
    if not TABELOG_CSV.exists():
        sys.exit(f"missing {TABELOG_CSV}")

    print(f"Reading {TABELOG_CSV.name} ...")
    existing = scan_existing(TABELOG_CSV)
    if args.tokyo_osaka_append:
        append_seen_urls = {
            url for region_data in existing.values()
            for url in region_data["urls"]
        }
        try:
            plans = [
                {
                    "region": region,
                    "list_url": url,
                    "start_page": append_start_page(url, region, start),
                    "seen_urls": append_seen_urls,
                }
                for region, url, start in (
                    ("tokyo", resolve_list_url_date(
                        args.tokyo_list_url or TOKYO_APPEND_LIST_URL
                    ), args.tokyo_start_page),
                    ("osaka", resolve_list_url_date(
                        args.osaka_list_url or OSAKA_APPEND_LIST_URL
                    ), args.osaka_start_page),
                )
            ]
        except ValueError as exc:
            sys.exit(str(exc))
        for plan in plans:
            print(f"[{plan['region']}] start page {plan['start_page']}, "
                  f"page limit {APPEND_PAGE_LIMIT}, score floor {APPEND_SCORE_FLOOR}; "
                  f"list URL {plan['list_url']}")
    else:
        stats = load_region_stats(TABELOG_CSV)
        totals = load_totals_cache()
        only = [r.strip().lower() for r in args.regions] if args.regions else None
        if args.tokyo:
            if only and only != ["tokyo"]:
                print(f"--tokyo scrapes tokyo only; ignoring extra region args {only}")
            svd = args.svd or datetime.date.today().strftime("%Y%m%d")
            tokyo_template = TOKYO_LIST_TEMPLATE.replace("{svd}", svd)
            print(f"--tokyo: filtered list, start page {args.start_page}, "
                  f"cheap-floor ¥{args.cheap_floor:,}, svd={svd}")
            plans = plan_tokyo(
                stats, totals, existing, args.hard_cap, args.ratio,
                args.start_page, tokyo_template, args.cheap_floor,
            )
        else:
            plans = plan_topup(stats, totals, existing, only, args.hard_cap, args.ratio)
        if not plans:
            print("\nNothing to top up.")
            return
        print_plan(plans, args.hard_cap)
    if args.dry_run:
        return

    INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)
    intermediate = INTERMEDIATE_DIR / (
        "tabelog_tokyo_osaka_append_intermediate.csv"
        if args.tokyo_osaka_append else "tabelog_topup_intermediate.csv"
    )
    all_kept: list[dict] = []
    append_errors: list[str] = []

    async with async_playwright() as p:
        session = Session(p)
        await session.connect()

        for plan in plans:
            print(f"\n=== {plan['region']} ===")
            if args.tokyo_osaka_append:
                print(f"  resume from page {plan['start_page']}")
                try:
                    result = await collect_append(
                        session, plan["region"], plan["start_page"],
                        plan["list_url"], plan["seen_urls"],
                        cheap_floor=args.cheap_floor,
                    )
                    kept = result.rows
                    if result.stop_reason == "score_floor":
                        print(f"[{plan['region']}] score_floor: below 3.50 observed "
                              f"on page {result.last_page}")
                    else:
                        print(f"[{plan['region']}] page_limit: reached page "
                              f"{APPEND_PAGE_LIMIT}; the score floor was not observed")
                    if result.unknown_ratings:
                        print(f"[{plan['region']}] {result.unknown_ratings} cards "
                              f"had no usable rating and were skipped")
                except AppendCollectionError as exc:
                    kept = exc.rows
                    append_errors.append(str(exc))
                    print(f"[{plan['region']}] INCOMPLETE: {exc}")
            else:
                print(f"  total={plan['total']}, deficit={plan['deficit']}, "
                      f"target={plan['target']} (hard_cap={args.hard_cap})")
                print(f"  resume from page {plan['start_page']}")
                kept = await collect_topup(
                    session,
                    plan["region"],
                    plan["start_page"],
                    plan["target"],
                    plan["seen_urls"],
                    url_template=plan.get("url_template"),
                    cheap_floor=plan.get("cheap_floor", CHEAP_EATS_THRESHOLD_YEN),
                )
            all_kept.extend(kept)
            write_intermediate(all_kept, intermediate)

        if not all_kept:
            if append_errors:
                sys.exit("Append incomplete: " + "; ".join(append_errors))
            print("\nNo new rows collected.")
            return

        print(f"\nCollected {len(all_kept)} new rows. Visiting detail pages ...")
        detail_rejected_urls: set[str] = set()
        for n, row in enumerate(all_kept, 1):
            url = row.get("detail_url")
            if not url:
                continue
            try:
                d = await fetch_detail(session, url)
            except Exception as e:
                print(f"  [{n}/{len(all_kept)}] {row.get('name')!r}: gave up — {e}")
                continue
            if args.tokyo_osaka_append:
                reject_reason = append_detail_reject_reason(d)
                if reject_reason:
                    detail_rejected_urls.add(url)
                    print(f"  [{n}/{len(all_kept)}] {row.get('name')!r}: "
                          f"excluded ({reject_reason})")
                    continue
                row["operating_status"] = d.get("operating_status") or "unknown"
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
                write_intermediate(
                    [r for r in all_kept if r.get("detail_url") not in detail_rejected_urls],
                    intermediate,
                )
        if detail_rejected_urls:
            all_kept = [
                row for row in all_kept
                if row.get("detail_url") not in detail_rejected_urls
            ]
            print(f"Excluded {len(detail_rejected_urls)} append candidates "
                  f"after detail checks")
        write_intermediate(all_kept, intermediate)

    if not all_kept:
        if append_errors:
            sys.exit("Append incomplete: " + "; ".join(append_errors))
        print("No new rows passed detail checks.")
        return
    if args.translate:
        await translate_reservation_policy(all_kept, intermediate)

    append_and_dedupe(all_kept, TABELOG_CSV)
    if append_errors:
        sys.exit("Append incomplete: " + "; ".join(append_errors))

def main(argv: list[str] | None = None) -> None:
    asyncio.run(amain(argv))


if __name__ == "__main__":
    main()
