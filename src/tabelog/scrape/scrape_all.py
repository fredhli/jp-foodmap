"""
Unified Tabelog list scraper. Pass a Tabelog region slug — the URL only
varies by that one path segment.

  uv run python src/tabelog/scrape/scrape_all.py osaka
  uv run python src/tabelog/scrape/scrape_all.py hyogo --top-pct 0.5
  uv run python src/tabelog/scrape/scrape_all.py okayama --hard-cap 200
  uv run python src/tabelog/scrape/scrape_all.py tottori --top-pct 2 --hard-cap 100

Phase 1 paginates the list (sorted by rating desc). On the first page it
reads the region's total restaurant count (the "全 N 件" badge) and
computes target = min(round(total * top_pct / 100), hard_cap), then keeps
paginating until that many cards are collected. Phase 2 visits each detail
page for address + tabelog_bookable (plus seat_count / reservation_policy
as schema fillers). Phase 3 appends to data/tabelog/tabelog.csv (unified
across all regions, each row carries a `region` column) and dedupes by
detail_url, keeping the newly-scraped row when a URL appears in both.

Genre / holiday / reservation_policy stay in Japanese — no translation.
reservation_policy_chinese is preserved in the schema but left blank for new
rows; existing translated rows keep their value unless overwritten by dedupe.
"""

import argparse
import asyncio
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from deep_translator import GoogleTranslator
from playwright.async_api import async_playwright
from tabelog.browser import get_or_spawn_chrome
from tabelog.paths import INTERMEDIATE_DIR, TABELOG_CSV, TABELOG_DIR

BASE_TEMPLATE = "https://tabelog.com/{region}/rstLst/{page}/?Srt=D&SrtT=rt&sort_mode=1"
LIST_PAGE_DELAY_S = 2.0
DETAIL_PAGE_DELAY_S = 1.5
CHECKPOINT_EVERY = 10
MAX_RETRIES = 3                 # per row/page
RECONNECT_BACKOFF_S = 3.0       # after the browser/page dies

FIELDS = [
    "region",
    "rank", "name", "rating", "review_count", "save_count",
    "genre", "station", "station_distance_m",
    "dinner_upper", "lunch_upper", "holiday",
    "seat_count", "address", "reservation_policy",
    "reservation_policy_chinese", "tabelog_bookable",
    "detail_url", "source_page",
    "lat", "lon",
    "photo1_url", "photo2_url", "photo3_url",
]

CARDS_JS = r"""
() => {
    const cards = Array.from(document.querySelectorAll('.list-rst.js-rst-cassette-wrap'));
    const cardData = cards.map(card => {
        const detailUrl = card.getAttribute('data-detail-url') || null;
        const rankEl = card.querySelector('.c-ranking-badge__contents');
        const rank = rankEl ? rankEl.innerText.trim() : null;
        const nameEl = card.querySelector('.list-rst__rst-name-target');
        const name = nameEl ? nameEl.innerText.trim() : null;
        const areaGenreEl = card.querySelector('.list-rst__area-genre');
        const areaGenreText = areaGenreEl ? areaGenreEl.innerText.replace(/\s+/g, ' ').trim() : '';
        const ratingEl = card.querySelector('.list-rst__rating-val');
        const rating = ratingEl ? ratingEl.innerText.trim() : null;
        const rvwEl = card.querySelector('.list-rst__rvw-count-num');
        const review_count = rvwEl ? rvwEl.innerText.trim() : null;
        const saveEl = card.querySelector('.list-rst__save-count-num');
        const save_count = saveEl ? saveEl.innerText.trim() : null;
        const items = Array.from(card.querySelectorAll('.list-rst__info-item'));
        let dinner = '', lunch = '', holiday = '';
        for (const it of items) {
            const i = it.querySelector('i[aria-label]');
            const v = it.querySelector('.c-rating-v3__val, .list-rst__holiday-text');
            const label = i ? i.getAttribute('aria-label') : '';
            const val = v ? v.innerText.trim() : '';
            if (label === '夜の予算') dinner = val;
            else if (label === '昼の予算') lunch = val;
            else if (label === '定休日') holiday = val;
        }
        return {detailUrl, rank, name, areaGenreText, rating,
                review_count, save_count, dinner, lunch, holiday};
    });
    // pull "全 N 件" from the page header (e.g. "1～20 件を表示 ／ 全 71824 件")
    const bodyText = document.body ? (document.body.innerText || '') : '';
    const m = bodyText.match(/全\s*([\d,]+)\s*件/);
    const total = m ? parseInt(m[1].replace(/,/g, ''), 10) : null;
    return {cards: cardData, total};
}
"""

