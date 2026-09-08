#!/usr/bin/env python3
"""Functional access-path check for features outside the 3.1 UX demo.

The 2.3 feature DOM inventory is used as a retention baseline, but an ID's
presence is never counted as a pass by itself. Each group below is opened
through a visible user control and at least one reversible interaction is
exercised. All external HTTPS requests are blocked.
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


def assert_hit(locator, label: str) -> None:
    if not locator.is_visible():
        raise AssertionError(f"{label} is not visible through its user path")
    locator.click(trial=True)


def main(argv: list[str] | None = None) -> int:
    from playwright.sync_api import sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", type=Path, default=ROOT / "docs")
    parser.add_argument("--browser", choices=("chromium", "webkit"), default="chromium")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    baseline = json.loads(BASELINE.read_text())
    baseline_ids = {node.get("id") for node in baseline["all_nodes"] if node.get("id")}
    required_baseline = {
        "ff-region", "ff-rating", "ff-bookable-only", "ff-gcal-only",
        "wb-list", "wb-fav", "fab-layers", "fab-transit-long",
        "fab-transit-city", "fab-attractions", "fab-bookmarks", "bm-modal",
        "ss-avatar", "ss-menu", "ssm-export", "ssm-import",
    }
    missing_from_inventory = sorted(required_baseline - baseline_ids)
    if missing_from_inventory:
        raise AssertionError(f"2.3 feature inventory lacks expected anchors: {missing_from_inventory}")

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

        current_ids = set(page.eval_on_selector_all("[id]", "els => els.map(e => e.id)"))
        missing_current = sorted(required_baseline - current_ids)
        if missing_current:
            raise AssertionError(f"retained feature anchors missing from current DOM: {missing_current}")
        records.append({
            "case": "baseline-inventory",
            "pass": True,
            "note": "anchors only; functional cases below determine retention",
            "baseline": baseline["source"],
        })

        # Advanced filters: reach them from the 3.1 control, inspect the full
        # choices, then toggle and restore a persisted non-demo filter.
        page.locator('[data-ux-tab="filter"]').click()
        page.wait_for_selector("#ff-sheet-content:visible")
        filter_shape = page.evaluate("""() => ({
          regions:document.querySelectorAll('#ff-region option').length,
          genres:document.querySelectorAll('#ff-genre-box input[type=checkbox]').length,
          awards:document.querySelectorAll('#ff-sheet-content input[name="ff-award"]').length,
          price:document.querySelectorAll('#ff-sheet-content input[name="ff-price"]').length,
          toggles:['ff-bookable-only','ff-only-fav','ff-hide-black','ff-hide-foreign','ff-gcal-only']
            .filter(id=>document.getElementById(id)?.offsetParent!==null).length})""")
        if filter_shape["regions"] != 48 or filter_shape["genres"] < 18 \
                or filter_shape["price"] < 4 or filter_shape["awards"] < 4 \
                or filter_shape["toggles"] != 5:
            raise AssertionError(f"advanced filter choices are incomplete: {filter_shape}")
        gcal = page.locator("#ff-gcal-only")
        before_gcal = gcal.is_checked()
        gcal.click()
        persisted = json.loads(page.evaluate("localStorage.getItem('tabelog.filterState')"))
        if persisted.get("gcalOnly") is before_gcal:
            raise AssertionError("advanced coordinate-calibration filter did not persist")
        gcal.click()
        records.append({"case": "advanced-filters", "pass": True, **filter_shape})

        # Result sorting and batch mode: enter through Results, select a real
        # row, prove actions enable, then cancel. Save one row for Saved tests.
        page.locator('[data-ux-tab="results"]').click()
        page.wait_for_selector("#wb-list .wb-row")
        sort_values = page.locator("#wb-sort option").evaluate_all("els => els.map(e=>e.value)")
        if not {"rating", "price", "award", "name", "distance"}.issubset(set(sort_values)):
            raise AssertionError(f"sort choices are incomplete: {sort_values}")
        page.locator("#wb-select-btn").click()
        if not page.locator("#wb-bulk").is_visible():
            raise AssertionError("result batch toolbar did not open")
        page.locator("#wb-list .wb-row").first.click()
        if page.locator("#wb-bulk-fav").is_disabled() or page.locator("#wb-bulk-black").is_disabled():
            raise AssertionError("result batch actions stayed disabled after selecting a row")
        page.locator("#wb-list .wb-row").nth(1).click()
        page.locator("#wb-bulk-fav").click()
        saved = page.evaluate("JSON.parse(localStorage.getItem('omakase_state_cache_v2')).fav")
        if len(saved) != 2:
            raise AssertionError(f"result batch save did not persist both rows: {saved}")
        records.append({"case": "sorting-and-result-batch", "pass": True, "sorts": sort_values})

        # Exercise list CRUD and multi-list membership through the actual UI.
        page.locator('[data-ux-tab="fav"]').click()
        page.wait_for_selector("#fv-body .wb-row")
        def stored_bookmarks():
            return page.evaluate("JSON.parse(localStorage.getItem('tabelog.bookmarks') || '[]')")

        list_ids = []
        for name in ("Planning A", "Planning B"):
            page.locator("#fv-new").click()
            page.locator("#fl-name").fill(name)
            page.locator("#fl-modal .fl-save").click()
            item = next((b for b in stored_bookmarks() if b.get("kind") == "list" and b.get("name") == name), None)
            if not item:
                raise AssertionError(f"new collection was not persisted: {name}")
            list_ids.append(item["id"])
            page.locator("#fv-group").select_option("city")
            page.locator("#fv-select").click()
            for index in (0, 1):
                page.locator("#fv-body .wb-row").nth(index).click()
            page.locator("#fv-bulk-move").click()
            page.locator("#fv-menu .fv-mi").filter(has_text=name).click()
            members = {b.get("ref") for b in stored_bookmarks() if b.get("kind") == "member" and b.get("list") == item["id"]}
            if members != set(saved):
                raise AssertionError(f"batch membership incomplete for {name}: {members}")

        # Remove both restaurants from A through the real bulk menu. They
        # must remain favourites and members of B, then be addable to A again
        # so the rename/copy/delete flow below starts from the same topology.
        page.locator("#fv-group").select_option("city")
        page.locator("#fv-select").click()
        for index in (0, 1):
            page.locator("#fv-body .wb-row").nth(index).click()
        page.locator("#fv-bulk-out").click()
        page.locator("#fv-menu .fv-mi").filter(has_text="Planning A").click()
        after_remove = stored_bookmarks()
        members_a = {b.get("ref") for b in after_remove if b.get("kind") == "member" and b.get("list") == list_ids[0]}
        members_b = {b.get("ref") for b in after_remove if b.get("kind") == "member" and b.get("list") == list_ids[1]}
        favourites = set(page.evaluate("JSON.parse(localStorage.getItem('omakase_state_cache_v2')).fav"))
        if members_a or members_b != set(saved) or favourites != set(saved):
            raise AssertionError(
                "batch remove from Planning A damaged retained state: "
                f"A={members_a}, B={members_b}, favourites={favourites}")

        page.locator("#fv-select").click()
        for index in (0, 1):
            page.locator("#fv-body .wb-row").nth(index).click()
        page.locator("#fv-bulk-move").click()
        page.locator("#fv-menu .fv-mi").filter(has_text="Planning A").click()
        after_readd = stored_bookmarks()
        for list_id, name in zip(list_ids, ("Planning A", "Planning B")):
            members = {b.get("ref") for b in after_readd if b.get("kind") == "member" and b.get("list") == list_id}
            if members != set(saved):
                raise AssertionError(f"batch re-add did not restore {name}: {members}")

        page.locator("#fv-group").select_option("list")
        group = page.locator(f'.fv-grp[data-list="{list_ids[0]}"]')
        group.locator(".fv-grp-h .fv-more").click()
        page.locator("#fv-menu .fv-mi").filter(has_text="重命名").click()
        page.locator("#fl-name").fill("Planning renamed")
        page.locator("#fl-modal .fl-save").click()
        if not any(b.get("id") == list_ids[0] and b.get("name") == "Planning renamed" for b in stored_bookmarks()):
            raise AssertionError("renaming changed the list id or did not persist")
        page.evaluate("Object.defineProperty(navigator, 'clipboard', {configurable:true,value:{writeText:async text=>{window.__copiedList=text}}})")
        group.locator(".fv-grp-h .fv-more").click()
        page.locator("#fv-menu .fv-mi").filter(has_text="复制清单文本").click()
        copied = page.evaluate("window.__copiedList")
        if "Planning renamed" not in (copied or "") or not all(url in copied for url in saved):
            raise AssertionError("copy did not pass the named list and its members to the clipboard API")
        group.locator(".fv-grp-h .fv-more").click()
        page.locator("#fv-menu .fv-mi.danger").click()
        remaining = stored_bookmarks()
        if any(b.get("id") == list_ids[0] or b.get("list") == list_ids[0] for b in remaining):
            raise AssertionError("delete left collection metadata behind")
        if {b.get("ref") for b in remaining if b.get("list") == list_ids[1]} != set(saved):
            raise AssertionError("deleting one collection damaged another collection's members")
        if set(page.evaluate("JSON.parse(localStorage.getItem('omakase_state_cache_v2')).fav")) != set(saved):
            raise AssertionError("deleting a collection removed restaurant favorites")
        records.append({"case": "saved-lists-and-batch", "pass": True,
                        "bulkRemoveReadd": True})

        # Layer access: open the real layer menu, verify all four scopes, then
        # toggle and restore Attractions so the run leaves no changed state.
        page.locator('[data-ux-tab="map"]').click()
        assert_hit(page.locator("#fab-layers"), "layer menu")
        page.locator("#fab-layers").click()
        layer_ids = ("fab-transit-long", "fab-transit-city", "fab-attractions", "fab-bookmarks")
        for layer_id in layer_ids:
            assert_hit(page.locator(f"#{layer_id}"), layer_id)
        attractions = page.locator("#fab-attractions")
        layer_before = page.evaluate("localStorage.getItem('tabelog.showAttractions') || '1'")
        attractions.click()
        layer_after = page.evaluate("localStorage.getItem('tabelog.showAttractions')")
        if layer_after == layer_before:
            raise AssertionError("Attractions layer control did not change state")
        for _ in range(3):
            if page.evaluate("localStorage.getItem('tabelog.showAttractions')") == layer_before:
                break
            attractions.click()
        if page.evaluate("localStorage.getItem('tabelog.showAttractions')") != layer_before:
            raise AssertionError("Attractions layer state could not be restored")
        page.keyboard.press("Escape")
        records.append({"case": "map-layers", "pass": True, "layers": list(layer_ids)})

        # Context-menu path to user bookmarks/attractions. Switch the kind in
        # the actual modal, inspect list membership, then cancel.
        page.mouse.click(196, 360, button="right")
        page.wait_for_selector("#ff-add-bm")
        page.locator("#ff-add-bm").click()
        page.wait_for_selector("#bm-modal.bm-open")
        attraction_kind = page.locator('#bm-modal [data-kind="attraction"]')
        assert_hit(attraction_kind, "custom attraction kind")
        attraction_kind.click()
        if attraction_kind.get_attribute("aria-checked") != "true":
            raise AssertionError("bookmark modal could not switch to Attraction")
        if not page.locator("#bm-lists").is_visible():
            raise AssertionError("bookmark/list membership path is not visible")
        page.locator("#bm-modal .bm-cancel").click()
        records.append({"case": "bookmarks-and-attractions", "pass": True})

        # Secondary restaurant routes: open from Results, scroll the detail
        # body to its actions, and hit-test sharing and Tabelog navigation.
        page.locator('[data-ux-tab="results"]').click()
        page.locator("#wb-list .wb-row").first.click()
        page.wait_for_selector("#bs-sheet.bs-open")
        page.locator("#bs-content").evaluate("e => e.scrollTop = e.scrollHeight")
        share = page.locator("#bs-content .rst-share")
        tabelog = page.locator("#bs-content .rst-tabelog")
        assert_hit(share, "restaurant share")
        assert_hit(tabelog, "restaurant Tabelog link")
        page.locator("#ux-detail-back").click()
        records.append({"case": "detail-share-and-source", "pass": True})

        # Account/data routes: actually open the account panel and hit-test
        # backup/import/privacy controls without triggering a download or API.
        page.locator('[data-ux-tab="map"]').click()
        page.locator("#ss-avatar").click()
        page.wait_for_selector("#ss-menu.open")
        for selector, label in (
            ("#ssm-export", "export"), ("#ssm-import", "import"),
            ("#ssm-privacy-link", "privacy"), ("#ssm-reset", "reset"),
        ):
            page.locator(selector).scroll_into_view_if_needed()
            assert_hit(page.locator(selector), f"account {label}")
        records.append({"case": "account-backup-and-privacy", "pass": True})

        if errors:
            raise AssertionError(f"console/page errors: {errors[:5]}")
        if output:
            page.screenshot(path=str(output / f"feature-retention-{args.browser}-393.png"), full_page=False)
        context.close()
        browser.close()

    if output:
        (output / "results.json").write_text(json.dumps(records, ensure_ascii=False, indent=2))
    print(json.dumps(records, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
