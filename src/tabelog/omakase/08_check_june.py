"""
Navigate to a restaurant's /reservations/new page, detect which month the
calendar opens on, then click left or right to land on the target month
(2026-06). Report clickable vs disabled dates for that month.
"""

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from playwright.async_api import async_playwright

from tabelog.browser import get_or_spawn_chrome, get_or_open_page
from tabelog.paths import INTERMEDIATE_DIR

URL = "https://omakase.in/r/jk392762/reservations/new"
TARGET_YEAR = 2026
TARGET_MONTH = 6
OUT_DIR = INTERMEDIATE_DIR

DUMP_JS = r"""
() => {
    // Day cells are <td><a class="">N</a></td> (clickable)
    //          or <td><a class="disabled">N</a></td> (gray).
    const anchors = Array.from(document.querySelectorAll('td > a'));
    const dayCells = [];
    for (const a of anchors) {
        const t = (a.innerText || '').trim();
        if (!/^\d{1,2}$/.test(t)) continue;
        const cls = (a.className || '').toString();
        const disabled = /\bdisabled\b/.test(cls);
        dayCells.push({ day: parseInt(t, 10), cls, disabled, clickable: !disabled });
    }

    // Month label: scan headers / short text matching "YYYY年M月".
    let monthLabel = null;
    const headers = document.querySelectorAll('th, h2, h3, .header, [class*="header"]');
    for (const el of headers) {
        const t = (el.innerText || '').trim();
        const m = t.match(/(20\d{2})\s*年\s*(\d{1,2})\s*月/);
        if (m && t.length < 40) { monthLabel = m[0]; break; }
    }
    if (!monthLabel) {
        for (const el of document.querySelectorAll('*')) {
            const t = (el.innerText || '').trim();
            const m = t.match(/^(20\d{2})\s*年\s*(\d{1,2})\s*月$/);
            if (m) { monthLabel = m[0]; break; }
        }
    }

    return { url: location.href, month_label: monthLabel, day_cells: dayCells };
}
"""

# direction: 'next' or 'prev'
CLICK_NAV_JS = r"""
(direction) => {
    const wantNext = direction === 'next';
    const iconRe = wantNext
        ? /fa-chevron-right|fa-angle-right/
        : /fa-chevron-left|fa-angle-left/;
    const ariaRe = wantNext ? /next|翌|次/ : /prev|previous|前/;
    const txtRe  = wantNext ? /^(>|»|›|❯|→)$|翌月|次の月|次月/
                            : /^(<|«|‹|❮|←)$|前月|先月/;

    const candidates = [];
    for (const el of document.querySelectorAll('a, button, [role="button"], i, span, div')) {
        const aria = (el.getAttribute('aria-label') || '').toLowerCase();
        const cls  = (el.className || '').toString().toLowerCase();
        const txt  = (el.innerText || '').trim();
        if (iconRe.test(cls) || ariaRe.test(aria) || txtRe.test(txt)) {
            const r = el.getBoundingClientRect();
            if (r.width < 4 || r.height < 4) continue;
            candidates.push({ el, cls, aria, txt });
        }
    }
    if (!candidates.length) return { clicked: false };

    const findClickable = el => {
        let node = el;
        for (let i = 0; i < 6 && node; i++) {
            if (node.tagName === 'A' || node.tagName === 'BUTTON') return node;
            if (node.getAttribute && node.getAttribute('role') === 'button') return node;
            if (node.onclick) return node;
            const cs = getComputedStyle(node);
            if (cs.cursor === 'pointer') return node;
            node = node.parentElement;
        }
        return el;
    };

    candidates.sort((a, b) => {
        const score = c => (iconRe.test(c.cls) ? 0 :
                            ariaRe.test(c.aria) ? 1 : 2);
        return score(a) - score(b);
    });

    const target = findClickable(candidates[0].el);
    target.click();
    return { clicked: true, target_cls: (target.className || '').toString().slice(0, 80) };
}
"""

_MONTH_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月")


