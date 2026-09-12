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

# The published row count moves with every scrape (2.2.0 shipped 10,250 after
# the Tokyo top-up), so read it from the build instead of pinning a number.
EXPECTED_TOTAL = len(json.loads(
    (REPO / "docs" / "data" / "restaurants.json").read_text(encoding="utf-8")))

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
    """4.0: the filter form is built by the `filters` module into #filters-root,
    which containers moves into whichever host the current mode uses (the narrow
    sheet, or the wide left column's 筛选 tab). 3.2.x waited on #ff-sheet-content
    becoming laid out after clicking .wb-filter-btn / #wb-seg's filter segment;
    the equivalent is act.setTab('filters') plus waiting for the form to be laid
    out. Same guarantee: after this returns the filter controls are on screen and
    their values reflect the restored state."""
    page.evaluate("() => App.act.setTab('filters')")
    page.wait_for_function(
        "() => { const r = document.getElementById('filters-root');"
        "  return !!(r && r.offsetParent !== null"
        "            && r.querySelector('[data-sw=\"bookableOnly\"]')); }",
        timeout=15000,
    )
    page.wait_for_timeout(350)  # let the sheet height transition settle


def filters_state(page) -> dict:
    """App.state.filters with the three Sets turned into sorted lists."""
    return page.evaluate(
        "() => { const f = App.state.filters;"
        "  return Object.assign({}, f, {budgets: Array.from(f.budgets).sort(),"
        "    cuisines: Array.from(f.cuisines).sort(),"
        "    awards: Array.from(f.awards).sort()}); }"
    )


def fav_count(page) -> int:
    """3.2.x read the #ff-fav-count badge. In 4.0 that number is the size of
    App.state.user.fav, which the Saved tab's own count is rendered from."""
    return int(page.evaluate("() => App.state.user.fav.size"))


def import_file(page, path) -> None:
    """3.2.x drove the hidden #ssm-import-file input behind 导入 in the avatar
    menu. 4.0's equivalent is #ov-file, created by the overlays module at init
    for exactly this reason; setting files on it runs the same
    act.readImportFile -> preview path a real OS picker would."""
    page.wait_for_selector("#ov-file", state="attached", timeout=15000)
    page.set_input_files("#ov-file", str(path))
    page.wait_for_function(
        "() => App.state.overlay.kind === 'importDialog'", timeout=15000)


def import_preview(page) -> dict:
    """What the preview shows BEFORE the user confirms. 3.2.x read #imp-fav-n /
    #imp-bm-n / #imp-note; 4.0 shows 文件含有 / 新增 / 跳过 plus a per-category
    pick row carrying each category's own count."""
    return page.evaluate(
        "() => { const stats = [];"
        "  document.querySelectorAll('#modal-root .ov-import-stat').forEach(el => {"
        "    const b = el.querySelector('b');"
        "    stats.push(parseInt((b ? b.textContent : '').replace(/[^0-9]/g, ''), 10) || 0); });"
        "  const picks = {};"
        "  document.querySelectorAll('#modal-root [data-ov=\"imp-pick\"]').forEach(i => {"
        "    const row = i.closest('.ov-imp-pick');"
        "    const n = row && row.querySelector('.num');"
        "    picks[i.dataset.key] = n ? (parseInt(n.textContent.replace(/[^0-9]/g,''),10)||0) : 0; });"
        "  const btn = document.querySelector('#modal-root [data-ov=\"import-confirm\"]');"
        "  const err = document.querySelector('#modal-root .field-error');"
        "  return {contains: stats[0], add: stats[1], skip: stats[2], picks: picks,"
        "          confirmEnabled: !!(btn && !btn.disabled),"
        "          error: err ? err.textContent : null}; }"
    )


def import_confirm(page) -> None:
    page.eval_on_selector(
        "#modal-root [data-ov='import-confirm']", "el => el.click()")
    page.wait_for_function(
        "() => App.state.overlay.kind !== 'importDialog'", timeout=15000)


def load_fixture(name: str) -> dict[str, str]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data["localStorage"]


# ---------------------------------------------------------------------------

