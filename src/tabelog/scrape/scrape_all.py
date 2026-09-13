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
paginating until that many cards are collected. A separate quota caps the
number of fine-dining rows (dinner_upper or lunch_upper >= ¥20,000) at
max(round(total * fine_dine_pct / 100), 5) — small regions still get
at least 5 fine-dining slots. Once that quota is hit, further
fine-dining cards are skipped while cheaper rows continue accumulating
toward `target`. Phase 2 visits each detail page for address +
tabelog_bookable (plus seat_count / reservation_policy as schema fillers).
Phase 3 appends to data/tabelog/tabelog.csv (unified across all regions,
each row carries a `region` column) and dedupes by detail_url, keeping
the newly-scraped row when a URL appears in both.

reservation_policy is translated to zh-CN by default (uses Google Translate
via deep_translator). Pass --no-translate to skip if you're rate-limited or
want a quick run. Genre / holiday stay Japanese.
"""

import argparse
import asyncio
import csv
import json
from html import unescape
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

# Windows: when launched through a subprocess / pipe, stdout falls back to
# cp1252 and chokes on Japanese restaurant names in progress prints.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from deep_translator import GoogleTranslator
from playwright.async_api import async_playwright
from tabelog.browser import get_or_spawn_chrome
from tabelog.paths import (
    INTERMEDIATE_DIR,
    SCRAPE_FAILED_LEDGER,
    TABELOG_CSV,
    TABELOG_DIR,
    atomic_write_csv,
)

BASE_TEMPLATE = "https://tabelog.com/{region}/rstLst/{page}/?Srt=D&SrtT=rt&sort_mode=1"
LIST_PAGE_DELAY_S = 2.0
DETAIL_PAGE_DELAY_S = 1.5
CHECKPOINT_EVERY = 10
MAX_RETRIES = 3                 # per row/page
RECONNECT_BACKOFF_S = 3.0       # after the browser/page dies
FINE_DINE_THRESHOLD_YEN = 20000  # dinner_upper / lunch_upper at or above this = fine-dining
FINE_DINE_MIN_CAP = 5            # floor for the fine-dine quota (so small regions still get a few)

FIELDS = [
    "region",
    "rank", "name", "rating", "review_count", "save_count",
    "awards",
    "genre", "station", "station_distance_m",
    "dinner_upper", "lunch_upper", "holiday",
    "seat_count", "address", "reservation_policy",
    "reservation_policy_chinese", "tabelog_bookable",
    "detail_url", "source_page",
    "lat", "lon",
    "photo1_url", "photo2_url", "photo3_url",
    # M-096: per-row scrape timestamp (UTC, ISO-8601). Rows written before
    # this column existed keep an empty value — append_and_dedupe already
    # unions fieldnames, so adding it is a no-op for the existing corpus.
    # map.py prints age quantiles over the rows that do carry one.
    "scraped_at",
]

# M-019: columns whose old value must survive a re-scrape that came back
# empty. lat/lon are never produced by phase 1 at all (M-172); photos and the
# translated policy come from phase 2 / the translate pass and go blank on any
# detail-page failure. Everything NOT listed here (rating, awards, price, ...)
# is overwritten by the new value even when that value is empty — otherwise an
# award that lapsed could never be cleared.
PRESERVE_IF_EMPTY = (
    "lat", "lon",
    "photo1_url", "photo2_url", "photo3_url",
    "reservation_policy_chinese",
)

# M-019: a phase-2 failure (HTTP error, or a 200 whose '住所' row moved and the
# selector silently returned null) leaves address empty. Merging such a row
# would drop the restaurant out of restaurants.json for good, so the row is
# rejected. If more than this share of the scraped batch is address-less, the
# scrape itself is broken (Tabelog redesign) and the master CSV is not touched.
MAX_EMPTY_ADDRESS_PCT = 5.0

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
        const awards = Array.from(card.querySelectorAll('.list-rst__award-badge')).map(b => {
            const span = b.querySelector('span[class*="c-badge-"]');
            const tip = b.querySelector('.list-rst__award-tooltip');
            const shortEl = b.querySelector('i');
            const short = shortEl ? shortEl.innerText.trim() : '';
            const long = tip ? tip.innerText.replace(/\s+/g, ' ').trim() : '';
            const cls = span ? span.className : '';
            const kind = cls.includes('c-badge-award') ? 'award'
                       : cls.includes('c-badge-hyakumeiten') ? 'hyakumeiten'
                       : 'other';
            const m = cls.match(/--(\d{4}[a-z]+)/);
            const variant = m ? m[1] : '';
            return {kind, variant, short, long};
        });
        return {detailUrl, rank, name, areaGenreText, rating,
                review_count, save_count, dinner, lunch, holiday, awards};
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

# Two URL shapes appear on detail pages depending on whether the restaurant
# has a hero carousel:
#   carousel:   tblg.k-img.com/resize/660x370c/restaurant/images/Rvw/{rid}/{photo_id}.jpg
#   thumb grid: tblg.k-img.com/restaurant/images/Rvw/{rid}/{w}x{h}_(square|rect)_{photo_id}.jpg
# /resize/* is token-gated (403 outside the page); the thumb-grid form is
# the public CDN directly. Either way the (rid, photo_id) pair rebuilds a
# 640x640_rect_ URL. photo_id is a 32-char hex hash or a numeric ID.
CAROUSEL_RE = re.compile(
    r"tblg\.k-img\.com/resize/[^/]+/restaurant/images/Rvw/(\d+)/([A-Za-z0-9_-]+)\.jpg"
)
THUMBNAIL_RE = re.compile(
    r"tblg\.k-img\.com/restaurant/images/Rvw/(\d+)/"
    r"\d+x\d+_(?:square|rect)_([A-Za-z0-9]+)\.jpg"
)


def extract_photo_urls(html: str, limit: int = 3) -> list[str]:
    """First unique photos, preserving the actual URL found on the page."""
    seen: set[tuple[str, str]] = set()
    out: list[str] = []
    html = unescape(html)
    for regex in (CAROUSEL_RE, THUMBNAIL_RE):
        for m in regex.finditer(html):
            key = (m.group(1), m.group(2))
            if key in seen:
                continue
            seen.add(key)
            query = re.match(r'\?[^\s"<>\']*', html[m.end():])
            out.append("https://" + m.group(0) + (query.group(0) if query else ""))
            if len(out) >= limit:
                return out
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


async def scrape_list_page(
    session: Session,
    region: str,
    page_num: int,
    url_template: str = BASE_TEMPLATE,
) -> tuple[list[dict], int | None]:
    # url_template gets {region} and {page} filled in. Defaults to the plain
    # rating-sorted list; scrape_topup's --tokyo path passes a template that
    # carries Tabelog's own server-side price / category filters instead.
    url = url_template.format(region=region, page=page_num)
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
        awards_list = c.get("awards") or []
        awards_str = json.dumps(awards_list, ensure_ascii=False) if awards_list else ""
        rows.append({
            "region": region,
            "rank": c["rank"], "name": c["name"],
            "rating": c["rating"], "review_count": c["review_count"],
            "save_count": c["save_count"],
            "awards": awards_str,
            "genre": genre, "station": station, "station_distance_m": dist,
            "dinner_upper": parse_price_upper(c["dinner"]),
            "lunch_upper": parse_price_upper(c["lunch"]),
            "holiday": c["holiday"],
            "seat_count": "", "address": "", "reservation_policy": "",
            "reservation_policy_chinese": "", "tabelog_bookable": "",
            "detail_url": c["detailUrl"], "source_page": page_num,
            "scraped_at": _now_iso(),  # M-096
        })
    return rows, raw.get("total")


def _is_fine_dine(row: dict) -> bool:
    return (
        (row.get("dinner_upper") or 0) >= FINE_DINE_THRESHOLD_YEN
        or (row.get("lunch_upper") or 0) >= FINE_DINE_THRESHOLD_YEN
    )


async def collect_list(
    session: Session,
    region: str,
    top_pct: float,
    hard_cap: int,
    fine_dine_pct: float,
) -> list[dict]:
    kept: list[dict] = []
    fine_dine_count = 0
    fine_dine_skipped = 0
    target: int | None = None
    fine_dine_cap: int | None = None
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
                      f"falling back to hard-cap {hard_cap} "
                      f"(fine-dine cap disabled — needs total count)")
                target = hard_cap
                fine_dine_cap = None
            else:
                pct_quota = round(total * top_pct / 100)
                target = min(pct_quota, hard_cap)
                fd_pct_quota = round(total * fine_dine_pct / 100)
                fine_dine_cap = max(fd_pct_quota, FINE_DINE_MIN_CAP)
                print(f"[list page {page_num}] total={total}, top {top_pct}% = "
                      f"{pct_quota}, hard-cap={hard_cap} -> target={target}; "
                      f"fine-dine cap = max({fine_dine_pct}% = {fd_pct_quota}, "
                      f"{FINE_DINE_MIN_CAP}) = {fine_dine_cap} "
                      f"(>=¥{FINE_DINE_THRESHOLD_YEN:,})")

        page_kept = 0
        page_skipped = 0
        for row in rows:
            if len(kept) >= target:
                break
            if _is_fine_dine(row):
                if fine_dine_cap is not None and fine_dine_count >= fine_dine_cap:
                    fine_dine_skipped += 1
                    page_skipped += 1
                    continue
                fine_dine_count += 1
            kept.append(row)
            page_kept += 1

        print(f"[list page {page_num}] {len(rows)} cards, kept {page_kept} "
              f"(skipped {page_skipped} fine-dine), running kept={len(kept)}/{target}, "
              f"fine-dine={fine_dine_count}"
              + (f"/{fine_dine_cap}" if fine_dine_cap is not None else ""))

        if len(kept) >= target:
            print(f"[list page {page_num}] reached target {target}; stopping pagination "
                  f"(fine-dine total={fine_dine_count}, skipped={fine_dine_skipped})")
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


def _load_translation_cache(csv_path: Path) -> dict[str, str]:
    """Build {ja: zh} from every row in csv_path that has both fields filled.
    Used to skip API calls for policies we've already translated in a previous
    scrape (and to seed the in-batch dedup cache). Returns {} on first run."""
    cache: dict[str, str] = {}
    if not csv_path.exists():
        return cache
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            ja = (r.get("reservation_policy") or "").strip()
            zh = (r.get("reservation_policy_chinese") or "").strip()
            if ja and zh:
                cache[ja] = zh
    return cache


async def translate_reservation_policy(rows: list[dict], checkpoint: Path) -> None:
    """ja -> zh-CN for reservation_policy. Cache is seeded from the master
    CSV and grown in-batch, so duplicate policies ("予約可", "予約不可", ...)
    translate exactly once across the whole corpus."""
    targets = [
        r for r in rows
        if (r.get("reservation_policy") or "").strip()
        and not (r.get("reservation_policy_chinese") or "").strip()
    ]
    if not targets:
        print("\nNo reservation_policy text to translate.")
        return
    cache = _load_translation_cache(TABELOG_CSV)
    print(f"\nTranslating {len(targets)} reservation_policy values -> zh-CN "
          f"(cache seeded with {len(cache)} entries) ...")
    translator = GoogleTranslator(source="ja", target="zh-CN")
    n_api = 0
    n_cache = 0
    for n, row in enumerate(targets, 1):
        ja = row["reservation_policy"].strip()
        if ja in cache:
            zh = cache[ja]
            n_cache += 1
            tag = "cache"
        else:
            try:
                zh = translator.translate(ja) or ""
            except Exception as e:
                print(f"  [{n}/{len(targets)}] FAIL: {e}")
                zh = ""
            if zh:
                cache[ja] = zh
            n_api += 1
            tag = "api"
        row["reservation_policy_chinese"] = zh
        print(f"  [{n}/{len(targets)}] [{tag}] {row.get('name')!r}: "
              f"{len(ja)} -> {len(zh)} chars")
        if n % CHECKPOINT_EVERY == 0:
            write_intermediate(rows, checkpoint)
        if tag == "api":
            await asyncio.sleep(0.3)
    write_intermediate(rows, checkpoint)
    print(f"  translate summary: api {n_api}, cache {n_cache}")


def write_intermediate(rows: list[dict], path: Path) -> None:
    # M-020: atomic — a Ctrl-C during a checkpoint used to leave a truncated
    # intermediate CSV, which the resume path then read as "fewer rows".
    atomic_write_csv(
        path,
        [{k: r.get(k, "") for k in FIELDS} for r in rows],
        FIELDS,
    )


def _now_iso() -> str:
    """UTC, second precision, ISO-8601 with a trailing Z (M-096)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def merge_row(old: dict, new: dict, fieldnames: list[str]) -> dict:
    """M-019 / M-172: keep-last, except for the PRESERVE_IF_EMPTY columns
    where an empty new value falls back to the old one.

    Deliberately NOT a blanket "fill every missing key from old": `awards`,
    `rating`, `dinner_upper` etc. must be able to go back to empty when the
    restaurant genuinely lost the award / delisted the price.
    """
    merged: dict = {}
    for k in fieldnames:
        nv = new.get(k, "")
        nv = "" if nv is None else nv
        if k in PRESERVE_IF_EMPTY and _blank(nv):
            ov = old.get(k, "")
            merged[k] = "" if ov is None else ov
        else:
            merged[k] = nv
    return merged