DETAIL_JS = r"""
() => {
    const out = {seat_count: null, address: null,
                 reservation_policy: null, tabelog_bookable: false};
    for (const tr of document.querySelectorAll('tr')) {
        const th = tr.querySelector('th');
        const td = tr.querySelector('td');
        if (!th || !td) continue;
        const label = (th.innerText || '').trim();
        const value = (td.innerText || '').replace(/\s+/g, ' ').trim();
        if (label === '席数') out.seat_count = value;
        else if (label === '住所') out.address = value;
        else if (label === '予約可否') out.reservation_policy = value;
    }
    out.tabelog_bookable = document.querySelectorAll('a.js-booking-form-open').length > 0;
    return out;
}
"""

# Photo carousel images appear in detail-page HTML as
#   https://tblg.k-img.com/resize/660x370c/restaurant/images/Rvw/{rid}/{photo_id}.jpg
# The /resize/* paths are token-gated (403 outside the page), but the same
# (rid, photo_id) rebuilds a public CDN URL that the map's JS can rewrite
# to 150x150_square_ for thumbnails. photo_id is either a 32-char hex hash
# or a numeric ID — both work with the 640x640_rect_ prefix.
CAROUSEL_RE = re.compile(
    r"tblg\.k-img\.com/resize/[^/]+/restaurant/images/Rvw/(\d+)/([A-Za-z0-9_-]+)\.jpg"
)


def extract_photo_urls(html: str, limit: int = 3) -> list[str]:
    """First `limit` unique (rid, photo_id) pairs in document order, as
    public CDN URLs."""
    seen: set[tuple[str, str]] = set()
    out: list[str] = []
    for m in CAROUSEL_RE.finditer(html):
        key = (m.group(1), m.group(2))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            f"https://tblg.k-img.com/restaurant/images/Rvw/{key[0]}/"
            f"640x640_rect_{key[1]}.jpg"
        )
        if len(out) >= limit:
            break
    return out


def parse_area_genre(s: str) -> tuple[str, str | None, int | None]:
    """'谷町六丁目駅 317m / 寿司' -> ('寿司', '谷町六丁目駅', 317)"""
    s = s.strip()
    if not s:
        return ("", None, None)
    if "/" in s:
        left, right = s.split("/", 1)
        left, genre = left.strip(), right.strip()
    else:
        left, genre = "", s
    station, distance_m = None, None
    if left:
        m = re.match(r"^(.+?駅)\s*(\d+)\s*m\s*$", left)
        if m:
            station, distance_m = m.group(1), int(m.group(2))
        else:
            station = left or None
    return genre, station, distance_m


def parse_price_upper(s: str) -> int | None:
    """Extract the bucketing int from a Tabelog budget string.

    '￥30,000～￥39,999' -> 39999   (two-sided: upper bound)
    '～￥999'            -> 999     (capped: that IS the upper)
    '￥30,000～'         -> 30000   (open above: use the lower so it buckets right)
    '-' / ''             -> None
    """
    s = (s or "").strip()
    if not s or s == "-":
        return None
    num = r"([\d,]+)"
    m = re.search(rf"￥{num}\s*～\s*￥{num}", s)
    if m:
        return int(m.group(2).replace(",", ""))
    m = re.match(rf"^\s*～\s*￥{num}\s*$", s)
    if m:
        return int(m.group(1).replace(",", ""))
    m = re.match(rf"^\s*￥{num}\s*～\s*$", s)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def clean_address(s: str | None) -> str:
    if not s:
        return ""
    for stopper in ("大きな地図", "周辺のお店", "このお店は"):
        i = s.find(stopper)
        if i > 0:
            s = s[:i]
    return s.strip()


class Session:
    """Holds the playwright instance + current page; can refresh both if
    Chrome closes mid-run."""
    def __init__(self, p):
        self.p = p
        self.browser = None
        self.page = None

    async def connect(self) -> None:
        self.browser = await get_or_spawn_chrome(self.p)
        ctx = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
        # wait briefly for a tab to materialize
        for _ in range(20):
            if ctx.pages:
                break
            await asyncio.sleep(0.2)
        pages = list(ctx.pages)
        self.page = next(
            (pg for pg in pages if "tabelog.com" in (pg.url or "")),
            pages[0] if pages else await ctx.new_page(),
        )
        try:
            await self.page.bring_to_front()
        except Exception:
            pass

    async def reconnect(self) -> None:
        print(f"    [session] reconnecting in {RECONNECT_BACKOFF_S:.0f}s ...")
        await asyncio.sleep(RECONNECT_BACKOFF_S)
        await self.connect()
        print("    [session] reconnected")