@case("01_filterstate_pre_bookableonly.json")
def t01_filter_state(page, base):
    """Old filterState shape: page boots, the legacy `bookable` string is
    derived into the bookableOnly checkbox, the unknown `book` key is ignored,
    and the corpus total is untouched."""
    eq(total_count(page), EXPECTED_TOTAL, "total restaurant count")
    # 3.2.x read #ff-bookable-only.checked. The 4.0 control is the switch
    # [data-sw="bookableOnly"]; assert BOTH it and the state it renders, so a
    # regression in the derivation and one in the rendering are both visible.
    eq(filters_state(page)["bookableOnly"], True,
       "legacy bookable:'yes' derived into bookableOnly")
    open_filter_panel(page)
    checked = page.eval_on_selector('[data-sw="bookableOnly"]', "el => el.checked")
    eq(checked, True, "the derived value reaches the switch the user sees")
    if shown_count(page) <= 0:
        raise AssertionError("no restaurants shown after restoring old filter state")

    # The three genres in the fixture come back checked; the buckets that are
    # not in the (pre-M-016) list arrive unchecked. That is the documented
    # legacy behaviour — a checked list is a closed world — and it self-heals
    # the moment apply() runs, because this build persists the complement
    # (uncheckedGenres) instead. Assert both halves so a regression in either
    # direction is visible.
    # 3.2.x: input[name=ff-genre]:checked. 4.0: the same set, read off
    # App.state.filters.cuisines and off the checked [data-cuisine] boxes.
    eq(sorted(filters_state(page)["cuisines"]),
       sorted(["寿司·海鲜", "烤肉·内脏", "烤鸡·串烧"]),
       "legacy checked genre list restored verbatim")
    on = page.eval_on_selector_all(
        "[data-cui-cb]:checked", "els => els.map(e => e.dataset.cuiCb)")
    eq(sorted(on), sorted(["寿司·海鲜", "烤肉·内脏", "烤鸡·串烧"]),
       "and the same three boxes are ticked in the form")
    # Dispatch the click through the DOM rather than Playwright's pointer
    # path: the panel's nodes are re-created by the runtime emoji/i18n pass,
    # so the resolved handle can go stale mid-actionability-check and the
    # click never lands. We are asserting persistence here, not hit-testing.
    # 3.2.x clicked #ff-genre-all ("tick every cuisine"); the 4.0 control with
    # that meaning is the 全选 checkbox at the top of the cuisine box (A05). An earlier 4.0 pass pointed this at
    # 全清 because the adapter then read an empty set as "no restriction" —
    # that reading is gone (the sets are literal now, 全清 really clears), so
    # this goes back to the button the 3.2.x assertion was written against.
    # The assertion below is unchanged and is the point: the persisted shape
    # must be the complement (uncheckedGenres: []), so a cuisine bucket added
    # in a later release arrives INCLUDED rather than silently hiding its
    # restaurants.
    page.eval_on_selector(
        '[data-bulk-cb="cuisine"]',
        "el => { el.checked = true; el.dispatchEvent(new Event('change', {bubbles:true})); }")
    page.wait_for_timeout(400)
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
    eq(fav_count(page), 3, "favorites count from the pre-2.0 cache")

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
    # 3.2.x painted every built-in and then hid the tombstoned one with CSS, so
    # the test counted a `.bm-mk-hidden` element that was present but had no
    # offsetParent. 4.0 filters it out of the layer instead (Data.visibleLandmarks
    # honours layers.hiddenLandmarks), so the same guarantee — "the tombstone
    # hides it, and it is hidden rather than deleted" — is asserted as:
    #   (a) the id is in the hidden set,
    #   (b) no marker for it is on the map by default,
    #   (c) turning 也显示已隐藏的景点 on brings it back, struck through.
    eq(page.evaluate("() => Data.hiddenLandmarkIds().has('fb-tokyo-tower')"), True,
       "the tombstone is recognised as hiding that built-in")
    eq(page.eval_on_selector_all(".bm-mk-hidden", "els => els.length"), 0,
       "the hidden built-in is not painted")
    shown_ids = page.evaluate(
        "() => Data.visibleLandmarks(App.state.user, App.state.layers).map(l => l.id)")
    if "fb-tokyo-tower" in shown_ids:
        raise AssertionError("the tombstoned built-in is still in the visible set")
    page.evaluate("() => App.act.setLayers({hiddenLandmarks: true})")
    page.wait_for_timeout(400)
    eq(page.eval_on_selector_all(".bm-mk-hidden", "els => els.length"), 1,
       "with 也显示已隐藏的景点 on, exactly the one tombstoned built-in comes "
       "back marked hidden — it was hidden, not deleted")
    page.evaluate("() => App.act.setLayers({hiddenLandmarks: false})")
    page.wait_for_timeout(300)
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
    eq(total_count(page), EXPECTED_TOTAL, "corpus total unaffected by the region filter")
    open_filter_panel(page)
    # 3.2.x used a <select id="ff-region"> whose "" option was 全部地区 and whose
    # option count proved all 47 prefectures were offered. 4.0 uses a button
    # plus the overlays regionPicker; the same two facts are
    # App.state.filters.region === None and Data.config.REGIONS.length === 47.
    eq(filters_state(page)["region"], None,
       "a state without `region` selects 全部地区")
    eq(page.evaluate("() => Data.config.REGIONS.length"), 47,
       "all 47 prefectures are offered")
    page.evaluate("() => App.act.openOverlay('regionPicker')")
    page.wait_for_timeout(300)
    n_opts = page.eval_on_selector_all("[data-region]", "els => els.length")
    eq(n_opts, 48, "47 prefectures plus the 全部地区 row")
    page.evaluate("() => App.act.closeOverlay('cancel')")
    page.wait_for_timeout(200)
    if shown_count(page) <= 0:
        raise AssertionError("no restaurants shown after restoring pre-region state")
    # The rest of the old state still applies, so this is a real restore and
    # not a silent reset-to-defaults.
    eq(page.eval_on_selector("#ft-rating", "el => el.value"), "3.6",
       "the rating from the old state survived")
    eq(round(filters_state(page)["ratingMin"], 2), 3.6,
       "and the state behind the slider agrees")
    # And an apply() rewrites the state WITH the new field, additively. 3.2.x
    # clicked #ff-price-all (tick every price tier). Under FILTER-01 every tier
    # is already the default (the empty set), so that button is correctly
    # disabled here — any real user apply() proves the same thing, so tick one
    # price tier instead. What matters is the assertion below: the rewritten
    # state carries `region`.
    page.eval_on_selector(
        '.ft-budget[data-budget] input[type=checkbox]', "el => el.click()")
    page.wait_for_timeout(400)
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
    eq(total_count(page), EXPECTED_TOTAL, "corpus total unaffected")
    open_filter_panel(page)
    eq(filters_state(page)["region"], None,
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
    eq(filters_state(page)["region"], None,
       "region 99 (out of 0..46) falls back to 全部地区")
    if shown_count(page) <= 0:
        raise AssertionError("an out-of-range region emptied the map")


@case("07_bookmarks_with_meta.json")
def t07_bookmarks_with_meta(page, base):
    """M-031 / E2 sub-collection rows share the bookmarks array with real
    pins and hide-tombstones: they paint no marker, don't tombstone anything,
    survive an unrelated save untouched, and only the orphaned member is
    swept."""
    page.wait_for_function(
        "() => document.querySelectorAll('.bm-mk-attraction').length > 0",
        timeout=30000,
    )
    # 219 built-in landmarks + the one real personal pin. The three metadata
    # entries must produce no marker at all — renderBookmark's numeric-coord
    # guard is what stops them becoming pins at (undefined, undefined).
    # 3.2.x counted `.bm-mk` (every marker the bookmarks layer drew, hidden
    # ones included). 4.0 leaves a hidden built-in out of the layer entirely,
    # so the on-screen count is 218 built-ins + 1 pin; the invariant being
    # protected — three metadata rows paint NO marker — is asserted as the
    # difference between the rows in storage and the markers on the map.
    eq(page.eval_on_selector_all(
        ".bm-mk-attraction, .bm-mk-bookmark", "els => els.length"), 219,
       "marker count: 218 shown built-ins + 1 personal pin, metadata paints none")
    eq(page.evaluate("() => Data.hiddenLandmarkIds().size"), 1,
       "only the fb-* tombstone hides a built-in — 'meta' is not 'hidden'")
    eq(page.evaluate(
        "() => Data.pins(App.state.user.bookmarks).length"), 1,
       "the three category:'meta' rows are not pins")
    eq(page.eval_on_selector_all(".bm-mk-bookmark", "els => els.length"), 1,
       "the ordinary personal pin still renders")

    # The data layer reads the fixture back.
    lists = page.evaluate("() => window.__flLists()")
    eq(len(lists), 1, "one sub-collection")
    eq(lists[0]["id"], "list:k7f2x", "list id read back verbatim")
    eq(lists[0]["name"], "京都", "list name read back verbatim")
    live = "https://tabelog.com/kyoto/A2601/A260302/26000305/"
    orphan = "https://tabelog.com/tokyo/A1301/A130101/13000000/"
    eq(page.evaluate("() => window.__flMembers('list:k7f2x')"), [live],
       "the orphaned member is hidden on read, the starred one is not")
    eq(page.evaluate("(u) => window.__flListsOf(u)", live), ["list:k7f2x"],
       "reverse lookup finds the list")
    eq(page.evaluate("(u) => window.__flListsOf(u)", orphan), ["list:k7f2x"],
       "reverse lookup is not orphan-filtered — the row is still there")

    before = json.loads(page.evaluate(
        "() => localStorage.getItem('tabelog.bookmarks')"))
    eq(len(before), 5, "nothing was rewritten by a pure page load")

    # An unrelated write (a brand-new list) must leave every pre-existing
    # entry byte-identical and take exactly the orphan with it.
    page.evaluate("() => window.__flCreate('无关', '📁')")
    page.wait_for_timeout(200)
    after = json.loads(page.evaluate(
        "() => localStorage.getItem('tabelog.bookmarks')"))
    kept = {json.dumps(b, sort_keys=True, ensure_ascii=False) for b in after}
    for b in before:
        blob = json.dumps(b, sort_keys=True, ensure_ascii=False)
        is_orphan = b.get("ref") == orphan
        if is_orphan and blob in kept:
            raise AssertionError("the orphaned member row was not swept")
        if not is_orphan and blob not in kept:
            raise AssertionError(f"entry rewritten by an unrelated save: {blob}")
    eq(len(after), 5, "4 survivors + the new list body")

    # The favourite behind the live member is untouched by all of this.
    cache = json.loads(page.evaluate(
        "() => localStorage.getItem('omakase_state_cache_v2')"))
    if live not in (cache.get("fav") or []):
        raise AssertionError("sweeping an orphan must not touch state.fav")

    # Deleting a list takes its members and nothing else.
    page.evaluate("() => window.__flDelete('list:k7f2x')")
    page.wait_for_timeout(200)
    left = json.loads(page.evaluate(
        "() => localStorage.getItem('tabelog.bookmarks')"))
    eq([b["id"] for b in left if b.get("list") == "list:k7f2x"], [],
       "members of a deleted list go with it")
    eq(len([b for b in left if b.get("id") == "bm-legacy-9"]), 1,
       "the personal pin is untouched")
    eq(len([b for b in left if b.get("category") == "hidden"]), 1,
       "the built-in tombstone is untouched")
    cache = json.loads(page.evaluate(
        "() => localStorage.getItem('omakase_state_cache_v2')"))
    if live not in (cache.get("fav") or []):
        raise AssertionError("deleting a list must not un-star its members")


@case("08_export_legacy.json")
def t08_export_legacy(page, base):
    """A pre-2.0 favorites.json (favorites/blacklist = plain URL strings)
    still imports, and the M-032 object export round-trips back in."""
    import tempfile

    blob = json.loads((FIXTURES / "08_export_legacy.json").read_text(
        encoding="utf-8"))["exportFile"]
    tmp = Path(tempfile.mkdtemp()) / "favorites.json"
    tmp.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")

    # The file input is hidden behind 导入 in the account panel; set_input_files
    # drives it the same way the OS picker would.
    import_file(page, tmp)
    prev = import_preview(page)
    # 3.2.x read the "N 家餐厅" row (#imp-fav-n). 4.0's preview carries the same
    # number in the 收藏 pick row.
    eq(prev["picks"]["fav"], 3, "legacy string array counted as 3 favorites")
    import_confirm(page)

    eq(fav_count(page), 3,
       "favorites imported from the legacy string-array export")

    # The in-memory / on-disk state is still a set of URL STRINGS. The KV
    # blob shape depends on this — buildBody() serializes state.fav directly.
    cache = json.loads(page.evaluate(
        "() => localStorage.getItem('omakase_state_cache_v2')"))
    eq(len(cache.get("fav") or []), 3, "three favorites in the cache")
    if not all(isinstance(u, str) for u in cache["fav"]):
        raise AssertionError(f"state.fav must stay strings, got {cache['fav']!r}")
    if not all(isinstance(u, str) for u in (cache.get("black") or [])):
        raise AssertionError("state.black must stay strings")
    bms = json.loads(page.evaluate("() => localStorage.getItem('tabelog.bookmarks')"))
    if not any(b.get("id") == "bm-legacy-1" for b in bms):
        raise AssertionError("the legacy export's bookmark did not import")

    # M-032 export: objects carrying url/name/rating/city. Captured at
    # URL.createObjectURL rather than through the download machinery — the
    # bytes are what matters, not the browser's save dialog.
    page.evaluate(
        "() => { window.__exported = null;"
        "  const orig = URL.createObjectURL.bind(URL);"
        "  URL.createObjectURL = function(b) {"
        "    b.text().then(t => { window.__exported = t; });"
        "    return orig(b); }; }"
    )
    # 3.2.x clicked #ssm-export in the avatar menu; 4.0's account panel calls
    # act.exportBackup(), which is the same production path.
    page.evaluate("() => App.act.exportBackup()")
    page.wait_for_function("() => window.__exported !== null", timeout=15000)
    exported = json.loads(page.evaluate("() => window.__exported"))
    eq(exported.get("schema"), 1,
       "schema stays 1 — no reader gates on it and bumping it can only break "
       "older builds")
    favs = exported["favorites"]
    eq(len(favs), 3, "three favorites exported")
    for f in favs:
        if not isinstance(f, dict):
            raise AssertionError(f"export entry should be an object, got {f!r}")
        for k in ("url", "name", "rating", "city"):
            if k not in f:
                raise AssertionError(f"export entry missing {k}: {f!r}")
    if not any(f["name"] for f in favs):
        raise AssertionError("export entries carry no names at all")

    # And the new shape imports back: everything is already there, so the
    # merge reports nothing new rather than duplicating or throwing.
    tmp2 = tmp.with_name("favorites-new.json")
    tmp2.write_text(json.dumps(exported, ensure_ascii=False), encoding="utf-8")
    import_file(page, tmp2)
    prev2 = import_preview(page)
    eq(prev2["picks"]["fav"], 3,
       "the object export is counted the same as the string one")
    eq(prev2["add"], 0,
       "everything is already there, so the preview offers nothing new")
    import_confirm(page)
    eq(fav_count(page), 3,
       "re-importing the object export adds nothing and loses nothing")


@case("09_export_idless_pin.json")
def t09_export_idless_pin(page, base):
    """BE-C: a backup with an id-less legacy pin and one unreadable URL still
    imports everything readable, and says how much it skipped."""
    import tempfile

    blob = json.loads((FIXTURES / "09_export_idless_pin.json").read_text(
        encoding="utf-8"))["exportFile"]
    tmp = Path(tempfile.mkdtemp()) / "favorites.json"
    tmp.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")

    # 3.1.1 rejected this file outright: no modal, just "文件格式无法识别".
    import_file(page, tmp)
    prev = import_preview(page)
    # 3.2.x: #imp-fav-n "2 家餐厅", #imp-bm-n "0 个景点 · 1 个书签", #imp-note
    # carrying the skipped count. 4.0 shows the per-category counts in the pick
    # rows and the skipped count as its own 跳过 stat.
    eq(prev["picks"]["fav"], 2,
       "the two readable favorites are counted, the bad URL is not")
    eq(prev["picks"]["bm"], 1,
       "the id-less pin survives, the bad-coordinate one does not")
    eq(prev["skip"], 2,
       "the skipped count is reported to the user BEFORE they confirm")
    eq(prev["confirmEnabled"], True, "a partially-readable file is importable")

    import_confirm(page)

    eq(fav_count(page), 2, "the readable favorites imported")
    cache = json.loads(page.evaluate(
        "() => localStorage.getItem('omakase_state_cache_v2')"))
    if any("not-a-url" in u for u in (cache.get("fav") or [])):
        raise AssertionError("an unparseable URL reached state.fav")

    bms = json.loads(page.evaluate("() => localStorage.getItem('tabelog.bookmarks')"))
    legacy = [b for b in bms if b.get("name") == "无 id 的旧书签"]
    eq(len(legacy), 1, "the id-less legacy pin imported")
    if not str(legacy[0].get("id", "")).startswith("bm-"):
        raise AssertionError(f"no generated bm- id: {legacy[0]!r}")
    if any(b.get("id") == "bm-broken" for b in bms):
        raise AssertionError("the bad-coordinate pin should have been skipped")


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
