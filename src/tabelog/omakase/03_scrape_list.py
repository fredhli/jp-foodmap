"""
Scrape the full Kansai restaurant list from omakase.in, paginating until
no new restaurants appear, then filter to 大阪府 / 兵庫県.

Output: data/omakase/restaurants.csv with columns
    slug, name, cuisine, prefecture, detail_url

Each restaurant card on the list page is an <a href="/r/<slug>"> whose
innerText reads "<name>\\n<cuisine> / <prefecture>". We anchor on those
anchors directly (robust against card-wrapper variations).
"""

import asyncio
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from playwright.async_api import async_playwright

from tabelog.browser import get_or_spawn_chrome, get_or_open_page
from tabelog.paths import RESTAURANTS_CSV, RESTAURANTS_ALL_CSV

BASE = "https://omakase.in"
PAGE1_URL = f"{BASE}/r?area=kansai&cuisine=&search_keywords=&commit=%E6%A4%9C%E7%B4%A2"


def page_url(n: int) -> str:
    if n == 1:
        return PAGE1_URL
    return f"{BASE}/r/page/{n}?area=kansai&cuisine=&search_keywords=&commit=%E6%A4%9C%E7%B4%A2"


# /r/<slug> where slug is alnum (excludes /r/page/N)
_SLUG_RE = re.compile(r"^/r/([A-Za-z0-9_-]+)$")


EXTRACT_JS = """
() => {
    const out = [];
    const anchors = document.querySelectorAll('a[href]');
    for (const a of anchors) {
        const href = a.getAttribute('href') || '';
        const m = href.match(/^\\/r\\/([A-Za-z0-9_-]+)$/);
        if (!m) continue;
        const slug = m[1];
        const txt = (a.innerText || '').replace(/\\u00A0/g, ' ').trim();
        if (!txt) continue;
        out.push({ slug, href, text: txt });
    }
    return out;
}
"""


_PREF_SUFFIX = ("府", "県", "都", "道")


def parse_card(text: str) -> tuple[str, str, str]:
    """
    Card innerText looks like:
        "<name>\\n<cuisine> / <prefecture>"

    A handful of cards omit the "/ <prefecture>" tail (events / new openings).
    Detect prefecture by suffix: only take the post-slash segment as prefecture
    when it ends in 府/県/都/道 — otherwise leave prefecture blank.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "", "", ""
    classifier = lines[-1].replace("／", "/")
    name_lines = lines[:-1]
    badges = {"新規OPEN", "店頭受取"}
    while name_lines and name_lines[0] in badges:
        name_lines.pop(0)
    name = " ".join(name_lines).strip()

    if "/" in classifier:
        cuisine, last = classifier.rsplit("/", 1)
        last = last.strip()
        if last.endswith(_PREF_SUFFIX):
            return name, cuisine.strip(), last
        return name, classifier.strip(), ""
    return name, classifier.strip(), ""


async def scrape_all_pages(page) -> list[dict]:
    """Iterate pages 1, 2, 3, ... until no new slugs appear. Returns deduped list."""
    seen: dict[str, dict] = {}
    n = 1
    while True:
        url = page_url(n)
        print(f"[page {n}] {url}")
        await page.goto(url, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        rows = await page.evaluate(EXTRACT_JS)
        new_count = 0
        for r in rows:
            slug = r["slug"]
            if slug in seen:
                continue
            name, cuisine, prefecture = parse_card(r["text"])
            seen[slug] = {
                "slug": slug,
                "name": name,
                "cuisine": cuisine,
                "prefecture": prefecture,
                "detail_url": BASE + r["href"],
            }
            new_count += 1
        print(f"           {len(rows)} card-anchors, {new_count} new, total {len(seen)}")

        if new_count == 0:
            print(f"           no new restaurants — stopping after page {n}")
            break
        n += 1
        if n > 50:
            print("           hard cap of 50 pages reached")
            break

    return list(seen.values())


async def main() -> None:
    async with async_playwright() as p:
        browser = await get_or_spawn_chrome(p)
        page = await get_or_open_page(browser)
        all_restaurants = await scrape_all_pages(page)

    target_prefs = {"大阪府", "兵庫県"}
    target = [r for r in all_restaurants if r["prefecture"] in target_prefs]
    other  = [r for r in all_restaurants if r["prefecture"] not in target_prefs]
    unknown = [r for r in all_restaurants if not r["prefecture"]]

    print(f"\nTotal scraped: {len(all_restaurants)}")
    by_pref: dict[str, int] = {}
    for r in all_restaurants:
        by_pref[r["prefecture"]] = by_pref.get(r["prefecture"], 0) + 1
    for pref, n in sorted(by_pref.items(), key=lambda x: -x[1]):
        marker = "*" if pref in target_prefs else " "
        label = pref if pref else "(no prefecture in card)"
        print(f"  {marker} {label}: {n}")

    print(f"\nTarget (大阪府 + 兵庫県): {len(target)}")
    print(f"Other (filtered out)  : {len(other)}")

    if unknown:
        print(f"\n!! {len(unknown)} restaurants have NO prefecture in the list card:")
        for r in unknown:
            print(f"   {r['detail_url']}  -- {r['name']!r} ({r['cuisine']!r})")
        print("   These are NOT in restaurants.csv. Add manually if any are Osaka/Hyogo.")

    fields = ["slug", "name", "cuisine", "prefecture", "detail_url"]

    RESTAURANTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with RESTAURANTS_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in target:
            w.writerow(r)
    print(f"\nWrote {RESTAURANTS_CSV}  ({len(target)} rows, Osaka + Hyogo)")

    with RESTAURANTS_ALL_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in all_restaurants:
            w.writerow(r)
    print(f"Wrote {RESTAURANTS_ALL_CSV}  ({len(all_restaurants)} rows, all Kansai)")


if __name__ == "__main__":
    asyncio.run(main())