def _is_session_dead(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in (
        "target page", "context or browser has been closed",
        "browser has been closed", "page has been closed",
        "connection closed", "websocket", "target closed",
    ))


async def scrape_list_page(session: Session, region: str, page_num: int) -> tuple[list[dict], int | None]:
    url = BASE_TEMPLATE.format(region=region, page=page_num)
    print(f"[list page {page_num}] GET {url}")
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await session.page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(LIST_PAGE_DELAY_S)
            raw = await session.page.evaluate(CARDS_JS)
            break
        except Exception as e:
            last_err = e
            print(f"  [list page {page_num}] attempt {attempt}/{MAX_RETRIES} ERROR: {e}")
            if _is_session_dead(e):
                await session.reconnect()
            else:
                await asyncio.sleep(1.5 * attempt)
    else:
        raise RuntimeError(f"list page {page_num} failed after {MAX_RETRIES} attempts: {last_err}")
    rows = []
    for c in raw["cards"]:
        genre, station, dist = parse_area_genre(c["areaGenreText"])
        rows.append({
            "region": region,
            "rank": c["rank"], "name": c["name"],
            "rating": c["rating"], "review_count": c["review_count"],
            "save_count": c["save_count"],
            "genre": genre, "station": station, "station_distance_m": dist,
            "dinner_upper": parse_price_upper(c["dinner"]),
            "lunch_upper": parse_price_upper(c["lunch"]),
            "holiday": c["holiday"],
            "seat_count": "", "address": "", "reservation_policy": "",
            "reservation_policy_chinese": "", "tabelog_bookable": "",
            "detail_url": c["detailUrl"], "source_page": page_num,
        })
    return rows, raw.get("total")


async def collect_list(session: Session, region: str, top_pct: float, hard_cap: int) -> list[dict]:
    kept: list[dict] = []
    target: int | None = None
    page_num = 1
    while True:
        try:
            rows, total = await scrape_list_page(session, region, page_num)
        except Exception as e:
            print(f"[list page {page_num}] gave up: {e}")
            break
        if not rows:
            print(f"[list page {page_num}] no cards parsed; stopping")
            break

        if target is None:
            if total is None:
                print(f"[list page {page_num}] could not read '全 N 件'; "
                      f"falling back to hard-cap {hard_cap}")
                target = hard_cap
            else:
                pct_quota = round(total * top_pct / 100)
                target = min(pct_quota, hard_cap)
                print(f"[list page {page_num}] total={total}, top {top_pct}% = "
                      f"{pct_quota}, hard-cap={hard_cap} -> target={target}")

        remaining = target - len(kept)
        kept.extend(rows[:remaining])

        print(f"[list page {page_num}] {len(rows)} cards, "
              f"running kept={len(kept)}/{target}")

        if len(kept) >= target:
            print(f"[list page {page_num}] reached target {target}; stopping pagination")
            break
        page_num += 1
    return kept


async def fetch_detail(session: Session, url: str) -> dict:
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await session.page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(DETAIL_PAGE_DELAY_S)
            data = await session.page.evaluate(DETAIL_JS)
            data["address"] = clean_address(data.get("address"))
            html = await session.page.content()
            data["photos"] = extract_photo_urls(html)
            return data
        except Exception as e:
            last_err = e
            if _is_session_dead(e):
                print(f"    attempt {attempt}/{MAX_RETRIES} session dead: {e}")
                await session.reconnect()
            else:
                print(f"    attempt {attempt}/{MAX_RETRIES} ERROR: {e}")
                await asyncio.sleep(1.5 * attempt)
    raise RuntimeError(f"detail fetch failed after {MAX_RETRIES} attempts: {last_err}")


async def translate_reservation_policy(rows: list[dict], checkpoint: Path) -> None:
    """ja -> zh-CN for reservation_policy only. Genre/holiday stay Japanese."""
    targets = [r for r in rows if r.get("reservation_policy")]
    if not targets:
        print("\nNo reservation_policy text to translate.")
        return
    print(f"\nTranslating {len(targets)} reservation_policy values -> zh-CN ...")
    translator = GoogleTranslator(source="ja", target="zh-CN")
    for n, row in enumerate(targets, 1):
        ja = row["reservation_policy"]
        try:
            zh = translator.translate(ja) or ""
        except Exception as e:
            print(f"  [{n}/{len(targets)}] FAIL: {e}")
            zh = ""
        row["reservation_policy_chinese"] = zh
        print(f"  [{n}/{len(targets)}] {row.get('name')!r}: {len(ja)} -> {len(zh)} chars")
        if n % CHECKPOINT_EVERY == 0:
            write_intermediate(rows, checkpoint)
        await asyncio.sleep(0.3)
    write_intermediate(rows, checkpoint)


