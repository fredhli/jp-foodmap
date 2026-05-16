"""
Probe Tabelog DOM for one omakase restaurant.

Drives the already-running Chrome session (CDP 9223) to:
  1. Open Tabelog search for the first restaurant name in bookable.csv
  2. Dump the top 5 result cards
  3. Click the first result and dump rating + dinner-price DOM

Outputs to data/intermediate/:
    tabelog_search.html      search-results page HTML
    tabelog_search.txt       top-5 result summary
    tabelog_resto.html       restaurant page HTML
    tabelog_resto.txt        rating / dinner-price extraction

If a Tabelog captcha or login wall appears, the script just dumps what's
visible; you can solve it manually in the Chrome window and re-run.
"""

import asyncio
import csv
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from playwright.async_api import async_playwright

from tabelog.browser import get_or_spawn_chrome, get_or_open_page
from tabelog.paths import BOOKABLE_CSV, INTERMEDIATE_DIR

OUT_DIR = INTERMEDIATE_DIR

SEARCH_RESULTS_JS = r"""
() => {
    // Tabelog result cards usually have class js-rst-cassette-wrap / list-rst.
    // Fall back to any anchor pointing to /<region>/A.../A.../<id>/ if those
    // class names have changed.
    const cards = Array.from(document.querySelectorAll(
        '.list-rst, .js-rst-cassette-wrap, [data-cassette-id]'
    ));
    if (cards.length === 0) {
        // fallback: collect anchors with href matching restaurant-page pattern
        const anchors = Array.from(document.querySelectorAll('a[href*="/A"]'));
        const uniq = new Map();
        for (const a of anchors) {
            const href = a.href;
            if (!/^https:\/\/tabelog\.com\/[^/]+\/A\d+\/A\d+\/\d+\/?$/.test(href)) continue;
            if (uniq.has(href)) continue;
            uniq.set(href, {
                href,
                text: (a.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80),
                outer: a.outerHTML.slice(0, 200),
            });
            if (uniq.size >= 5) break;
        }
        return { mode: 'fallback', cards: Array.from(uniq.values()) };
    }
    return {
        mode: 'cassette',
        cards: cards.slice(0, 5).map(c => {
            const a = c.querySelector('a[href*="/A"]') || c.querySelector('a');
            return {
                href: a ? a.href : null,
                text: (c.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 160),
                outer_class: c.className,
            };
        }),
    };
}
"""

RESTO_EXTRACT_JS = r"""
() => {
    // Try several known rating selectors and the budget block.
    const tryText = sels => {
        for (const sel of sels) {
            const el = document.querySelector(sel);
            if (el) return { sel, text: (el.innerText || '').trim() };
        }
        return null;
    };

    const rating = tryText([
        '.rdheader-rating__score-val-dtl',
        '.rdheader-rating__score-val',
        '[class*="rating__score-val"]',
        'b.c-rating__val',
    ]);

    // Budget section: dinner uses dinner glyph; lunch uses lunch glyph.
    const findBudget = glyph => {
        const ems = document.querySelectorAll(`em.${glyph}, em[class*="${glyph}"]`);
        for (const em of ems) {
            const row = em.closest('p, li, dd, div, span') || em.parentElement;
            if (!row) continue;
            return (row.innerText || '').replace(/\s+/g, ' ').trim();
        }
        return null;
    };
    const dinner = findBudget('gly-b-dinner');
    const lunch  = findBudget('gly-b-lunch');

    // Address as a sanity check that we landed on the right page.
    const addrEl = document.querySelector(
        '.rstinfo-table__address, .rdheader-subinfo__item--address, [class*="rstinfo"][class*="address"]'
    );

    // Title
    const titleEl = document.querySelector(
        'h2.display-name, .rdheader-rstname-wrap h2, h2[class*="rstname"], h2'
    );

    return {
        url: location.href,
        title: (document.title || '').slice(0, 120),
        rest_title: titleEl ? (titleEl.innerText || '').trim().slice(0, 80) : null,
        rating,
        dinner,
        lunch,
        address: addrEl ? (addrEl.innerText || '').trim().slice(0, 200) : null,
    };
}
"""


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    bookable_csv = BOOKABLE_CSV
    if not bookable_csv.exists():
        sys.exit(f"Not found: {bookable_csv}")

    with bookable_csv.open(encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r["bookable"] == "yes"]
    if not rows:
        sys.exit("No bookable=yes rows in bookable.csv")
    sample = rows[0]
    name = sample["name"]
    print(f"Sample restaurant: {name!r}  ({sample['slug']})")

    query = urllib.parse.quote(name)
    search_url = f"https://tabelog.com/rstLst/?sw={query}"
    print(f"Search URL: {search_url}")

    async with async_playwright() as p:
        browser = await get_or_spawn_chrome(p)
        page = await get_or_open_page(browser)

        # -- Step 1: search results --
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        html = await page.content()
        (OUT_DIR / "tabelog_search.html").write_text(html, encoding="utf-8")

        results = await page.evaluate(SEARCH_RESULTS_JS)
        lines = [
            f"Query: {name!r}",
            f"URL:   {page.url}",
            f"Title: {await page.title()}",
            f"HTML:  {len(html):,} chars",
            f"Mode:  {results['mode']}",
            f"Cards: {len(results['cards'])}",
            "",
        ]
        for i, c in enumerate(results["cards"]):
            lines.append(f"[{i}] {c.get('href')!r}")
            lines.append(f"    text: {c.get('text','')!r}")
            if c.get("outer_class"):
                lines.append(f"    class: {c['outer_class'][:120]}")
            lines.append("")
        (OUT_DIR / "tabelog_search.txt").write_text("\n".join(lines), encoding="utf-8")
        print("\n".join(lines))

        if not results["cards"]:
            print("\n[!!] No result cards detected — Tabelog might have served a captcha")
            print(f"     or login wall. Check the Chrome window. HTML dumped at:")
            print(f"     {OUT_DIR / 'tabelog_search.html'}")
            return

        first_href = results["cards"][0].get("href")
        if not first_href:
            print("\n[!!] First card has no href.")
            return

        # -- Step 2: restaurant page --
        print(f"\nNavigating to first result: {first_href}")
        await page.goto(first_href, wait_until="domcontentloaded", timeout=30_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        html2 = await page.content()
        (OUT_DIR / "tabelog_resto.html").write_text(html2, encoding="utf-8")

        info = await page.evaluate(RESTO_EXTRACT_JS)
        out = [
            f"URL:        {info['url']}",
            f"Page title: {info['title']!r}",
            f"Rst title:  {info['rest_title']!r}",
            f"Rating:     {info['rating']!r}",
            f"Dinner:     {info['dinner']!r}",
            f"Lunch:      {info['lunch']!r}",
            f"Address:    {info['address']!r}",
        ]
        (OUT_DIR / "tabelog_resto.txt").write_text("\n".join(out), encoding="utf-8")
        print("\n" + "\n".join(out))


if __name__ == "__main__":
    asyncio.run(main())
