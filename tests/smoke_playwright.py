#!/usr/bin/env python3
"""Cross-viewport smoke test for the built page (M-056).

    uv run python tests/smoke_playwright.py
    uv run python tests/smoke_playwright.py --viewport fold-outer
    uv run python tests/smoke_playwright.py --screenshots <dir>

Six things, on each of the four viewports the site is actually used at:

  boot      the payload lands and the counter shows the corpus size
  search    typing a restaurant name hits the local index
  card      tapping a marker opens the detail sheet
  save      the star toggles, the counter moves, and it lands in localStorage
  filter    changing one filter changes the visible count
  account   the avatar menu opens and scrolls

A console error that is not on the offline allowlist (Google Identity's 403,
the third-party hosts this run blocks) fails the viewport. Serves docs/ over
a local http.server; no network, no sign-in, no writes to api.jpfoodmap.com.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

from lib_browser import (  # noqa: E402
    boot,
    install_guards,
    serve_docs,
    shown_count,
    total_count,
)

# Fold 8 outer / Fold 8 inner portrait / iPhone 15 / desktop. The two Fold
# sizes are the ones the site is used at on the road; desktop is where trips
# get planned.
VIEWPORTS = {
    "fold-outer": {"width": 416, "height": 657, "mobile": True},
    "fold-inner": {"width": 616, "height": 816, "mobile": True},
    # M-027: the fourth layout mode (top bar + left column + icon rail) only
    # exists between 700 and 1099px, and the Fold's inner screen in landscape
    # is the device that lives there.
    "fold-inner-landscape": {"width": 816, "height": 616, "mobile": True},
    "iphone": {"width": 393, "height": 852, "mobile": True},
    "desktop": {"width": 1440, "height": 900, "mobile": False},
}

# A restaurant that is in every build of the corpus; the local search index
# should match it without touching Nominatim.
SEARCH_TERM = "寿司"


def eq(actual, expected, what: str) -> None:
    if actual != expected:
        raise AssertionError(f"{what}: expected {expected!r}, got {actual!r}")


def open_filter_panel(page) -> None:
    """M-027: two hosts for the same #ff-sheet-content. Below 700px it is the
    bottom sheet behind #ff-fab; at 700px and up it is the non-modal popover
    behind the top bar's 筛选 button. Assert on the content being laid out
    (offsetParent) rather than on either host's class."""
    if page.eval_on_selector(
            "#ff-sheet-content", "el => el.offsetParent !== null"):
        return
    opened = page.evaluate(
        "() => { const b = Array.from(document.querySelectorAll('.wb-filter-btn'))"
        "         .find(e => e.offsetParent !== null);"
        "  if (b) { b.click(); return true; }"
        "  const f = document.getElementById('ff-fab');"
        "  if (f) { f.click(); return true; }"
        "  return false; }"
    )
    if not opened:
        raise AssertionError("no way to open the filter panel on this viewport")
    page.wait_for_function(
        "() => { const c = document.getElementById('ff-sheet-content');"
        "  return c && c.offsetParent !== null; }",
        timeout=15000,
    )
    page.wait_for_timeout(350)


# --- the six checks ---------------------------------------------------------

def check_boot(page, name):
    total = total_count(page)
    if total < 9000:
        raise AssertionError(f"corpus counter shows {total}, expected the full corpus")
    if shown_count(page) <= 0:
        raise AssertionError("no restaurants visible on first paint")
    return f"{shown_count(page):,} / {total:,} restaurants"


def check_search(page, name):
    # Only #ss-local matters here — that is the in-page restaurant index. The
    # #ss-api section is the Nominatim place lookup, whose host this run
    # blocks on purpose, so it legitimately renders "搜索失败".
    page.eval_on_selector("#ss-input", "el => el.focus()")
    page.fill("#ss-input", SEARCH_TERM)
    page.wait_for_function(
        "() => document.querySelectorAll('#ss-local .ss-row:not(.ss-empty)')"
        "  .length > 0",
        timeout=20000,
    )
    rows = page.eval_on_selector_all(
        "#ss-local .ss-row:not(.ss-empty)", "els => els.length")
    if rows < 5:
        raise AssertionError(
            f"local restaurant search for {SEARCH_TERM!r} returned only {rows} rows"
        )
    # (.ss-empty inside #ss-local is the "+N more matches, type more to
    # narrow" footer, not a no-results state — it is expected on a broad term.)
    # Close the dropdown again so it doesn't sit over the map for the next check.
    page.keyboard.press("Escape")
    page.eval_on_selector("#ss-input", "el => { el.value = ''; el.blur(); }")
    return f"{rows} local result rows"