def record_failed_rows(rows: list[dict], reason: str,
                       ledger_path: Path | None = None) -> None:
    """M-019: append-only JSONL trail of rows the merge refused. Never raises
    — a ledger problem must not abort a scrape that otherwise succeeded."""
    if not rows:
        return
    # Resolved at call time (not as a default arg) so tests can redirect the
    # ledger by patching the module constant.
    ledger_path = ledger_path or SCRAPE_FAILED_LEDGER
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = _now_iso()
        with ledger_path.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({
                    "at": stamp,
                    "reason": reason,
                    "region": r.get("region", ""),
                    "name": r.get("name", ""),
                    "detail_url": r.get("detail_url", ""),
                    "source_page": r.get("source_page", ""),
                }, ensure_ascii=False) + "\n")
        print(f"  recorded {len(rows)} rejected row(s) -> {ledger_path}")
    except OSError as e:
        print(f"  WARNING: could not write failed ledger {ledger_path}: {e}")


def append_and_dedupe(new_rows: list[dict], csv_path: Path,
                      max_empty_address_pct: float = MAX_EMPTY_ADDRESS_PCT) -> None:
    """Append new_rows to csv_path then dedupe by detail_url, keep-last with
    the M-019 guards:

      1. a new row with an empty `address` is dropped entirely (the old row
         survives) and logged to the failed ledger — an address-less row is
         excluded from restaurants.json and would never self-heal;
      2. per-column, an empty new value for lat/lon/photo*/policy_zh keeps the
         old value (M-172), everything else is overwritten as before;
      3. if more than `max_empty_address_pct` of the batch is address-less the
         scrape itself is broken — the master CSV is left untouched and the
         process exits non-zero.

    Existing CSV may have a different (older) column set; we widen the union
    of fieldnames so nothing gets dropped. New rows always carry FIELDS.
    """
    n_new = len(new_rows)
    empty_addr = [r for r in new_rows if _blank(r.get("address"))]
    empty_pct = (100.0 * len(empty_addr) / n_new) if n_new else 0.0
    if n_new and empty_pct > max_empty_address_pct:
        # M-019: the "HTTP 200 but the selector broke" case. Silently merging
        # here is how a Tabelog redesign wipes a whole region with exit code 0.
        record_failed_rows(empty_addr, "batch_rejected_empty_address")
        raise SystemExit(
            f"\nREFUSING to write {csv_path}: {len(empty_addr)}/{n_new} scraped "
            f"rows ({empty_pct:.1f}%) have an empty address, over the "
            f"{max_empty_address_pct:g}% threshold. That normally means the "
            f"detail-page selectors stopped matching (Tabelog redesign), not "
            f"that the restaurants lost their addresses. The master CSV was "
            f"NOT modified; the intermediate CSV is still on disk. Inspect "
            f"DETAIL_JS in scrape_all.py, then re-run."
        )

    existing: list[dict] = []
    existing_fields: list[str] = []
    had_existing = csv_path.exists()
    if had_existing:
        with csv_path.open(encoding="utf-8-sig", newline="") as f:
            r = csv.DictReader(f)
            existing_fields = list(r.fieldnames or [])
            existing = list(r)

    fieldnames = list(existing_fields)
    for k in FIELDS:
        if k not in fieldnames:
            fieldnames.append(k)

    by_url: dict[str, dict] = {}
    order: list[str] = []
    for row in existing:
        url = row.get("detail_url") or ""
        if not url:
            url = f"__no_url__{len(order)}"
        if url not in by_url:
            order.append(url)
        by_url[url] = {k: ("" if row.get(k) is None else row.get(k, ""))
                       for k in fieldnames}

    n_updated = 0
    n_added = 0
    rejected: list[dict] = []
    for row in new_rows:
        url = row.get("detail_url") or ""
        normalized = {k: ("" if row.get(k) is None else row.get(k, ""))
                      for k in fieldnames}
        if _blank(row.get("address")):
            # Guard 1: never let a blank detail page erase a good row. When the
            # URL is new there is nothing to protect, but the row would be
            # invisible on the map anyway, so it is rejected either way.
            rejected.append(row)
            continue
        if not url:
            url = f"__no_url__{len(order)}"
        if url in by_url:
            by_url[url] = merge_row(by_url[url], normalized, fieldnames)
            n_updated += 1
        else:
            order.append(url)
            by_url[url] = normalized
            n_added += 1

    deduped = [by_url[u] for u in order]

    # M-020: tmp + fsync + rename, with a one-generation .prev backup. The
    # master CSV is gitignored; before this there was no undo path at all.
    atomic_write_csv(csv_path, deduped, fieldnames, keep_prev=True)

    print(f"\nWrote {len(deduped)} rows -> {csv_path} "
          f"({n_added} new, {n_updated} updated in place, "
          f"{len(rejected)} rejected)")
    if had_existing:
        print(f"  previous version kept at {csv_path.name}.prev")
    if rejected:
        print(f"  {len(rejected)} scraped row(s) had no address and were "
              f"discarded (old rows kept):")
        for r in rejected[:10]:
            print(f"    - {r.get('name')!r} {r.get('detail_url')}")
        if len(rejected) > 10:
            print(f"    ... and {len(rejected) - 10} more")
        record_failed_rows(rejected, "empty_address")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("region", type=str,
                    help="Tabelog region slug — the path segment between "
                         "'tabelog.com/' and '/rstLst/'. Examples: osaka, "
                         "hyogo, kyoto, okayama, tottori, kobe. Case-insensitive.")
    ap.add_argument("--top-pct", type=float, default=1.0,
                    help="scrape the top N%% of the region's restaurants "
                         "by rating (default: 1.0)")
    ap.add_argument("--hard-cap", type=int, default=500,
                    help="absolute upper bound on rows kept, applied after "
                         "top-pct (default: 500)")
    ap.add_argument("--fine-dine-pct", type=float, default=0.1,
                    help="max share of the region's total restaurants allowed "
                         "to be fine-dining (dinner_upper or lunch_upper >= "
                         f"¥{FINE_DINE_THRESHOLD_YEN:,}). The cap is "
                         f"max(round(total * this%%), {FINE_DINE_MIN_CAP}) so "
                         "small regions still get a floor of "
                         f"{FINE_DINE_MIN_CAP} fine-dining slots. Once the cap "
                         "is hit, further fine-dining cards are skipped while "
                         "cheaper rows continue toward the top-pct/hard-cap "
                         "target (default: 0.1)")
    ap.add_argument("--translate", action=argparse.BooleanOptionalAction, default=True,
                    help="translate reservation_policy -> reservation_policy_chinese "
                         "via Google (default: on). Use --no-translate to skip.")
    return ap.parse_args(argv)


