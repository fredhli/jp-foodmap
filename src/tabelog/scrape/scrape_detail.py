"""
Phase 2: visit each detail URL in tabelog_osaka.csv, extract:
  seat_count, address, reservation_policy, tabelog_bookable
Then Google-translate reservation_policy -> reservation_policy_chinese.

Appends 5 columns to the CSV in-place. Checkpoints every 10 rows so a crash
mid-run doesn't lose progress.

CSV is utf-8-sig (BOM) so Excel renders Japanese correctly.
"""

import asyncio
import csv
import sys
from pathlib import Path

from deep_translator import GoogleTranslator
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from tabelog.browser import get_or_spawn_chrome
from tabelog.paths import TABELOG_OSAKA_CSV

CSV_PATH = TABELOG_OSAKA_CSV
PAGE_DELAY_S = 1.5

NEW_COLS = [
    "seat_count", "address", "reservation_policy",
    "reservation_policy_chinese", "tabelog_bookable",
]

DETAIL_JS = r"""
() => {
    const out = {seat_count: null, address: null,
                 reservation_policy: null, tabelog_bookable: false};
    for (const tr of document.querySelectorAll('tr')) {
        const th = tr.querySelector('th');
        const td = tr.querySelector('td');
        if (!th || !td) continue;
        const label = (th.innerText || '').trim();
        const value = (td.innerText || '').replace(/\s+/g, ' ').trim();
        if (label === '席数') out.seat_count = value;
        else if (label === '住所') out.address = value;
        else if (label === '予約可否') out.reservation_policy = value;
    }
    out.tabelog_bookable = document.querySelectorAll('a.js-booking-form-open').length > 0;
    return out;
}
"""


def clean_address(s: str | None) -> str:
    if not s:
        return ""
    for stopper in ("大きな地図", "周辺のお店", "このお店は"):
        i = s.find(stopper)
        if i > 0:
            s = s[:i]
    return s.strip()


def load_rows() -> tuple[list[dict], list[str]]:
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        rows = list(r)
        fieldnames = list(r.fieldnames or [])
    for col in NEW_COLS:
        if col not in fieldnames:
            fieldnames.append(col)
        for row in rows:
            row.setdefault(col, "")
    return rows, fieldnames


def save_rows(rows: list[dict], fieldnames: list[str]) -> None:
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


async def fetch_detail(page, url: str) -> dict:
    await page.goto(url, wait_until="domcontentloaded")
    await asyncio.sleep(PAGE_DELAY_S)
    data = await page.evaluate(DETAIL_JS)
    data["address"] = clean_address(data.get("address"))
    return data


async def main() -> None:
    rows, fieldnames = load_rows()
    if "detail_url" not in fieldnames:
        print("detail_url column not found")
        return

    targets = [(i, row) for i, row in enumerate(rows) if row.get("detail_url")]
    print(f"Processing {len(targets)} detail pages")

    async with async_playwright() as p:
        browser = await get_or_spawn_chrome(p)
        ctx = browser.contexts[0]
        page = next(
            (pg for pg in ctx.pages if "tabelog.com" in (pg.url or "")),
            ctx.pages[0],
        )
        await page.bring_to_front()

        for n, (i, row) in enumerate(targets, 1):
            url = row["detail_url"]
            name = row.get("name", "")
            try:
                data = await fetch_detail(page, url)
            except Exception as e:
                print(f"  [{n}/{len(targets)}] row {i+2} {name!r}: ERROR {e}")
                continue
            row["seat_count"] = data["seat_count"] or ""
            row["address"] = data["address"] or ""
            row["reservation_policy"] = data["reservation_policy"] or ""
            row["tabelog_bookable"] = "True" if data["tabelog_bookable"] else "False"
            print(f"  [{n}/{len(targets)}] row {i+2} {name!r}: "
                  f"seats={data['seat_count']!r}, bookable={data['tabelog_bookable']}")
            if n % 10 == 0:
                save_rows(rows, fieldnames)
        save_rows(rows, fieldnames)

    print("\nTranslating reservation_policy -> Chinese ...")
    translator = GoogleTranslator(source="ja", target="zh-CN")
    for i, row in enumerate(rows):
        ja = row.get("reservation_policy", "")
        if not ja:
            continue
        try:
            zh = translator.translate(ja)
        except Exception as e:
            print(f"  row {i+2}: translation FAIL: {e}")
            zh = ""
        row["reservation_policy_chinese"] = zh or ""
        print(f"  row {i+2}: ja {len(ja)} chars -> zh {len(zh or '')} chars")
        await asyncio.sleep(0.3)
    save_rows(rows, fieldnames)
    print(f"\nSaved {CSV_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
