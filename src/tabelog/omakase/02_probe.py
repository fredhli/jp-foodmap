"""
Attach to the running Chrome on CDP 9223 and dump enough of the current page
DOM to design the restaurant-list scraper. Writes:
  data/intermediate/list_page.html  -- full HTML of current page
  data/intermediate/probe.txt       -- URL, title, anchor/card summary

Run AFTER you've logged in and navigated to:
  https://omakase.in/r?area=kansai
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from playwright.async_api import async_playwright

from tabelog.browser import get_or_spawn_chrome, get_or_open_page
from tabelog.paths import INTERMEDIATE_DIR

OUT_DIR = INTERMEDIATE_DIR


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await get_or_spawn_chrome(p)
        page = await get_or_open_page(browser)

        url = page.url
        title = await page.title()
        html = await page.content()
        (OUT_DIR / "list_page.html").write_text(html, encoding="utf-8")

        # Pull a structured sample: anchor texts pointing to /r/<slug>,
        # plus any text near them (helps identify card boundaries).
        sample = await page.evaluate(
            """
            () => {
                const anchors = Array.from(document.querySelectorAll('a[href]'));
                const restoLinks = anchors.filter(a => /\\/r\\/[^?#]+/.test(a.getAttribute('href') || ''));
                const uniq = new Map();
                for (const a of restoLinks) {
                    const href = a.getAttribute('href');
                    if (uniq.has(href)) continue;
                    // climb to a card-like ancestor with enough text
                    let card = a;
                    for (let i = 0; i < 6 && card.parentElement; i++) {
                        card = card.parentElement;
                        if ((card.innerText || '').length > 60) break;
                    }
                    uniq.set(href, {
                        href,
                        link_text: (a.innerText || '').trim().slice(0, 80),
                        card_text: (card.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 400),
                        card_tag: card.tagName,
                        card_class: card.className,
                    });
                }
                return {
                    total_links: anchors.length,
                    resto_link_count: restoLinks.length,
                    unique_resto: uniq.size,
                    samples: Array.from(uniq.values()).slice(0, 5),
                };
            }
            """
        )

        # Pagination signals: look for "次へ" / "Next" / page numbers, plus rel=next
        pagination = await page.evaluate(
            """
            () => {
                const out = [];
                for (const a of document.querySelectorAll('a, button')) {
                    const t = (a.innerText || '').trim();
                    if (!t) continue;
                    if (/^(次|Next|»|>|\\d+)$/i.test(t) || a.rel === 'next') {
                        out.push({
                            tag: a.tagName,
                            text: t.slice(0, 30),
                            href: a.getAttribute('href') || null,
                            rel: a.rel || null,
                            cls: a.className || '',
                        });
                    }
                    if (out.length >= 30) break;
                }
                return out;
            }
            """
        )

        # Quick prefecture text scan: how does "大阪府" / "兵庫県" appear?
        pref_hits = await page.evaluate(
            """
            () => {
                const text = document.body.innerText || '';
                return {
                    osaka_count: (text.match(/大阪府/g) || []).length,
                    hyogo_count: (text.match(/兵庫県/g) || []).length,
                    kyoto_count: (text.match(/京都府/g) || []).length,
                };
            }
            """
        )

        lines = [
            f"URL:   {url}",
            f"Title: {title}",
            f"HTML size: {len(html):,} chars",
            "",
            f"Total <a>: {sample['total_links']}",
            f"Restaurant /r/<slug> links: {sample['resto_link_count']} ({sample['unique_resto']} unique)",
            "",
            "-- prefecture mentions on this page --",
            f"  大阪府: {pref_hits['osaka_count']}",
            f"  兵庫県: {pref_hits['hyogo_count']}",
            f"  京都府: {pref_hits['kyoto_count']}",
            "",
            "-- first 5 restaurant cards --",
        ]
        for s in sample["samples"]:
            lines.append(f"  href: {s['href']}")
            lines.append(f"    link_text: {s['link_text']!r}")
            lines.append(f"    card_tag.{s['card_class'][:60]!r}")
            lines.append(f"    card_text: {s['card_text']!r}")
            lines.append("")

        lines.append("-- pagination / next signals --")
        for p_ in pagination:
            lines.append(f"  {p_}")

        (OUT_DIR / "probe.txt").write_text("\n".join(lines), encoding="utf-8")
        print("\n".join(lines))
        print(f"\nWrote {OUT_DIR / 'probe.txt'} and {OUT_DIR / 'list_page.html'}")


if __name__ == "__main__":
    asyncio.run(main())