async def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.top_pct <= 0:
        sys.exit(f"--top-pct must be > 0 (got {args.top_pct})")
    if args.hard_cap <= 0:
        sys.exit(f"--hard-cap must be > 0 (got {args.hard_cap})")
    if args.fine_dine_pct < 0:
        sys.exit(f"--fine-dine-pct must be >= 0 (got {args.fine_dine_pct})")

    region = args.region.strip().lower()
    if not region or "/" in region:
        sys.exit(f"invalid region slug: {args.region!r}")

    INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)
    TABELOG_DIR.mkdir(parents=True, exist_ok=True)

    range_tag = f"top{args.top_pct:g}pct_cap{args.hard_cap}"
    intermediate = INTERMEDIATE_DIR / f"tabelog_{region}_scrape_{range_tag}.csv"
    out_csv = TABELOG_CSV

    print(f"Region: {region}  ->  {out_csv.name}")
    print(f"Target: top {args.top_pct}% of region, hard-capped at {args.hard_cap}; "
          f"fine-dine cap = {args.fine_dine_pct}% of region "
          f"(>=¥{FINE_DINE_THRESHOLD_YEN:,})")

    async with async_playwright() as p:
        session = Session(p)
        await session.connect()

        kept = await collect_list(
            session, region, args.top_pct, args.hard_cap, args.fine_dine_pct,
        )
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
