"""
Scrape Tabelog Osaka restaurant list, sorted by rating desc.
Keeps rows with MIN_RATING <= rating < MAX_RATING_EXCLUSIVE.
Stops paging when min rating on a page drops below MIN_RATING.

Phase 1 only: list-page fields. Detail-page fields (seats, full address)
are deferred — detail_url is recorded for a later pass.

All CSV I/O uses utf-8-sig so Excel renders Japanese cleanly.

Output:
  data/intermediate/tabelog_osaka_raw.csv   -- incremental, all kept rows
  data/tabelog/tabelog_osaka.csv            -- final, all kept rows
  data/tabelog/tabelog_osaka_price_le_<N>.csv -- filtered by MAX_DINNER
"""

import asyncio
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from playwright.async_api import async_playwright
from tabelog.browser import get_or_spawn_chrome
from tabelog.paths import INTERMEDIATE_DIR, TABELOG_DIR, TABELOG_OSAKA_CSV, TABELOG_OSAKA_RAW_CSV

INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)
TABELOG_DIR.mkdir(parents=True, exist_ok=True)
RAW_CSV = TABELOG_OSAKA_RAW_CSV

BASE = "https://tabelog.com/osaka/rstLst/{page}/?Srt=D&SrtT=rt&sort_mode=1"
MIN_RATING = 3.65            # inclusive lower bound
MAX_RATING_EXCLUSIVE = 3.80  # exclusive upper bound (3.80+ is the previous batch)
MAX_DINNER = 19999
PAGE_DELAY_S = 2.0

RANGE_TAG = f"{MIN_RATING:.2f}_to_{MAX_RATING_EXCLUSIVE:.2f}"

FIELDS = [
    "rank", "rst_id", "name", "rating", "review_count", "save_count",
    "genre", "station", "station_distance_m",
    "dinner_price", "dinner_upper", "lunch_price", "lunch_upper",
    "holiday", "detail_url", "source_page",
]

CARDS_JS = r"""
() => {
    const cards = Array.from(document.querySelectorAll('.list-rst.js-rst-cassette-wrap'));
    return cards.map(card => {
        const rstId = card.getAttribute('data-rst-id') || null;
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
        return {rstId, detailUrl, rank, name, areaGenreText, rating,
                review_count, save_count, dinner, lunch, holiday};
    });
}
"""


def parse_area_genre(s: str) -> tuple[str, str | None, int | None]:
    """ '谷町六丁目駅 317m / 寿司' -> ('寿司', '谷町六丁目駅', 317) """
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


def parse_price_range(s: str) -> tuple[str, int | None]:
    """Parse Tabelog budget text into (raw, upper_int_for_bucketing).

    Tabelog displays four shapes; the int returned is what we'd want to
    bucket by:
      '￥30,000～￥39,999' -> ('…', 39999)   # two-sided range, take upper
      '～￥999'            -> ('～￥999', 999) # capped, that IS the upper
      '￥30,000～'         -> ('￥30,000～', 30000) # open above; use lower
                                                   #   so it lands in the right bucket
      '-' / ''             -> ('', None)
    """
    s = (s or "").strip()
    if not s or s == "-":
        return "", None
    num = r"([\d,]+)"
    m = re.search(rf"￥{num}\s*～\s*￥{num}", s)
    if m:
        return s, int(m.group(2).replace(",", ""))
    m = re.match(rf"^\s*～\s*￥{num}\s*$", s)
    if m:
        return s, int(m.group(1).replace(",", ""))
    m = re.match(rf"^\s*￥{num}\s*～\s*$", s)
    if m:
        return s, int(m.group(1).replace(",", ""))
    return s, None


