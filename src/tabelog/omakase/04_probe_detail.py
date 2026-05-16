"""
Attach to current Chrome page (a restaurant detail page) and dump the DOM
around any 予約 / 予約する / Reserve button.

Usage: open ONE restaurant detail page in the Chrome window, then run:
    uv run python src/tabelog/omakase/04_probe_detail.py

Run it twice — once on a restaurant whose 予約 button is RED (bookable),
once on a GRAY (unbookable) one. The two outputs together let us pick the
attribute/class that distinguishes the two states.

Output:
    data/intermediate/detail_<slug>.html     full page HTML
    data/intermediate/detail_<slug>_buttons.txt   button summary
"""

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from playwright.async_api import async_playwright

from tabelog.browser import get_or_spawn_chrome, get_or_open_page
from tabelog.paths import INTERMEDIATE_DIR

OUT_DIR = INTERMEDIATE_DIR


BUTTON_JS = r"""
() => {
    // Collect any element whose innerText mentions 予約 / Reserve / 空席.
    const wanted = /予約|空席|Reserve|reservation/i;
    const seen = new Set();
    const results = [];

    function describe(el) {
        const cs = getComputedStyle(el);
        return {
            tag: el.tagName,
            classes: el.className && el.className.toString
                ? el.className.toString() : '',
            text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 120),
            disabled: el.disabled === true || el.getAttribute('disabled') !== null
                || el.getAttribute('aria-disabled') === 'true',
            href: el.tagName === 'A' ? (el.getAttribute('href') || null) : null,
            // visual state — useful to discriminate red vs gray
            color: cs.color,
            background: cs.backgroundColor,
            opacity: cs.opacity,
            pointer_events: cs.pointerEvents,
            cursor: cs.cursor,
            // structural identity
            id: el.id || null,
            role: el.getAttribute('role') || null,
            data_attrs: Array.from(el.attributes || [])
                .filter(a => a.name.startsWith('data-'))
                .map(a => `${a.name}=${a.value}`)
                .join(' '),
        };
    }

    // Buttons, anchors, and any element with role="button"
    const candidates = document.querySelectorAll(
        'button, a, [role="button"], input[type="button"], input[type="submit"]'
    );
    for (const el of candidates) {
        const txt = (el.innerText || el.value || '').trim();
        if (!txt) continue;
        if (!wanted.test(txt)) continue;
        if (seen.has(el)) continue;
        seen.add(el);
        results.push(describe(el));
    }

    // Also look for divs/spans containing 予約 that might be styled as a button
    if (results.length === 0) {
        for (const el of document.querySelectorAll('div, span, section')) {
            const txt = (el.innerText || '').trim();
            if (!wanted.test(txt)) continue;
            // skip if it's just a container that wraps many things
            if (txt.length > 50) continue;
            if (seen.has(el)) continue;
            seen.add(el);
            results.push(describe(el));
            if (results.length >= 10) break;
        }
    }

    return results;
}
"""


SLUG_RE = re.compile(r"/r/([A-Za-z0-9_-]+)")


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await get_or_spawn_chrome(p)
        page = await get_or_open_page(browser)
        url = page.url
        title = await page.title()
        m = SLUG_RE.search(url)
        slug = m.group(1) if m else "unknown"

        html = await page.content()
        html_path = OUT_DIR / f"detail_{slug}.html"
        html_path.write_text(html, encoding="utf-8")

        btns = await page.evaluate(BUTTON_JS)

        lines = [
            f"URL:   {url}",
            f"Title: {title}",
            f"Slug:  {slug}",
            f"HTML:  {len(html):,} chars  -> {html_path.name}",
            f"Buttons matching 予約/空席/Reserve: {len(btns)}",
            "",
        ]
        for i, b in enumerate(btns):
            lines.append(f"-- [{i}] {b['tag']}  text={b['text']!r}")
            lines.append(f"     classes : {b['classes']}")
            lines.append(f"     disabled: {b['disabled']}  pointer_events: {b['pointer_events']}  cursor: {b['cursor']}")
            lines.append(f"     color   : {b['color']}    bg: {b['background']}    opacity: {b['opacity']}")
            if b["href"]:
                lines.append(f"     href    : {b['href']}")
            if b["data_attrs"]:
                lines.append(f"     data    : {b['data_attrs']}")
            if b["id"] or b["role"]:
                lines.append(f"     id={b['id']!r}  role={b['role']!r}")
            lines.append("")

        out_path = OUT_DIR / f"detail_{slug}_buttons.txt"
        out_path.write_text("\n".join(lines), encoding="utf-8")
        print("\n".join(lines))
        print(f"\nWrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
