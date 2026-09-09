"""3.2.2: the base map drops to 1x tiles while a rail layer is drawing.

The switch is `window.__tilesHiDpi`, read by the getTileUrl override that
map.py installs before folium's map script. This asserts the three things
that make it work at all, on a DPR 2 screen:

  1. no rail layer on  -> the tile URLs carry @2x
  2. a rail layer on   -> the tiles requested from then on do not
  3. both off again    -> @2x comes back
  4. cold start with a rail layer already on in localStorage -> the very
     first tile of the page is 1x, with no @2x request and no redraw

Usage: .venv-wsl/bin/python tests/ux/tiles.py [--browser chromium|webkit]
"""
import argparse
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import lib_browser                                    # noqa: E402
from playwright.sync_api import sync_playwright       # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--browser", choices=["chromium", "webkit"], default="chromium")
ap.add_argument("--docs", type=Path, default=ROOT / "docs")
args = ap.parse_args()
lib_browser.DOCS = args.docs.resolve()

# 1x1 transparent PNG: the assertion is about the URL, not the picture.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
EMPTY_GEOJSON = json.dumps({"type": "FeatureCollection", "features": []})

tiles: list[str] = []


def main() -> int:
    with lib_browser.serve_docs(8977) as base, sync_playwright() as p:
        browser = getattr(p, args.browser).launch()
        ctx = browser.new_context(viewport={"width": 932, "height": 704},
                                  device_scale_factor=2, is_mobile=True,
                                  has_touch=True, service_workers="block")
        page = ctx.new_page()
        page.set_default_timeout(30000)

        def tile(route):
            tiles.append(route.request.url)
            route.fulfill(status=200, body=PNG, content_type="image/png",
                          headers={"access-control-allow-origin": "*"})

        page.route("**basemaps.cartocdn.com**", tile)
        # The rail overlay lives on R2; serve it empty so a toggle-on
        # succeeds instead of rolling itself back through lodloaderror.
        page.route("**assets.jpfoodmap.com**", lambda r: r.fulfill(
            status=200, body=EMPTY_GEOJSON, content_type="application/geo+json",
            headers={"access-control-allow-origin": "*"}))
        errors: list[str] = []
        lib_browser.install_guards(page, errors)
        lib_browser.seed_local_storage(page, {
            "tabelog.lang": "zh-CN",
            "tabelog.seenIntro": "1",
            "tabelog.showTransitLong": "0",
            "tabelog.showTransitCity": "0",
        })
        lib_browser.boot(page, base)
        page.wait_for_timeout(1500)
        assert page.evaluate("() => devicePixelRatio") == 2, "harness lost its DPR"

        assert tiles, "no tile requests at all"
        assert all("@2x" in u for u in tiles), (
            f"rail layers off: expected every tile @2x, got {tiles[:3]}")

        # 1 -> 2: turning a bucket on
        tiles.clear()
        page.click("#fab-layers")
        page.wait_for_timeout(300)
        page.click("#fab-transit-long")
        page.wait_for_timeout(2500)
        assert page.evaluate("() => window.__tilesHiDpi") is False, (
            "__tilesHiDpi should be false while a rail bucket is on")
        assert tiles, "turning a rail layer on requested no tiles (no redraw?)"
        bad = [u for u in tiles if "@2x" in u]
        assert not bad, f"rail layer on: {len(bad)}/{len(tiles)} tiles still @2x"

        # 2 -> 3: both off again
        tiles.clear()
        page.click("#fab-transit-long")
        page.wait_for_timeout(800)
        assert page.evaluate("() => window.__tilesHiDpi") is True, (
            "__tilesHiDpi should be true again with both buckets off")
        # No redraw on the way back up (deliberate), so pan to force new tiles.
        page.evaluate("() => { const m = window[Object.keys(window).find("
                      "k => /^map_[a-f0-9]/.test(k) && window[k] && window[k].panBy)];"
                      " m.panBy([600, 400], {animate: false}); }")
        page.wait_for_timeout(2000)
        assert tiles, "panning after the rail layers went off requested no tiles"
        bad = [u for u in tiles if "@2x" not in u]
        assert not bad, f"rail layers off again: {len(bad)}/{len(tiles)} tiles still 1x"
        ctx.close()

        # 4: cold start with a rail layer already on
        tiles.clear()
        ctx = browser.new_context(viewport={"width": 932, "height": 704},
                                  device_scale_factor=2, is_mobile=True,
                                  has_touch=True, service_workers="block")
        page = ctx.new_page()
        page.set_default_timeout(30000)
        page.route("**basemaps.cartocdn.com**", tile)
        page.route("**assets.jpfoodmap.com**", lambda r: r.fulfill(
            status=200, body=EMPTY_GEOJSON, content_type="application/geo+json",
            headers={"access-control-allow-origin": "*"}))
        lib_browser.install_guards(page, errors)
        lib_browser.seed_local_storage(page, {
            "tabelog.lang": "zh-CN",
            "tabelog.seenIntro": "1",
            "tabelog.showTransitLong": "0",
            "tabelog.showTransitCity": "1",
        })
        lib_browser.boot(page, base)
        page.wait_for_timeout(2000)
        bad = [u for u in tiles if "@2x" in u]
        assert not bad, (
            f"cold start with 市内 on: {len(bad)}/{len(tiles)} tiles were @2x — "
            "the switch has to be set before the first tile, not after")
        ctx.close()
        browser.close()

    real = [e for e in errors if "cartocdn" not in e and "jpfoodmap.com" not in e]
    if real:
        print("page errors:", *real, sep="\n  ")
        return 1
    print(f"tiles.py OK ({args.browser}): @2x off with a rail layer on, "
          "back on with both off, and 1x from the first tile on a cold start")
    return 0


sys.exit(main())
