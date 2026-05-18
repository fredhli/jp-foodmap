"""
One-shot pipeline: scrape a Tabelog region, geocode the new rows, and
regenerate docs/index.html.

  uv run python main.py osaka
  uv run python main.py hyogo --top-pct 0.5
  uv run python main.py okayama --hard-cap 200 --translate

Flags are forwarded verbatim to scrape_all (region, --top-pct, --hard-cap,
--translate / --no-translate). The geocode + render step always runs with
--fillempty so only the freshly-scraped rows hit GSI; existing rows keep
their cached lat/lon. Run src/tabelog/scrape/map.py directly if you need
a full re-geocode.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from tabelog.scrape import map as map_mod
from tabelog.scrape import scrape_all


async def _run(argv: list[str]) -> None:
    await scrape_all.main(argv)
    print("\n" + "=" * 60)
    print("Scrape done. Geocoding new rows and rebuilding map ...")
    print("=" * 60 + "\n")
    map_mod.main(["--fillempty"])


def main() -> None:
    asyncio.run(_run(sys.argv[1:]))


if __name__ == "__main__":
    main()
