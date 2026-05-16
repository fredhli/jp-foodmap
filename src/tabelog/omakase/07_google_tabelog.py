"""
Walk bookable.csv. For each row, open
    https://www.google.com/search?q=<name> tabelog
in the already-running Chrome (CDP 9223) and pause for Enter before moving on.

Type 'q' + Enter to quit early, 's' + Enter to skip back / forward — anything
else just advances.
"""

import asyncio
import csv
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from playwright.async_api import async_playwright

from tabelog.browser import get_or_spawn_chrome, get_or_open_page
from tabelog.paths import BOOKABLE_CSV

CSV_PATH = BOOKABLE_CSV


def google_url(name: str, prefecture: str) -> str:
    parts = [name]
    if prefecture:
        parts.append(prefecture)
    parts.append("tabelog")
    q = urllib.parse.quote_plus(" ".join(parts))
    return f"https://www.google.com/search?q={q}"


async def main() -> None:
    if not CSV_PATH.exists():
        sys.exit(f"Not found: {CSV_PATH}")
    with CSV_PATH.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("bookable.csv is empty")

    print(f"{len(rows)} restaurants to walk through.")
    print("Press Enter to advance, 'q' to quit.\n")

    async with async_playwright() as p:
        browser = await get_or_spawn_chrome(p)
        page = await get_or_open_page(browser)

        for i, row in enumerate(rows, 1):
            name = row.get("name", "").strip()
            prefecture = row.get("prefecture", "").strip()
            if not name:
                continue
            url = google_url(name, prefecture)
            print(f"[{i}/{len(rows)}] {name}")
            print(f"           {row.get('cuisine','')} / {row.get('prefecture','')}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                print(f"           [warn] goto failed: {e.__class__.__name__}: {e}")

            try:
                cmd = input("           Enter = next, 'q' = quit > ").strip().lower()
            except EOFError:
                break
            if cmd == "q":
                print("Quit.")
                break


if __name__ == "__main__":
    asyncio.run(main())
