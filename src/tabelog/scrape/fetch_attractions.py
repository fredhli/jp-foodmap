"""Fill in lon/lat for tourist-attraction rows in data/attractions.csv.

The CSV has columns: name_jp, name_cn, emoji, lon, lat. Rows where lon
or lat is blank trigger a Nominatim lookup using name_jp as the query,
and the resolved coords are written back into the CSV. Re-runs only
query rows still missing coords; --refresh re-queries every row.

The CSV is saved after each successful hit, so the script is safe to
interrupt. Nominatim's policy asks for ≤1 req/s and a real User-Agent;
both are honored. The Japan filter drops any hit whose display_name
doesn't contain '日本' / 'Japan' as a guard against same-name places
elsewhere (e.g. a '大阪城' in Xinjiang sneaking into the top slot).

Usage:
  uv run --python 3.13 python src/tabelog/scrape/fetch_attractions.py
  uv run --python 3.13 python src/tabelog/scrape/fetch_attractions.py --refresh
"""

import argparse
import csv
import sys
import time
from pathlib import Path

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from tabelog.paths import ATTRACTIONS_CSV

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "tabelog-map-build/0.1 (https://github.com/fredhli/jp-foodmap)"
RATE_LIMIT_S = 1.1
JAPAN_TOKENS = ("日本", "Japan")
CSV_HEADERS = ["name_jp", "name_cn", "emoji", "lon", "lat"]


def read_csv() -> list[dict]:
    with ATTRACTIONS_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(rows: list[dict]) -> None:
    with ATTRACTIONS_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADERS, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_HEADERS})


def pick_japan_hit(results: list[dict]) -> dict | None:
    """First result whose display_name names Japan. Nominatim usually
    ranks the Japanese match first, but occasionally a same-named place
    in China/Korea sneaks into the top slot — the filter is cheap
    insurance."""
    for hit in results:
        if any(t in hit.get("display_name", "") for t in JAPAN_TOKENS):
            return hit
    return None


def nominatim_lookup(query: str, client: httpx.Client) -> dict | None:
    r = client.get(
        NOMINATIM_URL,
        params={
            "q": query,
            "format": "json",
            "limit": 5,
            "accept-language": "ja,zh-CN,en",
        },
    )
    r.raise_for_status()
    return pick_japan_hit(r.json())


def needs_lookup(row: dict, refresh: bool) -> bool:
    if refresh:
        return True
    return not row.get("lon", "").strip() or not row.get("lat", "").strip()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--refresh", action="store_true",
        help="re-query every row, overwriting existing coords",
    )
    args = ap.parse_args()

    rows = read_csv()
    todo = [(i, r) for i, r in enumerate(rows) if needs_lookup(r, args.refresh)]
    print(f"{len(todo)}/{len(rows)} rows need a Nominatim lookup.")
    if not todo:
        return

    headers = {"User-Agent": USER_AGENT}
    hits = misses = 0
    with httpx.Client(timeout=15, headers=headers) as client:
        for n, (i, row) in enumerate(todo, 1):
            q = row.get("name_jp", "").strip()
            if not q:
                print(f"  [{n:>2}/{len(todo)}] SKIP   row {i}: empty name_jp",
                      file=sys.stderr)
                continue
            try:
                raw = nominatim_lookup(q, client)
            except Exception as e:
                print(f"  [{n:>2}/{len(todo)}] ERROR  {q!r}: {e}",
                      file=sys.stderr)
                misses += 1
                time.sleep(RATE_LIMIT_S)
                continue
            if raw is None:
                print(f"  [{n:>2}/{len(todo)}] MISS   {q!r}: no Japan-tagged result",
                      file=sys.stderr)
                misses += 1
                time.sleep(RATE_LIMIT_S)
                continue
            lat = float(raw["lat"])
            lon = float(raw["lon"])
            row["lat"] = f"{lat:.6f}"
            row["lon"] = f"{lon:.6f}"
            write_csv(rows)  # persist after each hit so Ctrl-C is safe
            hits += 1
            print(f"  [{n:>2}/{len(todo)}] HIT    {q!r:35s} -> "
                  f"({lat:.6f}, {lon:.6f})  {raw.get('display_name', '')}")
            time.sleep(RATE_LIMIT_S)

    print()
    print(f"{hits} resolved, {misses} unresolved.")


if __name__ == "__main__":
    main()