async def scrape_page(page, page_num: int) -> list[dict]:
    url = BASE.format(page=page_num)
    print(f"[page {page_num}] GET {url}")
    await page.goto(url, wait_until="domcontentloaded")
    await asyncio.sleep(PAGE_DELAY_S)
    raw = await page.evaluate(CARDS_JS)
    rows = []
    for c in raw:
        genre, station, dist = parse_area_genre(c["areaGenreText"])
        d_raw, d_upper = parse_price_range(c["dinner"])
        l_raw, l_upper = parse_price_range(c["lunch"])
        rows.append({
            "rank": c["rank"], "rst_id": c["rstId"], "name": c["name"],
            "rating": c["rating"], "review_count": c["review_count"],
            "save_count": c["save_count"],
            "genre": genre, "station": station, "station_distance_m": dist,
            "dinner_price": d_raw, "dinner_upper": d_upper,
            "lunch_price": l_raw, "lunch_upper": l_upper,
            "holiday": c["holiday"], "detail_url": c["detailUrl"],
            "source_page": page_num,
        })
    return rows


def append_rows(rows: list[dict], path: Path) -> None:
    write_header = not path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        w.writerows(rows)


def write_csv() -> None:
    if not RAW_CSV.exists():
        print("No raw CSV; skipping final CSV write")
        return

    with RAW_CSV.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    def _as_int(x: str) -> int | None:
        try:
            return int(x) if x not in ("", None) else None
        except ValueError:
            return None

    out_all = TABELOG_OSAKA_CSV
    out_filt = TABELOG_DIR / f"tabelog_osaka_price_le_{MAX_DINNER}.csv"

    n_filt = 0
    with out_all.open("w", encoding="utf-8-sig", newline="") as fa, \
         out_filt.open("w", encoding="utf-8-sig", newline="") as ff:
        wa = csv.DictWriter(fa, fieldnames=FIELDS)
        wf = csv.DictWriter(ff, fieldnames=FIELDS)
        wa.writeheader()
        wf.writeheader()
        for r in rows:
            ordered = {h: r.get(h, "") for h in FIELDS}
            wa.writerow(ordered)
            du = _as_int(r.get("dinner_upper", ""))
            lu = _as_int(r.get("lunch_upper", ""))
            # Dinner takes priority; fall back to lunch when dinner is NA.
            # Both NA -> keep (no info to filter on).
            if du is not None:
                keep = du <= MAX_DINNER
            elif lu is not None:
                keep = lu <= MAX_DINNER
            else:
                keep = True
            if keep:
                wf.writerow(ordered)
                n_filt += 1

    print(f"Wrote {len(rows)} rows -> {out_all.name}")
    print(f"Wrote {n_filt} rows -> {out_filt.name}")


async def main() -> None:
    if RAW_CSV.exists():
        RAW_CSV.unlink()

    async with async_playwright() as p:
        browser = await get_or_spawn_chrome(p)
        ctx = browser.contexts[0]
        page = next(
            (pg for pg in ctx.pages if "tabelog.com" in (pg.url or "")),
            ctx.pages[0],
        )
        await page.bring_to_front()

        page_num, total = 1, 0
        while True:
            try:
                rows = await scrape_page(page, page_num)
            except Exception as e:
                print(f"[page {page_num}] ERROR: {e}")
                break
            if not rows:
                print(f"[page {page_num}] no cards parsed; stopping")
                break

            ratings = [float(r["rating"]) for r in rows
                       if r["rating"] and re.match(r"^\d", r["rating"])]
            min_r = min(ratings) if ratings else None

            keep = [
                r for r in rows
                if r["rating"]
                and MIN_RATING <= float(r["rating"]) < MAX_RATING_EXCLUSIVE
            ]
            append_rows(keep, RAW_CSV)
            total += len(keep)
            print(f"[page {page_num}] {len(rows)} cards, min={min_r}, "
                  f"kept {len(keep)} (running total {total})")

            if min_r is not None and min_r < MIN_RATING:
                print(f"[page {page_num}] crossed MIN_RATING; stopping")
                break

            page_num += 1

    print(f"\nDone. {total} rows in [{MIN_RATING}, {MAX_RATING_EXCLUSIVE}) -> {RAW_CSV}")
    write_csv()


if __name__ == "__main__":
    asyncio.run(main())
