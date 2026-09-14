"""Region picker (4.2.3): several regions at once, group select-all, Tokyo on
its own ahead of Hokkaido with whole-Tokyo and its 25 districts, search,
persistence (including the states older builds wrote), nearby round trip, the
detail card's neighbourhood, and four languages.

    uv run python tests/ux/region_picker.py
    uv run python tests/ux/region_picker.py --browser webkit

External requests are blocked. Screenshots land in --output.
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import lib_browser  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--browser", choices=["chromium", "webkit"], default="chromium")
parser.add_argument("--output", type=Path, default=ROOT / "audit_outputs" / "4.2.3" / "region-picker")
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)

VIEWPORTS = [
    ("desktop", dict(viewport={"width": 1440, "height": 900})),
    ("iphone", dict(viewport={"width": 402, "height": 874}, device_scale_factor=3, is_mobile=True, has_touch=True)),
    ("fold-cover", dict(viewport={"width": 416, "height": 657}, device_scale_factor=3, is_mobile=True, has_touch=True)),
]
# WebKit has no controllable geolocation, so the page's own API answers from
# Shibuya (the same approach as nearby_mode.py).
GEO = """(() => {
  Object.defineProperty(navigator, 'geolocation', {configurable: true, value: {
    getCurrentPosition(ok) { setTimeout(() => ok({coords: {latitude: 35.6589, longitude: 139.6989, accuracy: 12}, timestamp: Date.now()}), 50); }
  }});
})();"""
# A reload saves the live filters on pagehide, so a state planted before the
# reload would be overwritten. This plants it on the next load instead, once.
PLANT = """(() => { try {
  const v = sessionStorage.getItem('test.plantFilterState');
  if (v !== null) { localStorage.setItem('tabelog.filterState', v); sessionStorage.removeItem('test.plantFilterState'); }
} catch (_) {} })();"""
BODY_JS = """() => {
  const body = document.querySelector('#ov-pop .ov-pop-body');
  if (!body) return null;
  return [...body.children].map(n => ({
    head: n.classList.contains('ov-rg-head'), hidden: n.hidden,
    text: (n.querySelector('.ov-rg-head-t, .ov-rg-name') || n).textContent.trim(),
    ov: n.dataset.ov || null, code: n.dataset.code === undefined ? null : n.dataset.code,
    zone: n.dataset.zone || null, checked: n.getAttribute('aria-checked'),
    all: n.querySelector('.ov-rg-all') ? { checked: n.querySelector('.ov-rg-all').getAttribute('aria-checked'),
      text: n.querySelector('.ov-rg-all').textContent, list: n.querySelector('.ov-rg-all').dataset.list } : null,
    h: n.getBoundingClientRect().height, right: n.getBoundingClientRect().right
  }));
}"""
SEL = "() => ({ regions: Array.from(App.state.filters.regions).sort((a, b) => a - b), areas: Array.from(App.state.filters.areas).sort(), region: App.state.filters.region })"


def check(cond, what):
    if not cond:
        raise AssertionError(what)


def open_picker(page):
    page.evaluate("() => App.act.openOverlay('regionPicker')")
    page.wait_for_selector("#ov-pop .ov-pop-body", timeout=5000)
    page.wait_for_timeout(250)


def rows(page):
    return page.evaluate(BODY_JS)


def visible(rs):
    return [r for r in rs if not r["hidden"]]


def reload(page):
    """Reload once the background popups.json download has finished: WebKit
    reports a fetch cut off by the reload as a console error, which is an
    artefact of the test, not of the page."""
    page.evaluate("async () => { try { await Business.loadPopups(); } catch (e) { /* reported by the page */ } }")
    lib_browser.reload_and_wait(page)


def attr(page, selector, name="aria-checked"):
    return page.get_attribute(selector, name)


def run_viewport(browser, base, name, ctx_opts):
    errors = []
    context = browser.new_context(service_workers="block", **ctx_opts)
    page = context.new_page()
    page.add_init_script(GEO)
    page.add_init_script(PLANT)
    lib_browser.install_guards(page, errors)
    page.route("https://**/*", lambda r: r.abort())
    lib_browser.seed_local_storage(page, {"tabelog.lang": "zh-CN", "tabelog.seenIntro": "1"})
    lib_browser.boot(page, base)
    width = ctx_opts["viewport"]["width"]
    shot = lambda tag: page.screenshot(path=str(args.output / f"{name}-{args.browser}-{tag}.png"))

    # 1. order: all regions, Tokyo (whole + districts) ahead of Hokkaido, Tokyo out of Kanto
    open_picker(page)
    rs = rows(page)
    vis = visible(rs)
    check(vis[0]["ov"] == "region-all" and vis[0]["checked"] == "true", f"first row is all regions, checked: {vis[0]}")
    check(vis[1]["head"] and vis[1]["text"] == "东京" and vis[1]["all"] is None, f"then the Tokyo head: {vis[1]}")
    check(vis[2]["code"] == "13" and vis[2]["text"] == "东京都（全部）", f"then whole Tokyo: {vis[2]}")
    check(vis[3]["ov"] == "region-tokyo" and "25" in vis[3]["text"], f"then the district page: {vis[3]}")
    check(vis[4]["head"] and vis[4]["text"] == "北海道·东北" and vis[4]["all"], f"then Hokkaido with select-all: {vis[4]}")
    check(len([r for r in rs if r["ov"] == "region" and r["code"] == "13"]) == 1, "Tokyo is listed once")
    kanto = next(r for r in rs if r["head"] and r["text"] == "关东")
    check(kanto["all"]["list"] == "7,8,9,10,11,12", f"Kanto's select-all leaves Tokyo out: {kanto['all']}")
    check(all(r["hidden"] for r in rs if r["ov"] == "area"), "district rows are search-only on the root page")
    check(all(r["h"] >= 43.5 for r in vis if not r["head"]), "rows keep the touch minimum")
    check(page.is_visible(".ov-rg-done"), "Done button")
    shot("root")

    # 2. search reaches districts by neighbourhood in any language
    for q, zone in [("中目黒", "nakameguro"), ("shimokitazawa", "setagaya"), ("台场", "tsukiji"), ("代代木上原", "shibuya")]:
        page.fill("#ov-region-q", q)
        page.wait_for_timeout(120)
        vr = visible(rows(page))
        check(zone in [r["zone"] for r in vr if r["ov"] == "area"], f"search {q!r} shows {zone}")
        check(not any(r["ov"] == "region-tokyo" for r in vr), "the district-page row hides while searching")
    page.fill("#ov-region-q", "zzzz-nothing")
    page.wait_for_timeout(120)
    check(page.is_visible("[data-rg-empty]"), "empty note")
    page.fill("#ov-region-q", "")
    page.wait_for_timeout(120)

    # 3. several prefectures, the picker stays open; group select-all toggles a block
    page.click('[data-ov="region"][data-code="26"]')
    page.click('[data-ov="region"][data-code="25"]')
    page.wait_for_timeout(200)
    check(page.evaluate(SEL) == {"regions": [25, 26], "areas": [], "region": None}, f"Osaka + Kyoto: {page.evaluate(SEL)}")
    check(page.evaluate("() => App.state.overlay.kind") == "regionPicker", "picker stays open while picking")
    check(attr(page, '[data-ov="region"][data-code="26"]') == "true" and attr(page, '[data-ov="region-all"]') == "false", "checks repaint in place")
    n = page.evaluate("() => Data.M(App.state).length")
    check(str(n) in page.text_content(".ov-rg-done").replace(",", ""), f"Done shows {n}")
    kinki = '[data-ov="region-group"][data-list="23,24,25,26,27,28,29"]'
    check(attr(page, kinki) == "mixed", "Kinki select-all is mixed")
    page.click(kinki)
    page.wait_for_timeout(150)
    check(page.evaluate(SEL)["regions"] == [23, 24, 25, 26, 27, 28, 29] and attr(page, kinki) == "true", "Kinki all")
    check(page.text_content(kinki) == "全清", "select-all turns into clear")
    page.click(kinki)
    page.wait_for_timeout(150)
    check(page.evaluate(SEL) == {"regions": [], "areas": [], "region": None}, "Kinki cleared")
    check(attr(page, '[data-ov="region-all"]') == "true", "nothing picked = all regions")

    # 4. whole Tokyo vs its districts
    page.click('[data-ov="region"][data-code="13"]')
    page.wait_for_timeout(150)
    check(page.evaluate(SEL) == {"regions": [13], "areas": [], "region": 13}, "whole Tokyo")
    page.click('[data-ov="region-tokyo"]')
    page.wait_for_selector(".ov-pop-back", timeout=3000)
    page.wait_for_timeout(250)
    rs = rows(page)
    check(rs[0]["code"] == "13" and sum(1 for r in rs if r["head"]) == 6, "district page: whole Tokyo, 6 groups")
    zones = [r for r in visible(rs) if r["ov"] == "area"]
    check(len(zones) == 25 and all(r["checked"] == "true" for r in zones), "whole Tokyo checks every district")
    check(max(r["right"] for r in visible(rs)) <= width + 1, "rows fit the viewport")
    check(page.evaluate("() => document.activeElement && document.activeElement.classList.contains('ov-pop-back')"), "back takes focus")
    page.click('[data-ov="area"][data-zone="shibuya"]')
    page.wait_for_timeout(150)
    st = page.evaluate(SEL)
    check(st["regions"] == [] and len(st["areas"]) == 24 and "shibuya" not in st["areas"] and st["region"] == 13, f"unpicking one district: {st}")
    check(attr(page, '[data-ov="region"][data-code="13"]') == "mixed", "whole Tokyo shows mixed")
    page.click('[data-ov="area"][data-zone="shibuya"]')
    page.wait_for_timeout(150)
    check(page.evaluate(SEL) == {"regions": [13], "areas": [], "region": 13}, "all 25 collapse back into whole Tokyo")
    page.click('[data-ov="region"][data-code="13"]')
    jonan = '[data-ov="area-group"][data-list="meguro,nakameguro,setagaya,kamata"]'
    page.click(jonan)
    page.click('[data-ov="area"][data-zone="shibuya"]')
    page.wait_for_timeout(200)
    check(page.evaluate(SEL) == {"regions": [], "areas": ["kamata", "meguro", "nakameguro", "setagaya", "shibuya"], "region": 13},
          f"a district group plus one more: {page.evaluate(SEL)}")
    check(attr(page, jonan) == "true", "the group's select-all is on")
    shot("districts")

    # 5. Done: results are exactly those districts, the map goes there, it is saved
    page.click(".ov-rg-done")
    page.wait_for_timeout(1500)
    st = page.evaluate("""() => { const ids = Data.M(App.state), f = App.state.filters, c = Data.counts(f), b = MapMod.map.getBounds();
      return { open: App.state.overlay.kind, n: ids.length, expected: Data.scopeCount(f, c),
               allIn: ids.every(id => f.areas.has(Data.zoneOf(Data.byId(id)))),
               inView: ids.filter(id => { const r = Data.byId(id); return b.contains([r.lat, r.lon]); }).length,
               saved: JSON.parse(localStorage.getItem('tabelog.filterState')),
               label: Data.scopeName(f, 'zh', true), chip: (document.querySelector('[data-scope-picker]') || {}).textContent || '' }; }""")
    check(st["open"] is None and st["n"] == st["expected"] and st["n"] > 0 and st["allIn"], f"results: {st}")
    check(st["inView"] >= st["n"] * 0.8, f"map moved to the selection ({st['inView']}/{st['n']})")
    check(st["saved"]["regions"] == [] and st["saved"]["areas"] == ["kamata", "meguro", "nakameguro", "setagaya", "shibuya"]
          and st["saved"]["region"] == 13, f"saved: {st['saved']}")
    check(st["label"] == "东京都 · 涩谷・惠比寿・代官山 等 5 个地区", f"label: {st['label']}")
    check("等 5 个地区" in st["chip"], f"scope chip: {st['chip']!r}")

    # 6. reload keeps it; reopening lands on the district page
    reload(page)
    check(page.evaluate(SEL)["areas"] == ["kamata", "meguro", "nakameguro", "setagaya", "shibuya"], "restored after reload")
    open_picker(page)
    check(page.is_visible(".ov-pop-back"), "reopening with districts picked lands on the district page")
    page.click(".ov-pop-back")
    page.wait_for_timeout(200)
    check(page.is_visible('[data-ov="region-tokyo"]') and "5" in page.text_content('[data-ov="region-tokyo"] .ov-rg-sub'), "back to root, district summary shown")
    page.evaluate("() => App.act.closeOverlay('cancel')")
    page.wait_for_timeout(200)

    # 7. mixing a prefecture in; the single-value view goes null (a 4.2.2 tab then shows all)
    page.evaluate("() => App.act.applyFilters(Data.toggleRegionPatch(App.state.filters, 26))")
    check(page.evaluate(SEL)["region"] is None and page.evaluate(SEL)["regions"] == [26], "district + prefecture")
    check(json.loads(page.evaluate("() => localStorage.getItem('tabelog.filterState')"))["region"] is None, "stored region is the superset")
    # a bare {region} patch (older callers) means exactly that prefecture
    check(page.evaluate("() => { App.act.applyFilters({ region: 25 }); return [Array.from(App.state.filters.regions), Array.from(App.state.filters.areas), App.state.filters.region]; }")
          == [[25], [], 25], "legacy {region} patch")

    # 8. states written by older builds, and hostile ones
    for stored, expect in [
        ({"rating": 3.4, "region": 13}, {"regions": [13], "areas": [], "region": 13}),
        ({"rating": 3.4, "region": 26, "regions": [13], "areas": ["shibuya"]}, {"regions": [13], "areas": [], "region": 13}),
        ({"rating": 3.4, "region": None, "regions": [99, "x", 3], "areas": ["nope", 7, "ginza"]}, {"regions": [3], "areas": ["ginza"], "region": None}),
        ({"rating": 3.4, "regions": "13", "areas": {}}, {"regions": [], "areas": [], "region": None}),
    ]:
        page.evaluate("s => sessionStorage.setItem('test.plantFilterState', JSON.stringify(s))", stored)
        reload(page)
        check(page.evaluate(SEL) == expect, f"stored {stored} -> {page.evaluate(SEL)}, expected {expect}")
        check(page.evaluate("() => Data.M(App.state).length") > 0, f"stored {stored} leaves results")

    # 9. nearby clears the selection and gives it back
    page.evaluate("() => App.act.applyFilters({ regions: new Set([26]), areas: new Set(['shibuya']) })")
    before = page.evaluate(SEL)
    page.evaluate("() => App.act.toggleNearby()")
    page.wait_for_function("() => App.state.nearby.active", timeout=15000)
    check(page.evaluate(SEL) == {"regions": [], "areas": [], "region": None}, "nearby clears the selection")
    page.evaluate("() => App.act.exitNearby()")
    page.wait_for_function("() => !App.state.nearby.active", timeout=5000)
    check(page.evaluate(SEL) == before, f"exiting nearby restores {before}")

    # 10. the detail card names the neighbourhood
    page.evaluate("() => App.act.applyFilters({ regions: new Set(), areas: new Set(['shibuya']) })")
    rid = page.evaluate("() => Data.M(App.state)[0]")
    page.evaluate("id => App.act.openDetail(id, 'search')", rid)
    page.wait_for_selector("#detail-root .dt-hood", timeout=15000)
    check(page.text_content("#detail-root .dt-hood") == page.evaluate("id => Data.neighborhoodName(Data.byId(id), 'zh')", rid), "detail neighbourhood")
    page.evaluate("() => App.act.closeDetail && App.act.closeDetail()")

    # 11. languages
    page.evaluate("() => App.act.applyFilters({ regions: new Set([26]), areas: new Set(['shibuya']) })")
    for lang in ["en", "ja", "tw"]:
        page.evaluate("l => App.i18n.setLang(l)", lang)
        page.wait_for_timeout(200)
        open_picker(page)
        page.click('[data-ov="region-tokyo"]')
        page.wait_for_selector(".ov-pop-back", timeout=3000)
        page.wait_for_timeout(200)
        names = [r["text"] for r in visible(rows(page)) if r["ov"] == "area"]
        check(len(names) == 25, f"{lang}: 25 districts")
        if lang == "en":
            check(not any(re.search(r"[぀-ヿ㐀-鿿]", x) for x in names), "English district names carry no CJK")
            check(page.evaluate("() => Data.scopeName(App.state.filters, 'en')") == "Osaka + 1 more", "English multi label")
        shot(f"districts-{lang}")
        page.evaluate("() => App.act.closeOverlay('cancel')")
        page.wait_for_timeout(150)
    page.evaluate("() => App.i18n.setLang('zh')")

    check(not errors, f"console/page errors: {errors[:5]}")
    context.close()


def main():
    with lib_browser.serve_docs(8995) as base, sync_playwright() as p:
        browser = getattr(p, args.browser).launch()
        for name, opts in VIEWPORTS:
            run_viewport(browser, base, name, opts)
            print(f"  ok    {name} ({args.browser})")
        browser.close()
    print(f"region picker: {len(VIEWPORTS)} viewports passed on {args.browser}")


if __name__ == "__main__":
    main()
