#!/usr/bin/env python3
"""Backwards-compatibility tests — pre-2.0 localStorage must still work.

    uv run python tests/compat/run.py
    uv run python tests/compat/run.py 03      # one fixture by number

Each fixture in tests/compat/fixtures/ is a snapshot of what a real browser
had in localStorage before the 2.0.0 work, injected with add_init_script and
then asserted against the freshly built docs/index.html. This is the
executable form of CLAUDE.md's cardinal rule: a user who refreshes after a
deploy sees exactly the favorites / hidden pins / pending sync they saw
before it.

Needs a build in docs/ (uv run python src/tabelog/scrape/map.py) and
Playwright's Chromium. No network, no real Google sign-in, no writes to
api.jpfoodmap.com.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))

from lib_browser import (  # noqa: E402
    boot,
    reload_and_wait,
    install_guards,
    seed_local_storage,
    shown_count,
    serve_docs,
    total_count,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

_cases: list = []


def case(fixture: str):
    def deco(fn):
        _cases.append((fixture, fn))
        return fn
    return deco


def eq(actual, expected, what: str) -> None:
    if actual != expected:
        raise AssertionError(f"{what}: expected {expected!r}, got {actual!r}")


def open_filter_panel(page) -> None:
    """The filter panel's inputs are in the DOM from boot but only laid out
    once a host is open. M-027 gave it two hosts: the bottom sheet behind
    #ff-fab (<700px) and the non-modal popover behind the top bar's 筛选
    button (>=700px). Wait on #ff-sheet-content being laid out, which is
    true in either."""
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
    page.wait_for_timeout(350)  # let the 0.25s slide-up settle


def load_fixture(name: str) -> dict[str, str]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data["localStorage"]


# ---------------------------------------------------------------------------

@case("01_filterstate_pre_bookableonly.json")
def t01_filter_state(page, base):
    """Old filterState shape: page boots, the legacy `bookable` string is
    derived into the bookableOnly checkbox, the unknown `book` key is ignored,
    and the corpus total is untouched."""
    eq(total_count(page), 9807, "total restaurant count")
    checked = page.eval_on_selector("#ff-bookable-only", "el => el.checked")
    eq(checked, True, "legacy bookable:'yes' derived into bookableOnly")
    if shown_count(page) <= 0:
        raise AssertionError("no restaurants shown after restoring old filter state")

    # The three genres in the fixture come back checked; the buckets that are
    # not in the (pre-M-016) list arrive unchecked. That is the documented
    # legacy behaviour — a checked list is a closed world — and it self-heals
    # the moment apply() runs, because this build persists the complement
    # (uncheckedGenres) instead. Assert both halves so a regression in either
    # direction is visible.
    on = page.eval_on_selector_all(
        "input[name=ff-genre]:checked", "els => els.map(e => e.value)"
    )
    eq(sorted(on), sorted(["寿司·海鲜", "烤肉·内脏", "烤鸡·串烧"]),
       "legacy checked genre list restored verbatim")
    # Dispatch the click through the DOM rather than Playwright's pointer
    # path: the panel's nodes are re-created by the runtime emoji/i18n pass,
    # so the resolved handle can go stale mid-actionability-check and the
    # click never lands. We are asserting persistence here, not hit-testing.
    open_filter_panel(page)
    page.eval_on_selector("#ff-genre-all", "el => el.click()")
    page.wait_for_timeout(300)
    saved = json.loads(page.evaluate(
        "() => localStorage.getItem('tabelog.filterState')"))
    if not isinstance(saved.get("uncheckedGenres"), list):
        raise AssertionError(
            "after an apply(), state should be rewritten in the "
            "forward-compatible complement form (uncheckedGenres)"
        )
    eq(saved["uncheckedGenres"], [],
       "select-all writes an empty unchecked list, so a future bucket "
       "arrives checked instead of silently hiding its restaurants")


@case("02_state_cache_dirty.json")
def t02_dirty_cache(page, base):
    """Unsynced anonymous state: the three favorites survive and the dirty
    flag is NOT cleared at boot."""
    fav_txt = page.eval_on_selector("#ff-fav-count", "el => el.textContent")
    n_fav = int("".join(c for c in fav_txt if c.isdigit()) or 0)
    eq(n_fav, 3, "favorites count from the pre-2.0 cache")

    raw = page.evaluate("() => localStorage.getItem('omakase_state_cache_v2')")
    cache = json.loads(raw)
    eq(len(cache.get("fav") or []), 3, "favorites still in the cache")
    eq(len(cache.get("black") or []), 1, "blacklist still in the cache")
    eq(cache.get("dirty"), True,
       "dirty flag preserved (clearing it would drop the pending push)")

    # And it survives a reload, which is the shape the cardinal rule is
    # actually written in.
    reload_and_wait(page)
    cache = json.loads(page.evaluate(
        "() => localStorage.getItem('omakase_state_cache_v2')"))
    eq(cache.get("dirty"), True, "dirty flag still true after a refresh")
    eq(len(cache.get("fav") or []), 3, "favorites still there after a refresh")


@case("03_bookmarks_tombstone.json")
def t03_hidden_builtin(page, base):
    """A {id:'fb-*', category:'hidden'} tombstone still hides that built-in."""
    page.wait_for_function(
        "() => document.querySelectorAll('.bm-mk-attraction').length > 0",
        timeout=30000,
    )
    hidden = page.eval_on_selector_all(".bm-mk-hidden", "els => els.length")
    eq(hidden, 1, "exactly the one tombstoned built-in is marked hidden")
    visible = page.evaluate(
        "() => Array.from(document.querySelectorAll('.bm-mk-hidden'))"
        "  .filter(e => e.offsetParent !== null).length"
    )
    eq(visible, 0, "the hidden built-in is not painted")
    # The personal pin in the same array still renders.
    pins = page.eval_on_selector_all(".bm-mk-bookmark", "els => els.length")
    if pins < 1:
        raise AssertionError("the ordinary personal pin stopped rendering")
    # And the tombstone survives the round trip untouched.
    bms = json.loads(page.evaluate("() => localStorage.getItem('tabelog.bookmarks')"))
    tomb = [b for b in bms if b.get("category") == "hidden"]
    eq(len(tomb), 1, "tombstone still in tabelog.bookmarks")
    eq(tomb[0]["id"], "fb-tokyo-tower", "tombstone id unchanged")


@case("04_syncbase_present.json")
def t04_syncbase_preserved(page, base):
    """tabelog.syncBase is the three-way-merge ancestor — boot must not clear
    it, even when the page never reaches the Worker."""
    raw = page.evaluate("() => localStorage.getItem('tabelog.syncBase')")
    if not raw:
        raise AssertionError("tabelog.syncBase was cleared at boot")
    base_obj = json.loads(raw)
    eq(base_obj.get("v"), 7, "syncBase version preserved")
    eq(base_obj.get("w"), "abc123", "syncBase write id preserved")
    eq(base_obj.get("sub"), "1234567890", "syncBase account preserved")
    eq(len(base_obj.get("favorites") or []), 1, "syncBase favorites preserved")

    reload_and_wait(page)
    raw2 = page.evaluate("() => localStorage.getItem('tabelog.syncBase')")
    if not raw2:
        raise AssertionError("tabelog.syncBase was cleared on the second load")
    eq(json.loads(raw2).get("v"), 7, "syncBase still v7 after a refresh")


@case("05_filterstate_pre_region.json")
def t05_filterstate_pre_region(page, base):
    """filterState written before the region selector (C1 / M-023) existed:
    no `region` key, plus an unknown key from a hypothetical future build."""
    eq(total_count(page), 9807, "corpus total unaffected by the region filter")
    open_filter_panel(page)
    sel = page.eval_on_selector("#ff-region", "el => el.value")
    eq(sel, "", "a state without `region` selects 全部地区")
    n_opts = page.eval_on_selector("#ff-region", "el => el.options.length")
    eq(n_opts, 48, "47 prefectures plus the 全部地区 row")
    if shown_count(page) <= 0:
        raise AssertionError("no restaurants shown after restoring pre-region state")
    # The rest of the old state still applies, so this is a real restore and
    # not a silent reset-to-defaults.
    eq(page.eval_on_selector("#ff-rating", "el => el.value"), "3.6",
       "the rating from the old state survived")
    # And an apply() rewrites the state WITH the new field, additively.
    page.eval_on_selector("#ff-price-all", "el => el.click()")
    page.wait_for_timeout(300)
    saved = json.loads(page.evaluate(
        "() => localStorage.getItem('tabelog.filterState')"))
    if "region" not in saved:
        raise AssertionError("apply() should persist the new `region` field")
    eq(saved["region"], None, "no region picked => null, not 0")
    eq(saved.get("someFutureKey"), None,
       "unknown keys are not required to survive, but must not throw")


@case("06_filterstate_bad_region.json")
def t06_filterstate_bad_region(page, base):
    """A `region` of the wrong TYPE (a prefecture name) degrades to 全部地区
    rather than throwing or emptying the map."""
    eq(total_count(page), 9807, "corpus total unaffected")
    open_filter_panel(page)
    eq(page.eval_on_selector("#ff-region", "el => el.value"), "",
       "a non-integer region falls back to 全部地区")
    if shown_count(page) <= 0:
        raise AssertionError("a bad region value emptied the map")
    # Out-of-range integers take the same path.
    page.evaluate(
        "() => { const s = JSON.parse(localStorage.getItem('tabelog.filterState'));"
        "  s.region = 99; localStorage.setItem('tabelog.filterState', JSON.stringify(s)); }"
    )
    reload_and_wait(page)
    open_filter_panel(page)
    eq(page.eval_on_selector("#ff-region", "el => el.value"), "",
       "region 99 (out of 0..46) falls back to 全部地区")
    if shown_count(page) <= 0:
        raise AssertionError("an out-of-range region emptied the map")


# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    from playwright.sync_api import sync_playwright

    wanted = argv[1:]
    n_pass = n_fail = 0
    with serve_docs(8926) as base, sync_playwright() as p:
        browser = p.chromium.launch()
        for fixture, fn in _cases:
            if wanted and not any(w in fixture for w in wanted):
                continue
            errors: list[str] = []
            # service_workers="block": docs/sw.js caches the big JSONs, and a
            # reload served from a half-warm SW cache makes fetches fail
            # nondeterministically under the test server. The SW has its own
            # coverage; compat is about localStorage.
            ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                      service_workers="block")
            page = ctx.new_page()
            install_guards(page, errors)
            try:
                seed_local_storage(page, load_fixture(fixture))
                boot(page, base)
                fn(page, base)
                if errors:
                    raise AssertionError(
                        f"{len(errors)} console/page error(s): {errors[:5]}"
                    )
            except Exception:
                n_fail += 1
                print(f"  FAIL  {fixture}")
                traceback.print_exc()
            else:
                n_pass += 1
                print(f"  ok    {fixture}  ({fn.__doc__.splitlines()[0]})")
            finally:
                ctx.close()
        browser.close()
    print(f"\ncompat: {n_pass} passed, {n_fail} failed")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