def check_card(page, name):
    page.wait_for_function(
        "() => document.querySelectorAll('.leaflet-marker-icon .rst-mk,"
        " .leaflet-marker-icon').length > 0",
        timeout=30000,
    )
    # Click a restaurant marker (not a bookmark/attraction one).
    clicked = page.evaluate(
        "() => { const els = Array.from(document.querySelectorAll("
        "  '.leaflet-marker-icon')).filter(e =>"
        "    !e.classList.contains('bm-mk') &&"
        "    !e.classList.contains('marker-cluster'));"
        "  if (!els.length) return false;"
        "  els[0].dispatchEvent(new MouseEvent('click', {bubbles: true}));"
        "  return true; }"
    )
    if not clicked:
        raise AssertionError("no restaurant marker found to click")
    page.wait_for_function(
        "() => { const s = document.getElementById('bs-sheet');"
        "  return s && (s.classList.contains('bs-open')"
        "    || s.classList.contains('open')"
        "    || getComputedStyle(s).transform.indexOf('matrix') === 0"
        "       && !s.hidden); }",
        timeout=20000,
    )
    txt = page.eval_on_selector("#bs-content", "el => el.textContent.trim().slice(0, 40)")
    if not txt:
        raise AssertionError("detail sheet opened empty")
    page.evaluate(
        "() => { const b = document.querySelector('#bs-content .rst-close');"
        "  if (b) b.click(); }"
    )
    page.wait_for_timeout(300)
    return f"card shows {txt[:20]!r}"


def check_save(page, name):
    before = page.evaluate(
        "() => { try { const c = JSON.parse("
        "  localStorage.getItem('omakase_state_cache_v2') || '{}');"
        "  return (c.fav || []).length; } catch (e) { return -1; } }"
    )
    n_before = int(page.eval_on_selector("#ff-fav-count", "el => el.textContent") or 0)
    # Open a card and press its star.
    page.evaluate(
        "() => { const els = Array.from(document.querySelectorAll("
        "  '.leaflet-marker-icon')).filter(e =>"
        "    !e.classList.contains('bm-mk') &&"
        "    !e.classList.contains('marker-cluster'));"
        "  els[0].dispatchEvent(new MouseEvent('click', {bubbles: true})); }"
    )
    page.wait_for_selector("#bs-content .ff-fav-btn", timeout=20000)
    page.eval_on_selector("#bs-content .ff-fav-btn", "el => el.click()")
    page.wait_for_function(
        "(n) => Number(document.getElementById('ff-fav-count').textContent) !== n",
        arg=n_before,
        timeout=15000,
    )
    n_after = int(page.eval_on_selector("#ff-fav-count", "el => el.textContent") or 0)
    eq(n_after, n_before + 1, "favorites counter after starring")
    after = page.evaluate(
        "() => { try { const c = JSON.parse("
        "  localStorage.getItem('omakase_state_cache_v2') || '{}');"
        "  return (c.fav || []).length; } catch (e) { return -1; } }"
    )
    eq(after, before + 1, "favorites persisted to localStorage")
    # Undo so the run leaves no state behind for the next check.
    page.eval_on_selector("#bs-content .ff-fav-btn", "el => el.click()")
    page.wait_for_timeout(300)
    page.evaluate(
        "() => { const b = document.querySelector('#bs-content .rst-close');"
        "  if (b) b.click(); }"
    )
    return f"{n_before} -> {n_after} favorites, persisted"


