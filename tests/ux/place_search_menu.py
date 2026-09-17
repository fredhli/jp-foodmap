"""Place search opens the place menu for every kind of result (4.3.7 fix).

Picking a Nominatim row used to open 新建书签 / 新建景点 / 复制坐标 only when
the result's type landed at z16. 江之浦 in Odawara is an OSM
place=neighbourhood (z15), so it flew the map and stopped; the bus stop of the
same name in Isahaya (z16) got the menu. The rows below are the real
2026-09-17 Nominatim answers for 江之浦 and 小田原市, trimmed.

    uv run python tests/ux/place_search_menu.py
    uv run python tests/ux/place_search_menu.py --browser webkit
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import lib_browser  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--browser", choices=["chromium", "webkit"], default="chromium")
parser.add_argument("--shots", type=Path, default=None, help="write a screenshot per case here")
args = parser.parse_args()

VIEWPORTS = [("fold-outer", 416, 657, True), ("iphone", 402, 874, True),
             ("fold-inner", 816, 616, True), ("desktop", 1440, 900, False)]

PLACES = [
    {"osm_type": "node", "osm_id": 8670915767, "lat": "35.1873260", "lon": "139.1292320",
     "category": "place", "type": "neighbourhood", "place_rank": 24, "addresstype": "neighbourhood",
     "name": "江之浦", "display_name": "江之浦, 小田原市, 神奈川縣, 250-0024, 日本",
     "namedetails": {"name": "江之浦", "name:en": "Enoura", "name:ja": "江之浦"},
     "boundingbox": ["35.1773260", "35.1973260", "139.1192320", "139.1392320"]},
    {"osm_type": "node", "osm_id": 12195041555, "lat": "32.7697613", "lon": "130.0258997",
     "category": "highway", "type": "bus_stop", "place_rank": 30, "addresstype": "highway",
     "name": "江之浦", "display_name": "江之浦, 諫早飯盛線, 飯盛町下釜, 谏早市, 长崎县, 日本",
     "namedetails": {"name": "江の浦", "name:ja": "江の浦", "name:zh": "江之浦"},
     "boundingbox": ["32.7697113", "32.7698113", "130.0258497", "130.0259497"]},
    {"osm_type": "relation", "osm_id": 2689427, "lat": "35.2645301", "lon": "139.1522460",
     "category": "boundary", "type": "administrative", "place_rank": 16, "addresstype": "city",
     "name": "小田原市", "display_name": "小田原市, 神奈川縣, 日本",
     "namedetails": {"name": "小田原市", "name:en": "Odawara", "name:ja": "小田原市", "name:zh": "小田原市"},
     "boundingbox": ["35.1779060", "35.3299200", "139.0589120", "139.2940787"]},
]
CASES = [("neighbourhood", PLACES[0]), ("bus_stop", PLACES[1]), ("city", PLACES[2])]
MAP = "window[Object.keys(window).find(k=>/^map_[a-f0-9]/.test(k)&&window[k].fire)]"


def check(cond, what):
    if not cond:
        raise AssertionError(what)


def pick_place(page, place):
    page.evaluate("() => { App.act.closeOverlay && App.act.closeOverlay(); Overlays.focusSearch(); }")
    page.wait_for_selector("#ov-sinput")
    page.fill("#ov-sinput", "")
    page.fill("#ov-sinput", "江之浦")
    lat, lon = float(place["lat"]), float(place["lon"])
    idx = page.wait_for_function(
        """([lat, lon]) => { const i = Overlays.options().findIndex(o => o.type === 'place' && o.place.lat === lat && o.place.lon === lon);
             return i >= 0 && document.getElementById('ov-sg-' + i) ? { i } : false; }""",
        arg=[lat, lon], timeout=15000).json_value()["i"]
    page.locator(f"#ov-sg-{idx}").click()
    return lat, lon


def main():
    with lib_browser.serve_docs(8997) as base, sync_playwright() as p:
        browser = getattr(p, args.browser).launch()
        for name, w, h, touch in VIEWPORTS:
            errors = []
            ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=touch and args.browser == "chromium",
                                      has_touch=touch, service_workers="block")
            page = ctx.new_page()
            lib_browser.install_guards(page, errors)
            # Registered after the guards, so it wins over their Nominatim abort.
            page.route("**nominatim.openstreetmap.org/**", lambda r: r.fulfill(
                status=200, content_type="application/json", body=json.dumps(PLACES, ensure_ascii=False)))
            lib_browser.seed_local_storage(page, {"tabelog.seenIntro": "1", "tabelog.lang": "zh-CN"})
            lib_browser.boot(page, base)
            for case, place in CASES:
                lat, lon = pick_place(page, place)
                page.wait_for_timeout(1600)          # flight + render
                menu = page.evaluate("""() => { const s = App.state, b = document.querySelector('[data-ov="place-new"][data-cat="bookmark"]');
                    return { kind: s.overlay.kind, payload: s.overlay.payload, searchActive: s.search.active,
                             button: !!(b && b.getClientRects().length) }; }""")
                check(menu["kind"] == "placeMenu" and menu["button"],
                      f"{name}/{case}: picking the place opens the place menu: {menu}")
                pl = menu["payload"] or {}
                check(abs(pl.get("lat", 0) - lat) < 1e-9 and abs(pl.get("lon", 0) - lon) < 1e-9,
                      f"{name}/{case}: the menu is for the picked coordinates: {pl}")
                check(page.evaluate(MAP + f".getBounds().contains([{lat},{lon}])"),
                      f"{name}/{case}: the picked place is on the map")
                if args.shots:
                    args.shots.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(args.shots / f"{args.browser}-{name}-{case}.png"))
                print(f"  ok    {name}/{case}")
            # The menu still leads somewhere: 新建书签 on the neighbourhood.
            pick_place(page, PLACES[0])
            page.wait_for_timeout(1600)
            page.locator('[data-ov="place-new"][data-cat="bookmark"]').click()
            page.wait_for_selector("#ov-name", timeout=8000)
            check(not errors, f"{name}: console/page errors: {errors[:5]}")
            ctx.close()
        browser.close()
    print(f"place search menu: {len(VIEWPORTS)} viewports x {len(CASES)} kinds passed on {args.browser}")


if __name__ == "__main__":
    main()
