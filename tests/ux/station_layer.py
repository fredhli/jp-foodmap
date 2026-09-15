"""4.3.0 station-layer state, persistence, coupling and accessible UI.

Usage: .venv-wsl/bin/python tests/ux/station_layer.py [--docs PATH]
"""
import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import lib_browser  # noqa: E402


ap = argparse.ArgumentParser()
ap.add_argument("--docs", type=Path, default=ROOT / "docs")
ap.add_argument("--browser", choices=["chromium", "webkit"], default="chromium")
args = ap.parse_args()
lib_browser.DOCS = args.docs.resolve()

EMPTY_STATIONS = json.dumps({
    "v": 1,
    "fields": ["lon", "lat", "name", "name_en", "railway", "line_count"],
    "stations": [],
})
EMPTY_GEOJSON = json.dumps({"type": "FeatureCollection", "features": []})


def open_layers(page):
    if not page.locator('[data-ov="layer-toggle"][data-layer="stations"]').count():
        page.locator('[data-fab="layers"]').click()
    page.wait_for_selector('[data-ov="layer-toggle"][data-layer="stations"]')


def station_switch(page):
    return page.locator('[data-ov="layer-toggle"][data-layer="stations"]')


def main() -> int:
    errors: list[str] = []
    rail_requests: list[str] = []
    with lib_browser.serve_docs(8991) as base, sync_playwright() as p:
        browser = getattr(p, args.browser).launch()
        context = browser.new_context(viewport={"width": 932, "height": 704},
                                      service_workers="block")
        page = context.new_page()
        page.set_default_timeout(30000)
        lib_browser.install_guards(page, errors)
        lib_browser.seed_local_storage(page, {
            "tabelog.lang": "zh-CN",
            "tabelog.seenIntro": "1",
            "tabelog.showTransitLong": "0",
            "tabelog.showTransitCity": "0",
        })
        page.route("**/transit/japan-stations.json", lambda r: r.fulfill(
            status=200, body=EMPTY_STATIONS, content_type="application/json"))
        def rail_asset(route):
            rail_requests.append(route.request.url)
            route.fulfill(status=200, body=EMPTY_GEOJSON,
                          content_type="application/geo+json",
                          headers={"access-control-allow-origin": "*"})

        page.route("**assets.jpfoodmap.com**", rail_asset)
        lib_browser.boot(page, base)

        assert page.evaluate("App.state.layers.stations") is True
        page.wait_for_timeout(300)
        assert not rail_requests, (
            "stations-only boot fetched a line LOD: " + repr(rail_requests))
        assert page.locator(".mp-fab-badge").inner_text().strip() == "3"
        open_layers(page)
        sw = station_switch(page)
        assert sw.get_attribute("role") == "switch"
        assert sw.get_attribute("aria-checked") == "true"
        assert sw.is_enabled()

        # An intentional off choice persists through a fresh document.
        sw.click()
        page.wait_for_function("() => App.state.layers.stations === false")
        assert page.evaluate("localStorage.getItem('tabelog.showStations')") == "0"
        assert page.locator(".mp-fab-badge").inner_text().strip() == "2"
        page.reload(wait_until="domcontentloaded")
        lib_browser.wait_ready(page)
        assert page.evaluate("App.state.layers.stations") is False

        # Either rail bucket restores stations. Turning the last rail bucket
        # off does not reverse that one-way coupling.
        open_layers(page)
        page.locator('[data-layer="long"]').click()
        page.wait_for_function(
            "() => App.state.layers.long && App.state.layers.stations")
        assert page.evaluate("localStorage.getItem('tabelog.showStations')") == "1"
        assert station_switch(page).is_disabled()
        page.locator('[data-layer="long"]').click()
        page.wait_for_function("() => !App.state.layers.long")
        assert page.evaluate("App.state.layers.stations") is True
        assert station_switch(page).is_enabled()

        # Programmatic callers obey the same invariant, including an attempted
        # stations-off patch while rail remains active.
        page.evaluate("App.act.setLayers({city:true, stations:false})")
        assert page.evaluate(
            "App.state.layers.city && App.state.layers.stations") is True
        page.evaluate("App.act.setLayers({city:false})")
        assert page.evaluate(
            "!App.state.layers.city && App.state.layers.stations") is True

        # A load failure never flips the preference. It exposes one retry
        # control and dispatches to the station-specific retry method.
        page.evaluate("""() => {
          window.__stationRetries = 0;
          MapMod.retryStations = () => { window.__stationRetries++; return true; };
          App.emit('layers:load', {kind:'stations', status:'error'});
          App.flushNow();
        }""")
        retry = page.locator('[data-ov="layer-retry"][data-layer="stations"]')
        assert retry.count() == 1
        assert page.evaluate("App.state.layers.stations") is True
        retry.click()
        assert page.evaluate("window.__stationRetries") == 1

        context.close()
        browser.close()

    if errors:
        print("page errors:", *errors, sep="\n  ")
        return 1
    print("station_layer.py OK: default, explicit-off persistence, rail coupling, retry and a11y")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
