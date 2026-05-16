"""
Open omakase.in in a detached Chrome (CDP port 9223) and wait while you
log in manually. Session cookies persist in omakase/.chrome_profile/,
so later scripts can attach and reuse the logged-in browser.

Run: uv run python src/tabelog/omakase/01_launch.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from playwright.async_api import async_playwright

from tabelog.browser import get_or_spawn_chrome, get_or_open_page

START_URL = "https://omakase.in/"


async def main() -> None:
    async with async_playwright() as p:
        browser = await get_or_spawn_chrome(p)
        page = await get_or_open_page(browser)
        await page.goto(START_URL, wait_until="domcontentloaded")
        print(f"\nNavigated to {START_URL}")
        print("\n>>> Log in manually in the Chrome window that just opened.")
        print(">>> Then navigate to the Osaka (and/or Kobe) restaurant list page.")
        print(">>> Tell Claude the URL(s) you land on so the scraper can target them.\n")
        print("Chrome will stay running after this script exits — don't close it.")


if __name__ == "__main__":
    asyncio.run(main())
