"""Probe whatever tabelog page is currently open in the attached Chrome."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from playwright.async_api import async_playwright
from tabelog.browser import get_or_spawn_chrome
from tabelog.paths import INTERMEDIATE_DIR

OUT_DIR = INTERMEDIATE_DIR
OUT_DIR.mkdir(parents=True, exist_ok=True)


async def main() -> None:
    async with async_playwright() as p:
        browser = await get_or_spawn_chrome(p)
        ctx = browser.contexts[0]
        page = next(
            (pg for pg in ctx.pages if "tabelog.com" in (pg.url or "")),
            ctx.pages[0],
        )
        await page.bring_to_front()

        url = page.url
        title = await page.title()
        html = await page.content()
        (OUT_DIR / "tabelog_list.html").write_text(html, encoding="utf-8")

        info = await page.evaluate(
            r"""
            () => {
                const out = {};
                out.h1 = (document.querySelector('h1')?.innerText || '').trim();
                const countEls = Array.from(document.querySelectorAll(
                    '[class*="count"], [class*="result"], [class*="search-header"], .list-condition'
                )).slice(0, 25);
                out.counts = countEls.map(e => ({
                    cls: e.className,
                    text: (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 150),
                })).filter(x => x.text);

                const anchors = Array.from(document.querySelectorAll('a[href*="/osaka/"]'));
                const seen = new Set();
                const cards = [];
                for (const a of anchors) {
                    const href = a.getAttribute('href') || '';
                    if (!/\/osaka\/A\d+\/A\d+\/\d+\/?$/.test(href)) continue;
                    if (seen.has(href)) continue;
                    seen.add(href);
                    let card = a;
                    for (let i = 0; i < 8 && card.parentElement; i++) {
                        card = card.parentElement;
                        if ((card.innerText || '').length > 80) break;
                    }
                    cards.push({
                        href,
                        link_text: (a.innerText || '').trim().slice(0, 80),
                        card_class: card.className,
                        card_text: (card.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 700),
                    });
                    if (cards.length >= 5) break;
                }
                out.card_count_estimate = seen.size;
                out.cards = cards;

                const pag = Array.from(document.querySelectorAll('a, button')).filter(a => {
                    const t = (a.innerText || '').trim();
                    return /^(次|次の|»|>|\d+)$/.test(t);
                }).slice(0, 20).map(a => ({
                    tag: a.tagName,
                    text: (a.innerText || '').trim(),
                    href: a.getAttribute('href') || null,
                }));
                out.pagination = pag;
                return out;
            }
            """
        )

        lines = [
            f"URL:   {url}",
            f"Title: {title}",
            f"H1:    {info['h1']}",
            f"HTML size: {len(html):,} chars",
            "",
            f"Detected restaurant card links on page: {info['card_count_estimate']}",
            "",
            "-- count / result-header elements --",
        ]
        for c in info["counts"]:
            lines.append(f"  .{c['cls'][:60]!r}: {c['text']!r}")
        lines.append("")
        lines.append("-- first 5 cards --")
        for c in info["cards"]:
            lines.append(f"  href: {c['href']}")
            lines.append(f"    link_text: {c['link_text']!r}")
            lines.append(f"    card_class: {c['card_class']!r}")
            lines.append(f"    card_text: {c['card_text']!r}")
            lines.append("")
        lines.append("-- pagination --")
        for p_ in info["pagination"]:
            lines.append(f"  {p_}")

        out_txt = OUT_DIR / "tabelog_list.txt"
        out_txt.write_text("\n".join(lines), encoding="utf-8")
        print("\n".join(lines))
        print(f"\nWrote {out_txt} and tabelog_list.html")


if __name__ == "__main__":
    asyncio.run(main())