def parse_month(label: str | None) -> tuple[int, int] | None:
    if not label:
        return None
    m = _MONTH_RE.search(label)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def summarize(dump: dict, label: str) -> str:
    cells = dump["day_cells"]
    clickable = [c for c in cells if c["clickable"]]
    disabled  = [c for c in cells if c["disabled"]]
    lines = [
        f"== {label} ==",
        f"  URL:         {dump['url']}",
        f"  Month label: {dump['month_label']!r}",
        f"  Day cells:   {len(cells)}  (clickable {len(clickable)}, disabled {len(disabled)})",
    ]
    if clickable:
        lines.append(f"  Clickable:   {sorted({c['day'] for c in clickable})}")
    if disabled:
        lines.append(f"  Disabled:    {sorted({c['day'] for c in disabled})}")
    return "\n".join(lines)


async def dump_calendar(page) -> dict:
    return await page.evaluate(DUMP_JS)


async def click_nav(page, direction: str) -> dict:
    """direction: 'next' or 'prev'."""
    return await page.evaluate(CLICK_NAV_JS, direction)


async def navigate_to_month(page, target_year: int, target_month: int) -> dict:
    """Click left/right until calendar shows target year-month. Stops on
    no-progress (chevron disabled at boundary) or after 24 clicks."""
    for step in range(24):
        dump = await dump_calendar(page)
        cur = parse_month(dump["month_label"])
        if cur is None:
            print(f"  [step {step}] could not parse month label {dump['month_label']!r}; aborting")
            return dump
        cy, cm = cur
        delta = (target_year - cy) * 12 + (target_month - cm)
        print(f"  [step {step}] currently {cy}-{cm:02d}  target {target_year}-{target_month:02d}  delta={delta}")
        if delta == 0:
            return dump

        direction = "next" if delta > 0 else "prev"
        click = await click_nav(page, direction)
        if not click.get("clicked"):
            print(f"  [step {step}] no {direction} control found — stopping")
            return dump

        # wait for the calendar to re-render and verify month actually changed
        await page.wait_for_timeout(700)
        new_dump = await dump_calendar(page)
        new_cur = parse_month(new_dump["month_label"])
        if new_cur == cur:
            print(f"  [step {step}] {direction} click did not advance — chevron likely disabled at boundary")
            return new_dump
    return await dump_calendar(page)


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await get_or_spawn_chrome(p)
        page = await get_or_open_page(browser)

        # Only navigate if Chrome isn't already on the right page — direct
        # navigation to /reservations/new sometimes redirects to the detail
        # page when there's no referrer / session state.
        if page.url.rstrip("/") != URL.rstrip("/"):
            await page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
        try:
            await page.wait_for_selector("td > a", timeout=10_000)
        except Exception:
            cur_url = page.url
            print(f"[!!] Calendar didn't render. URL is now: {cur_url}")
            if "/reservations/new" not in cur_url:
                print("    The /reservations/new path was redirected back to the detail page.")
                print("    Manually click 'このお店を予約する' in the Chrome window to enter the")
                print("    reservation flow, then re-run this script without re-navigating:")
                print("    (the script will reuse whatever tab is open)")
            return
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass

        initial = await dump_calendar(page)
        (OUT_DIR / "cal_initial.html").write_text(await page.content(), encoding="utf-8")
        print(summarize(initial, "INITIAL (whatever month the page opened on)"))
        print()

        final = await navigate_to_month(page, TARGET_YEAR, TARGET_MONTH)
        (OUT_DIR / "cal_target.html").write_text(await page.content(), encoding="utf-8")
        print()
        print(summarize(final, f"TARGET ({TARGET_YEAR}-{TARGET_MONTH:02d})"))

        cur = parse_month(final["month_label"])
        if cur != (TARGET_YEAR, TARGET_MONTH):
            print(f"\n[!!] Did not land on {TARGET_YEAR}-{TARGET_MONTH:02d}; landed on {cur}.")


if __name__ == "__main__":
    asyncio.run(main())
