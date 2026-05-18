"""
Fetch up to 3 carousel photo URLs from each Tabelog detail page and stash
them back into data/tabelog/tabelog.csv as photo1_url / photo2_url /
photo3_url.

Tabelog ships its detail-page hero carousel as
  https://tblg.k-img.com/resize/660x370c/restaurant/images/Rvw/{id}/{hash}.jpg
but those /resize/* paths are token-gated and 403 from outside the page
context. Same image hash served from the un-resized CDN path is public:
  https://tblg.k-img.com/restaurant/images/Rvw/{id}/640x640_rect_{hash}.jpg
We persist that public URL; the map's JS can rewrite 640x640_rect_ ->
150x150_square_ on the fly for thumbnails.

  uv run python src/tabelog/scrape/fetch_photos.py            # all rows missing photos
  uv run python src/tabelog/scrape/fetch_photos.py --limit 10 # smoke test
  uv run python src/tabelog/scrape/fetch_photos.py --force    # re-scrape even if already populated

Idempotent: rows that already have photo1_url stay untouched unless --force.
Checkpoints to CSV every CHECKPOINT_EVERY rows so a Ctrl-C mid-run keeps
progress.
"""

import argparse
import asyncio
import csv
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from tabelog.paths import TABELOG_CSV

PHOTO_FIELDS = ("photo1_url", "photo2_url", "photo3_url")

# Matches any sized review-photo in the detail page HTML. The /resize/...
# variants are token-gated, but the (rid, photo_id) pair lets us rebuild the
# public /restaurant/images/Rvw/{rid}/640x640_rect_{photo_id}.jpg path.
#
# Filenames come in two flavors:
#   - 32-char hex hash:  ff2b57460b5eacd9619ef23c28af9739.jpg
#   - numeric ID:        140865031.jpg
# Both are valid; the public 640x640_rect_ prefix works for either.
#
# We prefer the 660x370c carousel size (appears 5+ times when a restaurant
# has a full carousel) but fall back to whatever resize sizes are present —
# single-photo restaurants only ship the og:image at 640x640c.
CAROUSEL_RE = re.compile(
    r"tblg\.k-img\.com/resize/[^/]+/restaurant/images/Rvw/(\d+)/([A-Za-z0-9_-]+)\.jpg"
)

CONCURRENCY = 6
PER_WORKER_DELAY_S = 0.8        # politeness; ~6 req/s aggregate
CHECKPOINT_EVERY = 25
REQUEST_TIMEOUT_S = 20.0
MAX_RETRIES = 3
RETRY_BACKOFF_S = 2.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.8",
}


def public_url(rid: str, photo_hash: str) -> str:
    return (
        f"https://tblg.k-img.com/restaurant/images/Rvw/{rid}/"
        f"640x640_rect_{photo_hash}.jpg"
    )


def extract_photo_urls(html: str, limit: int = 3) -> list[str]:
    """First `limit` unique (rid, hash) pairs in document order."""
    seen: set[tuple[str, str]] = set()
    out: list[str] = []
    for m in CAROUSEL_RE.finditer(html):
        key = (m.group(1), m.group(2))
        if key in seen:
            continue
        seen.add(key)
        out.append(public_url(*key))
        if len(out) >= limit:
            break
    return out


async def fetch_one(
    client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore
) -> list[str]:
    async with sem:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    await asyncio.sleep(PER_WORKER_DELAY_S)
                    return extract_photo_urls(resp.text)
                # 404 — restaurant page gone; don't retry
                if resp.status_code in (404, 410):
                    return []
                # 429 / 5xx — back off
                await asyncio.sleep(RETRY_BACKOFF_S * attempt)
            except (httpx.RequestError, httpx.TimeoutException) as e:
                if attempt == MAX_RETRIES:
                    print(f"  ! give up after {MAX_RETRIES} tries: {url} ({e})")
                    return []
                await asyncio.sleep(RETRY_BACKOFF_S * attempt)
        return []


def load_rows() -> tuple[list[dict], list[str]]:
    with TABELOG_CSV.open(encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        rows = list(r)
        fieldnames = list(r.fieldnames or [])
    for fld in PHOTO_FIELDS:
        if fld not in fieldnames:
            fieldnames.append(fld)
    return rows, fieldnames


def write_rows(rows: list[dict], fieldnames: list[str]) -> None:
    tmp = TABELOG_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})
    tmp.replace(TABELOG_CSV)


def needs_fetch(row: dict, force: bool) -> bool:
    if force:
        return True
    return not (row.get("photo1_url") or "").strip()


async def main_async(limit: int | None, force: bool) -> None:
    rows, fieldnames = load_rows()
    targets = [(i, r) for i, r in enumerate(rows) if r.get("detail_url") and needs_fetch(r, force)]
    if limit is not None:
        targets = targets[:limit]
    if not targets:
        print("nothing to fetch — every row already has photos (use --force to redo)")
        return

    print(f"fetching photos for {len(targets)} of {len(rows)} rows "
          f"(concurrency={CONCURRENCY}, ~{PER_WORKER_DELAY_S}s per worker)")

    sem = asyncio.Semaphore(CONCURRENCY)
    done = 0
    hits = 0
    misses = 0

    async with httpx.AsyncClient(
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT_S,
        follow_redirects=True,
        http2=False,
    ) as client:
        tasks_list = [
            (asyncio.create_task(fetch_one(client, r["detail_url"], sem)), i, r)
            for i, r in targets
        ]
        pending = {t for t, _, _ in tasks_list}
        meta = {t: (i, r) for t, i, r in tasks_list}

        while pending:
            finished, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for t in finished:
                i, r = meta[t]
                urls = t.result()
                if urls:
                    for slot, fld in enumerate(PHOTO_FIELDS):
                        r[fld] = urls[slot] if slot < len(urls) else ""
                    hits += 1
                else:
                    misses += 1
                done += 1
                print(f"  [{done}/{len(targets)}] {r.get('name','?')[:30]:30s}  "
                      f"+{len(urls)} photos")
                if done % CHECKPOINT_EVERY == 0:
                    write_rows(rows, fieldnames)
                    print(f"  -- checkpoint at {done}/{len(targets)} --")

    write_rows(rows, fieldnames)
    print(f"\ndone: {hits} with photos, {misses} empty, written to {TABELOG_CSV}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="only fetch this many rows (smoke test)")
    ap.add_argument("--force", action="store_true",
                    help="re-fetch even if photo1_url is already populated")
    args = ap.parse_args()
    asyncio.run(main_async(args.limit, args.force))


if __name__ == "__main__":
    main()