def check_filter(page, name):
    before = shown_count(page)
    open_filter_panel(page)
    # Raise the rating threshold to its maximum — always a strict subset.
    page.eval_on_selector(
        "#ff-rating",
        "el => { el.value = el.max; el.dispatchEvent(new Event('input', {bubbles: true}));"
        "        el.dispatchEvent(new Event('change', {bubbles: true})); }",
    )
    page.wait_for_function(
        "(n) => { const el = document.querySelector('.ff-count');"
        "  return el && Number(el.textContent.replace(/[^0-9]/g, '')) !== n; }",
        arg=before,
        timeout=15000,
    )
    after = shown_count(page)
    if after >= before:
        raise AssertionError(
            f"raising the rating filter did not shrink the result set "
            f"({before} -> {after})"
        )
    saved = json.loads(
        page.evaluate("() => localStorage.getItem('tabelog.filterState') || '{}'")
    )
    if "rating" not in saved:
        raise AssertionError("filter state was not persisted")
    return f"{before:,} -> {after:,} restaurants"


def check_account(page, name):
    page.eval_on_selector("#ss-avatar", "el => el.click()")
    page.wait_for_function(
        "() => { const m = document.getElementById('ss-menu');"
        "  return m && !m.hidden && m.classList.contains('open'); }",
        timeout=15000,
    )
    box = page.eval_on_selector(
        "#ss-menu",
        "el => ({h: el.getBoundingClientRect().height,"
        "        sh: el.scrollHeight, ov: getComputedStyle(el).overflowY})",
    )
    if box["h"] <= 0:
        raise AssertionError("account menu opened with zero height")
    vh = page.evaluate("() => window.innerHeight")
    if box["h"] > vh:
        raise AssertionError(
            f"account menu is {box['h']:.0f}px tall on a {vh}px viewport and "
            f"cannot be scrolled to its end"
        )
    if box["sh"] > box["h"] + 1 and box["ov"] not in ("auto", "scroll"):
        raise AssertionError(
            f"account menu content ({box['sh']:.0f}px) overflows its box "
            f"({box['h']:.0f}px) with overflow-y: {box['ov']}"
        )
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    return f"menu {box['h']:.0f}px tall on a {vh}px viewport"


CHECKS = [
    ("boot", check_boot),
    ("search", check_search),
    ("card", check_card),
    ("save", check_save),
    ("filter", check_filter),
    ("account", check_account),
]


def main(argv: list[str] | None = None) -> int:
    from playwright.sync_api import sync_playwright

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--viewport", action="append", choices=sorted(VIEWPORTS),
                    help="run only this viewport (repeatable)")
    ap.add_argument("--screenshots", type=Path, default=None,
                    help="write one PNG per viewport into this directory")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args(argv)

    names = args.viewport or list(VIEWPORTS)
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)

    n_fail = 0
    with serve_docs(8926) as base, sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        for name in names:
            vp = VIEWPORTS[name]
            print(f"\n[{name}] {vp['width']}x{vp['height']}")
            errors: list[str] = []
            ctx = browser.new_context(
                viewport={"width": vp["width"], "height": vp["height"]},
                is_mobile=vp["mobile"],
                has_touch=vp["mobile"],
                device_scale_factor=2 if vp["mobile"] else 1,
                # docs/sw.js caches the multi-MB payloads; a half-warm SW cache
                # makes local fetches flaky. The SW is not what this test is for.
                service_workers="block",
            )
            page = ctx.new_page()
            install_guards(page, errors)
            try:
                boot(page, base)
                for check_name, fn in CHECKS:
                    detail = fn(page, name)
                    print(f"  ok    {check_name}: {detail}")
                if args.screenshots:
                    shot = args.screenshots / f"smoke-{name}.png"
                    page.screenshot(path=str(shot))
                    print(f"  shot  {shot}")
                if errors:
                    raise AssertionError(
                        f"{len(errors)} console/page error(s): {errors[:5]}"
                    )
                print(f"  PASS  {name} (console clean)")
            except Exception:
                n_fail += 1
                print(f"  FAIL  {name}")
                traceback.print_exc()
                if args.screenshots:
                    try:
                        page.screenshot(
                            path=str(args.screenshots / f"smoke-{name}-FAIL.png"))
                    except Exception:
                        pass
            finally:
                ctx.close()
        browser.close()

    print(f"\nsmoke: {len(names) - n_fail}/{len(names)} viewports passed")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
