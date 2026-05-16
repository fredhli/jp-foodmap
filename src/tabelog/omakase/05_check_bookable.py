"""
Visit each restaurant in restaurants.csv and check whether the red bookable
button exists. The button signature (established by probing 3 sample pages):

    <a class="ui button primary big fluid" href="/r/<slug>/reservations/new">
      このお店を予約する
    </a>

Presence -> bookable=yes. Absence -> bookable=no (gray state: either
"敬请关注最新预约信息" or "没有空座" — both render identically in DOM).

Output: data/omakase/bookable.csv  (incremental, resumable).
Columns: slug, name, cuisine, prefecture, detail_url, bookable,
         button_text, reservation_url, probed_at

Re-running skips rows already written in bookable.csv. Delete that file
to re-check from scratch.
"""

import asyncio
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from playwright.async_api import async_playwright

from tabelog.browser import get_or_spawn_chrome, get_or_open_page
from tabelog.paths import RESTAURANTS_CSV, BOOKABLE_CSV

IN_PATH = RESTAURANTS_CSV
OUT_PATH = BOOKABLE_CSV

FIELDS = [
    "slug", "name", "cuisine", "prefecture", "detail_url",
    "bookable", "button_text", "reservation_url", "probed_at",
]

# Check JS: look for the canonical red button. We accept any anchor whose
# className contains the four tokens, so DOM ordering of classes doesn't matter.
CHECK_JS = r"""
() => {
    const anchors = document.querySelectorAll('a[href]');
    for (const a of anchors) {
        const cls = (a.className || '').toString();
        const need = ['ui', 'button', 'primary', 'big', 'fluid'];
        if (!need.every(t => cls.split(/\s+/).includes(t))) continue;
        return {
            found: true,
            text: (a.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80),
            href: a.getAttribute('href') || '',
        };
    }
    return { found: false };
}
"""


def load_done() -> set[str]:
    if not OUT_PATH.exists():
        return set()
    done: set[str] = set()
    with OUT_PATH.open("r", encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            slug = (row.get("slug") or "").strip()
            if slug:
                done.add(slug)
    return done


def load_inputs() -> list[dict]:
    with IN_PATH.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


async def check_one(page, row: dict) -> dict:
    url = row["detail_url"]
    out = {
        **row,
        "bookable": "",
        "button_text": "",
        "reservation_url": "",
        "probed_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        result = await page.evaluate(CHECK_JS)
        if result.get("found"):
            out["bookable"] = "yes"
            out["button_text"] = result.get("text", "")
            href = result.get("href", "")
            if href.startswith("/"):
                href = "https://omakase.in" + href
            out["reservation_url"] = href
        else:
            out["bookable"] = "no"
    except Exception as e:
        out["bookable"] = f"error: {e.__class__.__name__}: {str(e)[:120]}"
    return out


async def main() -> None:
    if not IN_PATH.exists():
        sys.exit(f"Not found: {IN_PATH} — run 03_scrape_list.py first")

    inputs = load_inputs()
    done = load_done()
    todo = [r for r in inputs if r["slug"] not in done]

    print(f"Total in restaurants.csv: {len(inputs)}")
    print(f"Already done in bookable.csv: {len(done)}")
    print(f"To probe: {len(todo)}")
    if not todo:
        print("Nothing to do.")
        return

    new_file = not OUT_PATH.exists()
    async with async_playwright() as p:
        browser = await get_or_spawn_chrome(p)
        page = await get_or_open_page(browser)

        with OUT_PATH.open("a", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new_file:
                w.writeheader()
            for i, row in enumerate(todo, 1):
                print(f"[{i}/{len(todo)}] {row['slug']}  {row['name'][:40]}")
                result = await check_one(page, row)
                status = result["bookable"]
                marker = "✓" if status == "yes" else ("✗" if status == "no" else "!")
                print(f"          {marker} {status}  {result.get('button_text','')[:40]}")
                w.writerow(result)
                f.flush()

    # Summary
    bookable = 0
    gray = 0
    err = 0
    with OUT_PATH.open("r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            s = r["bookable"]
            if s == "yes":
                bookable += 1
            elif s == "no":
                gray += 1
            else:
                err += 1
    print(f"\nDone. {bookable} bookable / {gray} gray / {err} error.")
    print(f"Output: {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
