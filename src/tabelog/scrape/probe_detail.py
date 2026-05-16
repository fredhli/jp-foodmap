"""Probe the currently-open Tabelog detail page: dump structure, find 席数 / 住所."""

import asyncio
import re
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
            (pg for pg in ctx.pages
             if "tabelog.com" in (pg.url or "") and re.search(r"/\d{8}/?", pg.url or "")),
            None,
        )
        if page is None:
            print("No tabelog detail-style tab found. Open one first.")
            return
        await page.bring_to_front()

        url = page.url
        title = await page.title()
        html = await page.content()
        # name file by rst id
        m = re.search(r"/(\d{8})/?", url)
        rid = m.group(1) if m else "unknown"
        (OUT_DIR / f"tabelog_detail_{rid}.html").write_text(html, encoding="utf-8")

        # Find rows in tables that contain key labels (席数 / 住所 / 予算 etc.)
        info = await page.evaluate(
            r"""
            () => {
                const wanted = ['席数', '住所', '個室', '貸切', 'お席のみ予約',
                                '予算', '営業時間', '定休日', 'ジャンル', '電話番号'];
                const out = {};
                // Approach 1: scan all <th>/<td> pairs in tables
                const rows = [];
                for (const tr of document.querySelectorAll('tr')) {
                    const ths = tr.querySelectorAll('th');
                    const tds = tr.querySelectorAll('td');
                    if (!ths.length || !tds.length) continue;
                    const label = (ths[0].innerText || '').replace(/\s+/g, ' ').trim();
                    const value = (tds[0].innerText || '').replace(/\s+/g, ' ').trim();
                    if (label) rows.push({label, value: value.slice(0, 300),
                                           th_cls: ths[0].className,
                                           td_cls: tds[0].className});
                }
                out.table_rows = rows.slice(0, 80);

                // Approach 2: search for the exact wanted labels anywhere
                const hits = {};
                for (const w of wanted) {
                    hits[w] = [];
                    // find any element whose text starts with the label
                    const all = document.querySelectorAll('th, dt, p, span, div');
                    for (const el of all) {
                        const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
                        if (t === w || t.startsWith(w + ' ') || t.startsWith(w + ':') || t.startsWith(w + '：')) {
                            // grab nearest sibling td/dd or parent text
                            let v = '';
                            const sib = el.nextElementSibling;
                            if (sib) v = (sib.innerText || '').replace(/\s+/g, ' ').trim();
                            hits[w].push({
                                tag: el.tagName,
                                cls: el.className,
                                text: t.slice(0, 100),
                                next_sib_value: v.slice(0, 200),
                            });
                            if (hits[w].length >= 3) break;
                        }
                    }
                }
                out.label_hits = hits;

                // Approach 3: look for booking-related anchors/buttons
                const bookHints = [];
                for (const el of document.querySelectorAll('a, button')) {
                    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
                    if (!t) continue;
                    if (/予約|席のみ予約|ネット予約|空席確認|空席を探す|予約する/.test(t)) {
                        bookHints.push({
                            tag: el.tagName,
                            text: t.slice(0, 60),
                            href: el.getAttribute('href') || null,
                            cls: (el.className || '').slice(0, 100),
                            disabled: el.disabled || false,
                        });
                        if (bookHints.length >= 30) break;
                    }
                }
                out.booking_hints = bookHints;

                // Approach 4: look for telltale "online reservation unavailable"-style text
                const bodyText = document.body.innerText || '';
                const flags = {};
                for (const phrase of ['ネット予約', '席のみ予約', '空席を探す',
                                       '予約不可', 'インターネット予約',
                                       'TEL', '電話のみ', '電話で予約']) {
                    flags[phrase] = bodyText.includes(phrase);
                }
                out.body_flags = flags;
                return out;
            }
            """
        )

        lines = [
            f"URL:   {url}",
            f"Title: {title}",
            f"HTML size: {len(html):,}",
            "",
            "-- table rows (label / value) --",
        ]
        for r in info["table_rows"]:
            lines.append(f"  [{r['label']!r}]  th.{r['th_cls'][:30]} | td.{r['td_cls'][:30]}")
            lines.append(f"     -> {r['value']!r}")
        lines.append("")
        lines.append("-- explicit label hits --")
        for k, hs in info["label_hits"].items():
            if not hs:
                lines.append(f"  {k}: (no hit)")
                continue
            for h in hs:
                lines.append(f"  {k}: <{h['tag']}.{h['cls'][:40]}> text={h['text']!r}")
                lines.append(f"     next_sib -> {h['next_sib_value']!r}")
        lines.append("")
        lines.append("-- booking-related anchors/buttons --")
        for b in info["booking_hints"]:
            lines.append(f"  <{b['tag']}> {b['text']!r}  href={b['href']}  disabled={b['disabled']}")
            lines.append(f"     cls={b['cls']!r}")
        lines.append("")
        lines.append("-- body text flags --")
        for k, v in info["body_flags"].items():
            lines.append(f"  {k}: {v}")

        out_txt = OUT_DIR / f"tabelog_detail_{rid}.txt"
        out_txt.write_text("\n".join(lines), encoding="utf-8")
        print("\n".join(lines))
        print(f"\nWrote {out_txt} and tabelog_detail_{rid}.html")


if __name__ == "__main__":
    asyncio.run(main())