def write_intermediate(rows: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def append_and_dedupe(new_rows: list[dict], csv_path: Path) -> None:
    """Append new_rows to csv_path then dedupe by detail_url, keep-last.

    Existing CSV may have a different (older) column set; we widen the union
    of fieldnames so nothing gets dropped. New rows always carry FIELDS.
    """
    existing: list[dict] = []
    existing_fields: list[str] = []
    if csv_path.exists():
        with csv_path.open(encoding="utf-8-sig", newline="") as f:
            r = csv.DictReader(f)
            existing_fields = list(r.fieldnames or [])
            existing = list(r)

    fieldnames = list(existing_fields)
    for k in FIELDS:
        if k not in fieldnames:
            fieldnames.append(k)

    combined = existing + [{k: ("" if r.get(k) is None else r.get(k, ""))
                            for k in fieldnames} for r in new_rows]

    seen: dict[str, dict] = {}
    order: list[str] = []
    for row in combined:
        url = row.get("detail_url") or ""
        if not url:
            url = f"__no_url__{len(order)}"
        if url not in seen:
            order.append(url)
        seen[url] = row

    deduped = [seen[u] for u in order]

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(deduped)

    print(f"\nWrote {len(deduped)} rows ({len(combined) - len(deduped)} duplicates "
          f"collapsed) -> {csv_path}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("region", type=str,
                    help="Tabelog region slug — the path segment between "
                         "'tabelog.com/' and '/rstLst/'. Examples: osaka, "
                         "hyogo, kyoto, okayama, tottori, kobe. Case-insensitive.")
    ap.add_argument("--top-pct", type=float, default=1.0,
                    help="scrape the top N%% of the region's restaurants "
                         "by rating (default: 1.0)")
    ap.add_argument("--hard-cap", type=int, default=400,
                    help="absolute upper bound on rows kept, applied after "
                         "top-pct (default: 400)")
    ap.add_argument("--translate", action=argparse.BooleanOptionalAction, default=False,
                    help="translate reservation_policy -> reservation_policy_chinese "
                         "via Google (default: off). Use --translate / --no-translate.")
    return ap.parse_args(argv)


async def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.top_pct <= 0:
        sys.exit(f"--top-pct must be > 0 (got {args.top_pct})")
    if args.hard_cap <= 0:
        sys.exit(f"--hard-cap must be > 0 (got {args.hard_cap})")

    region = args.region.strip().lower()
    if not region or "/" in region:
        sys.exit(f"invalid region slug: {args.region!r}")

    INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)
    TABELOG_DIR.mkdir(parents=True, exist_ok=True)

    range_tag = f"top{args.top_pct:g}pct_cap{args.hard_cap}"
    intermediate = INTERMEDIATE_DIR / f"tabelog_{region}_scrape_{range_tag}.csv"
    out_csv = TABELOG_CSV

    print(f"Region: {region}  ->  {out_csv.name}")
    print(f"Target: top {args.top_pct}% of region, hard-capped at {args.hard_cap}")

    async with async_playwright() as p:
        session = Session(p)
        await session.connect()

        kept = await collect_list(session, region, args.top_pct, args.hard_cap)
        print(f"\nCollected {len(kept)} list rows. Now visiting detail pages.")
        write_intermediate(kept, intermediate)

        for n, row in enumerate(kept, 1):
            url = row["detail_url"]
            if not url:
                continue
            try:
                d = await fetch_detail(session, url)
            except Exception as e:
                print(f"  [{n}/{len(kept)}] {row.get('name')!r}: gave up — {e}")
                continue
            row["seat_count"] = d.get("seat_count") or ""
            row["address"] = d.get("address") or ""
            row["reservation_policy"] = d.get("reservation_policy") or ""
            row["tabelog_bookable"] = "True" if d.get("tabelog_bookable") else "False"
            photos = d.get("photos") or []
            row["photo1_url"] = photos[0] if len(photos) > 0 else ""
            row["photo2_url"] = photos[1] if len(photos) > 1 else ""
            row["photo3_url"] = photos[2] if len(photos) > 2 else ""
            print(f"  [{n}/{len(kept)}] {row.get('name')!r}: "
                  f"seats={row['seat_count']!r}, bookable={row['tabelog_bookable']}, "
                  f"photos={len(photos)}")
            if n % CHECKPOINT_EVERY == 0:
                write_intermediate(kept, intermediate)
        write_intermediate(kept, intermediate)

    if args.translate:
        await translate_reservation_policy(kept, intermediate)

    append_and_dedupe(kept, out_csv)


if __name__ == "__main__":
    asyncio.run(main())
