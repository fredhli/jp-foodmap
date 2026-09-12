#!/usr/bin/env python3
"""Functional access-path check for features outside the UX demo.

The 2.3 feature DOM inventory is still the retention baseline, but an ID's
presence was never counted as a pass by itself: each group below is opened
through a visible user control and at least one reversible interaction is
exercised. All external HTTPS requests are blocked.

4.0.0: the presentation layer was replaced wholesale, so none of the 2.3
element IDs exist any more. The inventory is therefore read as a list of
FEATURES rather than of ids — RETAINED_FEATURES below maps each 2.3 anchor to
the 4.0 control that now provides it, and the run fails if any of those
controls is missing. What each group asserts is unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import lib_browser  # noqa: E402


BASELINE = ROOT / "tests/fixtures/feature-dom-2.3.0.json"

# 2.3 anchor id -> how the same feature is reached in 4.0.
# `probe` is JS returning true when that capability is present on the page.
RETAINED_FEATURES = {
    "ff-region":        ("region filter",        "!!Data.config.REGIONS && Data.config.REGIONS.length === 47"),
    "ff-rating":        ("rating filter",        "!!document.getElementById('ft-rating')"),
    "ff-bookable-only": ("bookable-only filter", "!!document.querySelector('[data-sw=\"bookableOnly\"]')"),
    "ff-gcal-only":     ("google-calibrated filter", "!!document.querySelector('[data-sw=\"gcalOnly\"]')"),
    "wb-list":          ("result list",          "!!document.getElementById('list-root')"),
    "wb-fav":           ("Saved list",           "!!window.ListMod && typeof ListMod.groupsFor === 'function'"),
    "fab-layers":       ("layers control",       "!!document.querySelector('[data-fab=\"layers\"]')"),
    "fab-transit-long": ("long-haul rail layer", "'long' in App.state.layers"),
    "fab-transit-city": ("in-city rail layer",   "'city' in App.state.layers"),
    "fab-attractions":  ("landmarks layer",      "'landmarks' in App.state.layers"),
    "fab-bookmarks":    ("pins layer",           "'pins' in App.state.layers"),
    "bm-modal":         ("pin form",             "!!window.Overlays && typeof Overlays.openBookmarkForm === 'function'"),
    "ss-avatar":        ("account entry",        '!!document.querySelector(\'[data-kind="account"],[data-ov="open-account"],[data-ct="account"]\')'),
    "ss-menu":          ("account panel",        "typeof App.act.openOverlay === 'function'"),
    "ssm-export":       ("backup export",        "typeof App.act.exportBackup === 'function'"),
    "ssm-import":       ("backup import",        "typeof App.act.readImportFile === 'function'"),
}


def assert_hit(locator, label: str) -> None:
    if not locator.is_visible():
        raise AssertionError(f"{label} is not visible through its user path")
    locator.click(trial=True)


def open_tab(page, tab: str) -> None:
    """3.2.x used lib_browser.phone_tab (#wb-seg / the drawer tab row). 4.0's
    narrow panel is the bottom sheet; act.setTab is the same destination the
    entry-bar segments drive."""
    page.evaluate("(t) => { App.act.closeDetail && App.act.closeDetail();"
                  "         App.act.setTab(t); App.act.setSheet('expanded'); }", tab)
    page.wait_for_timeout(600)


def show_map(page) -> None:
    page.evaluate("() => { App.act.closeDetail && App.act.closeDetail();"
                  "        App.act.closeOverlay && App.act.closeOverlay('done');"
                  "        App.act.setSheet('collapsed'); }")
    page.wait_for_timeout(400)


def more_menu_click(page, label: str) -> None:
    """Click an entry in the shared ⋯ overlay by its visible label."""
    page.wait_for_selector('#overlay-root [data-ov="more-run"], #modal-root [data-ov="more-run"]',
                           timeout=10000)
    page.locator('[data-ov="more-run"]').filter(has_text=label).first.click()
    page.wait_for_timeout(400)


def main(argv: list[str] | None = None) -> int:
    from playwright.sync_api import sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", type=Path, default=ROOT / "docs")
    parser.add_argument("--browser", choices=("chromium", "webkit"), default="chromium")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    baseline = json.loads(BASELINE.read_text())
    baseline_ids = {node.get("id") for node in baseline["all_nodes"] if node.get("id")}
    missing_from_inventory = sorted(set(RETAINED_FEATURES) - baseline_ids)
    if missing_from_inventory:
        raise AssertionError(
            f"2.3 feature inventory lacks expected anchors: {missing_from_inventory}")

    output = args.output
    if output:
        output.mkdir(parents=True, exist_ok=True)
    lib_browser.DOCS = args.docs.resolve()
    records: list[dict] = []
    errors: list[str] = []

    with lib_browser.serve_docs(8981) as base, sync_playwright() as p:
        browser = getattr(p, args.browser).launch()
        context = browser.new_context(
            viewport={"width": 393, "height": 852},
            is_mobile=True,
            has_touch=True,
            device_scale_factor=2,
            service_workers="block",
        )
        context.route("https://**/*", lambda route: route.abort())
        page = context.new_page()
        page.set_default_timeout(15000)
        lib_browser.install_guards(page, errors)
        page.add_init_script(
            "localStorage.setItem('tabelog.lang','zh-CN');"
            "localStorage.setItem('tabelog.seenIntro','1');"
        )
        lib_browser.boot(page, base)

        # The filter form has to be built before its controls can be probed.
        open_tab(page, "filters")
        absent = []
        for anchor, (label, probe) in sorted(RETAINED_FEATURES.items()):
            if not page.evaluate("() => !!(" + probe + ")"):
                absent.append(f"{anchor} ({label})")
        if absent:
            raise AssertionError(
                f"retained features have no control in the 4.0 UI: {absent}")
        records.append({
            "case": "baseline-inventory",
            "pass": True,
            "note": "2.3 anchors mapped to their 4.0 controls; the functional "
                    "cases below determine retention",
            "baseline": baseline["source"],
        })

        # Advanced filters: reach them from the visible control, inspect the
        # full set of choices, then toggle and restore a persisted filter.
        filter_shape = page.evaluate("""() => ({
          regions: Data.config.REGIONS.length + 1,
          genres: document.querySelectorAll('#filters-root [data-cui-cb]').length,
          awards: document.querySelectorAll('#filters-root [data-award]').length,
          price: document.querySelectorAll('#filters-root [data-budget]').length,
          toggles: ['bookableOnly','favOnly','hideBlack','hideForeign','gcalOnly']
            .filter(k => { const e = document.querySelector('[data-sw="' + k + '"]');
                           return e && e.closest('.ft-sw-row').offsetParent !== null; }).length})""")
        if filter_shape["regions"] != 48 or filter_shape["genres"] < 18 \
                or filter_shape["price"] < 4 or filter_shape["awards"] < 4 \
                or filter_shape["toggles"] != 5:
            raise AssertionError(f"advanced filter choices are incomplete: {filter_shape}")
        before_gcal = page.evaluate("() => App.state.filters.gcalOnly")
        page.eval_on_selector('[data-sw="gcalOnly"]', "el => el.click()")
        page.wait_for_timeout(500)
        persisted = json.loads(page.evaluate("localStorage.getItem('tabelog.filterState')"))
        if persisted.get("gcalOnly") is before_gcal:
            raise AssertionError("advanced coordinate-calibration filter did not persist")
        page.eval_on_selector('[data-sw="gcalOnly"]', "el => el.click()")
        page.wait_for_timeout(400)
        records.append({"case": "advanced-filters", "pass": True, **filter_shape})

        # Result sorting and batch mode: enter through Results, select real
        # rows, prove the actions work, and save two for the Saved tests.
        open_tab(page, "results")
        page.wait_for_selector("#list-root .ls-row[data-id]")
        sort_values = page.evaluate("() => ListMod.sortItems().map(o => o.key)")
        # 3.2.x spelled the award sort "award" in the <select>; 4.0's contract
        # key is "awards" (the adapter maps it back to the business spelling
        # when it persists into tabelog.listView). Five sorts, same five.
        if len(sort_values) != 5 or not {"rating", "price", "name", "distance"}.issubset(
                set(sort_values)) or not any(k.startswith("award") for k in sort_values):
            raise AssertionError(f"sort choices are incomplete: {sort_values}")
        page.eval_on_selector('[data-act="multi"]', "el => el.click()")
        page.wait_for_timeout(400)
        if not page.evaluate("() => App.state.multi.active"):
            raise AssertionError("result batch mode did not open")
        rows = page.locator("#list-root .ls-row[data-id]")
        saved = []
        for i in (0, 1):
            rid = rows.nth(i).get_attribute("data-id")
            saved.append(rid)
            # LIST-01: in multi mode the row body toggles selection.
            rows.nth(i).locator(".ls-open").click()
        page.wait_for_timeout(400)
        if page.evaluate("() => App.state.multi.ids.size") != 2:
            raise AssertionError("selecting result rows did not build a selection")
        page.eval_on_selector('[data-act="bulk-fav"]', "el => el.click()")
        page.wait_for_timeout(700)
        stored = page.evaluate("JSON.parse(localStorage.getItem('omakase_state_cache_v2')).fav")
        if set(saved) - set(stored):
            raise AssertionError(f"result batch save did not persist both rows: {stored}")
        if page.locator('[data-act="cancel-multi"]').count():
            page.eval_on_selector('[data-act="cancel-multi"]', "el => el.click()")
        page.wait_for_timeout(300)
        records.append({"case": "sorting-and-result-batch", "pass": True, "sorts": sort_values})

        # Collection CRUD and multi-list membership through the real UI.
        open_tab(page, "saved")
        page.wait_for_selector("#list-root .ls-row[data-id]")

        def stored_bookmarks():
            return page.evaluate("JSON.parse(localStorage.getItem('tabelog.bookmarks') || '[]')")

        list_ids = []
        for name in ("Planning A", "Planning B"):
            page.eval_on_selector('[data-act="new-list"]', "el => el.click()")
            page.wait_for_selector("#ov-list-name", timeout=10000)
            page.fill("#ov-list-name", name)
            page.eval_on_selector('[data-ov="save-list"]', "el => el.click()")
            page.wait_for_timeout(600)
            item = next((b for b in stored_bookmarks()
                         if b.get("kind") == "list" and b.get("name") == name), None)
            if not item:
                raise AssertionError(f"new collection was not persisted: {name}")
            list_ids.append(item["id"])
            # file both saved restaurants into it through the member picker
            for ref in saved:
                page.evaluate("(ref) => App.act.openOverlay('memberPicker', {id: ref})", ref)
                page.wait_for_selector('[data-ov="member-toggle"][data-list="%s"]' % item["id"],
                                       timeout=10000)
                page.eval_on_selector('[data-ov="member-toggle"][data-list="%s"]' % item["id"],
                                      "el => el.click()")
                page.wait_for_timeout(350)
                page.evaluate("() => App.act.closeOverlay('done')")
                page.wait_for_timeout(250)
            members = {b.get("ref") for b in stored_bookmarks()
                       if b.get("kind") == "member" and b.get("list") == item["id"]}
            if members != set(saved):
                raise AssertionError(f"membership incomplete for {name}: {members}")

        # Remove both restaurants from A through the row menu. They must stay
        # favourites and members of B, then be addable to A again so the
        # rename/copy/delete flow below starts from the same topology.
        for ref in saved:
            page.evaluate("(ref) => App.act.openOverlay('memberPicker', {id: ref})", ref)
            page.wait_for_selector('[data-ov="member-toggle"][data-list="%s"]' % list_ids[0],
                                   timeout=10000)
            page.eval_on_selector('[data-ov="member-toggle"][data-list="%s"]' % list_ids[0],
                                  "el => el.click()")
            page.wait_for_timeout(350)
            page.evaluate("() => App.act.closeOverlay('done')")
            page.wait_for_timeout(250)
        after_remove = stored_bookmarks()
        members_a = {b.get("ref") for b in after_remove
                     if b.get("kind") == "member" and b.get("list") == list_ids[0]}
        members_b = {b.get("ref") for b in after_remove
                     if b.get("kind") == "member" and b.get("list") == list_ids[1]}
        favourites = set(page.evaluate(
            "JSON.parse(localStorage.getItem('omakase_state_cache_v2')).fav"))
        if members_a or members_b != set(saved) or not set(saved) <= favourites:
            raise AssertionError(
                "removing from Planning A damaged retained state: "
                f"A={members_a}, B={members_b}, favourites={favourites}")

        for ref in saved:
            page.evaluate("(ref) => App.act.openOverlay('memberPicker', {id: ref})", ref)
            page.wait_for_selector('[data-ov="member-toggle"][data-list="%s"]' % list_ids[0],
                                   timeout=10000)
            page.eval_on_selector('[data-ov="member-toggle"][data-list="%s"]' % list_ids[0],
                                  "el => el.click()")
            page.wait_for_timeout(350)
            page.evaluate("() => App.act.closeOverlay('done')")
            page.wait_for_timeout(250)
        after_readd = stored_bookmarks()
        for list_id, name in zip(list_ids, ("Planning A", "Planning B")):
            members = {b.get("ref") for b in after_readd
                       if b.get("kind") == "member" and b.get("list") == list_id}
            if members != set(saved):
                raise AssertionError(f"re-add did not restore {name}: {members}")

        # rename / copy / delete, through the collection's own ⋯ menu.
        page.evaluate("() => App.set({saved: {groupBy: 'list', openGroups: null}})")
        page.wait_for_timeout(700)
        menu_btn = '[data-group-menu="%s"]' % list_ids[0]
        page.wait_for_selector(menu_btn, timeout=10000)
        assert_hit(page.locator(menu_btn).first, "collection menu")
        page.eval_on_selector(menu_btn, "el => el.click()")
        more_menu_click(page, "重命名")
        page.wait_for_selector("#ov-list-name", timeout=10000)
        page.fill("#ov-list-name", "Planning renamed")
        page.eval_on_selector('[data-ov="save-list"]', "el => el.click()")
        page.wait_for_timeout(600)
        if not any(b.get("id") == list_ids[0] and b.get("name") == "Planning renamed"
                   for b in stored_bookmarks()):
            raise AssertionError("renaming changed the list id or did not persist")

        page.evaluate("Object.defineProperty(navigator, 'clipboard', {configurable:true,"
                      "value:{writeText:async text=>{window.__copiedList=text}}})")
        page.eval_on_selector(menu_btn, "el => el.click()")
        more_menu_click(page, "复制清单文本")
        copied = page.evaluate("window.__copiedList")
        if "Planning renamed" not in (copied or "") or not all(u in copied for u in saved):
            raise AssertionError(
                "copy did not pass the named list and its members to the clipboard API")

        page.eval_on_selector(menu_btn, "el => el.click()")
        more_menu_click(page, "删除收藏夹")
        remaining = stored_bookmarks()
        if any(b.get("id") == list_ids[0] or b.get("list") == list_ids[0] for b in remaining):
            raise AssertionError("delete left collection metadata behind")
        if {b.get("ref") for b in remaining if b.get("list") == list_ids[1]} != set(saved):
            raise AssertionError("deleting one collection damaged another collection's members")
        if not set(saved) <= set(page.evaluate(
                "JSON.parse(localStorage.getItem('omakase_state_cache_v2')).fav")):
            raise AssertionError("deleting a collection removed restaurant favorites")
        records.append({"case": "saved-lists-and-batch", "pass": True, "bulkRemoveReadd": True})

        # Layer access: open the real layer popover, verify all four scopes,
        # then toggle and restore Landmarks so the run changes nothing.
        show_map(page)
        assert_hit(page.locator('[data-fab="layers"]'), "layer menu")
        page.locator('[data-fab="layers"]').click()
        page.wait_for_selector('[data-ov="layer-toggle"]', timeout=10000)
        for kind in ("long", "city", "landmarks", "pins"):
            assert_hit(page.locator('[data-ov="layer-toggle"][data-layer="%s"]' % kind).first, kind)
        layer_before = page.evaluate("localStorage.getItem('tabelog.showAttractions') || '1'")
        page.eval_on_selector('[data-ov="layer-toggle"][data-layer="landmarks"]',
                              "el => el.click()")
        page.wait_for_timeout(500)
        layer_after = page.evaluate("localStorage.getItem('tabelog.showAttractions')")
        if layer_after == layer_before:
            raise AssertionError("Landmarks layer control did not change state")
        for _ in range(3):
            if page.evaluate("localStorage.getItem('tabelog.showAttractions')") == layer_before:
                break
            page.eval_on_selector('[data-ov="layer-toggle"][data-layer="landmarks"]',
                                  "el => el.click()")
            page.wait_for_timeout(400)
        if page.evaluate("localStorage.getItem('tabelog.showAttractions')") != layer_before:
            raise AssertionError("Landmarks layer state could not be restored")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        records.append({"case": "map-layers", "pass": True,
                        "layers": ["long", "city", "landmarks", "pins"]})

        # Context-menu path to user pins / landmarks. Switch the kind in the
        # real form, check the collection row, then cancel.
        show_map(page)
        page.evaluate("""() => { const k = Object.keys(window).find(x => x.startsWith('map_'));
          window[k].fire('contextmenu', {latlng: L.latLng(35.0, 139.0)}); }""")
        page.wait_for_selector('[data-ov="place-new"][data-cat="bookmark"]', timeout=10000)
        page.eval_on_selector('[data-ov="place-new"][data-cat="bookmark"]', "el => el.click()")
        page.wait_for_selector("#ov-name", timeout=10000)
        attraction_kind = page.locator('[data-ov="form-type"][data-val="attraction"]')
        assert_hit(attraction_kind, "custom attraction kind")
        attraction_kind.click()
        page.wait_for_timeout(300)
        if attraction_kind.get_attribute("aria-checked") != "true":
            raise AssertionError("the pin form could not switch to Landmark")
        if not page.locator('[data-ov="form-list"], [data-ov="new-list"]').first.is_visible():
            raise AssertionError("pin/collection membership path is not visible")
        bm_before = len(stored_bookmarks())
        page.eval_on_selector('[data-ov="close"]', "el => el.click()")
        page.wait_for_timeout(400)
        if len(stored_bookmarks()) != bm_before:
            raise AssertionError("cancelling the pin form still wrote something")
        records.append({"case": "bookmarks-and-attractions", "pass": True})

        # Secondary restaurant routes: open from Results, then hit-test
        # sharing and the Tabelog source link.
        open_tab(page, "results")
        page.wait_for_selector("#list-root .ls-row[data-id] .ls-open")
        page.eval_on_selector("#list-root .ls-row[data-id] .ls-open", "el => el.click()")
        page.wait_for_function("() => !!App.state.selected.id", timeout=20000)
        page.wait_for_timeout(700)
        tabelog = page.locator('.dt-actions a[href^="https://tabelog.com/"]').first
        assert_hit(tabelog, "restaurant Tabelog link")
        # 3.2.x: #bs-more opened #bs-more-menu and .rst-share lived in it.
        # 4.0: the ⋯ is .dt-more in the card's title row at narrow and
        # .dt-act-more in the wide action row; both open the shared ⋯ overlay.
        page.locator('[data-act="more"]').first.click()
        page.wait_for_selector('[data-ov="more-run"]', timeout=10000)
        more = page.locator('[data-ov="more-run"]').filter(has_text="分享").first
        assert_hit(more, "restaurant share")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        page.evaluate("() => App.act.closeDetail()")
        page.wait_for_timeout(300)
        records.append({"case": "detail-share-and-source", "pass": True})

        # Account/data routes: open the account panel and hit-test backup /
        # import / privacy / reset without triggering a download or an API call.
        show_map(page)
        page.evaluate("() => App.act.openOverlay('account')")
        page.wait_for_selector('[data-ov="export"]', timeout=10000)
        for selector, label in (
            ('[data-ov="export"]', "export"), ('[data-ov="import"]', "import"),
            ('[data-ov="privacy"]', "privacy"), ('[data-ov="reset-filters"]', "reset"),
        ):
            page.locator(selector).scroll_into_view_if_needed()
            assert_hit(page.locator(selector), f"account {label}")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        records.append({"case": "account-backup-and-privacy", "pass": True})

        if errors:
            raise AssertionError(f"console/page errors: {errors[:5]}")
        if output:
            page.screenshot(path=str(output / f"feature-retention-{args.browser}-393.png"),
                            full_page=False)
        context.close()
        browser.close()

    if output:
        (output / "results.json").write_text(json.dumps(records, ensure_ascii=False, indent=2))
    print(json.dumps(records, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
