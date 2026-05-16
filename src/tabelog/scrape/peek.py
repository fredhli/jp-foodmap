"""Quick one-off: attach to running Chrome on 9223 and print current page state."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from playwright.async_api import async_playwright
from tabelog.browser import get_or_spawn_chrome, get_or_open_page


async def main() -> None:
    async with async_playwright() as p:
        browser = await get_or_spawn_chrome(p)
        ctx = browser.contexts[0]
        print(f"\nContexts: {len(browser.contexts)}, pages: {len(ctx.pages)}")
        for i, pg in enumerate(ctx.pages):
            try:
                title = await pg.title()
            except Exception:
                title = "<unreadable>"
            print(f"  [{i}] {pg.url!r}  —  {title!r}")


if __name__ == "__main__":
    asyncio.run(main())
