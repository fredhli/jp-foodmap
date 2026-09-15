#!/usr/bin/env python3
"""Minimal contract for CARTO basemap switching and cold-start restore."""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import lib_browser  # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nC8AAAAASUVORK5CYII="
)
PATHS = {
    "voyager": "/rastertiles/voyager/",
    "positron": "/light_all/",
    "voyager-nolabels": "/rastertiles/voyager_nolabels/",
    "positron-nolabels": "/light_nolabels/",
}


def main() -> int:
    with lib_browser.serve_docs(8994) as base, sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1000, "height": 760}, device_scale_factor=2)
        context.add_init_script("""localStorage.setItem('tabelog.lang','zh-CN');
          localStorage.setItem('tabelog.seenIntro','1');
          if (!localStorage.getItem('tabelog.basemapStyle'))
            localStorage.setItem('tabelog.basemapStyle','voyager-nolabels');""")
        page = context.new_page()
        requests: list[str] = []

        def tile(route) -> None:
            requests.append(route.request.url)
            route.fulfill(status=200, body=PNG, content_type="image/png")

        page.route("**basemaps.cartocdn.com/**", tile)
        for host in lib_browser.BLOCKED_HOST_FRAGMENTS:
            page.route(f"**{host}**", lambda route: route.abort())
        page.route("**accounts.google.com/**", lambda route: route.abort())
        page.goto(base + "/index.html", wait_until="domcontentloaded")
        lib_browser.wait_ready(page, 90_000)
        assert requests and all(PATHS["voyager-nolabels"] in url for url in requests), requests[:2]
        assert all(parse_qs(urlparse(url).query).get("key", [""])[0] for url in requests)
        assert any("@2x.png" in url for url in requests), "DPR 2 did not retain CARTO @2x"

        page.locator('[data-ov="open"][data-kind="account"]').click()
        select = page.locator('[data-ov-field="basemap"]')
        select.wait_for()
        assert select.locator("option").count() == 4
        view0 = page.evaluate("() => ({c:MapMod.map.getCenter(),z:MapMod.map.getZoom()})")
        layer_id = page.evaluate("""() => { let out=null; MapMod.map.eachLayer(l=>{
          if(!out && l instanceof L.TileLayer && l._url) out=L.stamp(l); }); return out; }""")
        for style, path in PATHS.items():
            requests.clear()
            select.select_option(style)
            page.wait_for_timeout(300)
            layer_url = page.evaluate("""() => { let out=''; MapMod.map.eachLayer(l=>{
              if(!out && l instanceof L.TileLayer && l._url) out=l._url; }); return out; }""")
            assert path in layer_url, (style, urlparse(layer_url).path)
            assert parse_qs(urlparse(layer_url).query).get("key", [""])[0]
            assert "{r}.png" in layer_url
            assert page.evaluate("localStorage.getItem('tabelog.basemapStyle')") == style
            assert page.evaluate("MapMod.tileStatus().style") == style
            assert page.evaluate("""() => { let out=null; MapMod.map.eachLayer(l=>{
              if(!out && l instanceof L.TileLayer && l._url) out=L.stamp(l); }); return out; }""") == layer_id
            view = page.evaluate("() => ({c:MapMod.map.getCenter(),z:MapMod.map.getZoom()})")
            assert view["z"] == view0["z"]
            assert abs(view["c"]["lat"] - view0["c"]["lat"]) < 1e-9
            assert abs(view["c"]["lng"] - view0["c"]["lng"]) < 1e-9

        select.select_option("voyager")
        page.wait_for_timeout(200)
        assert page.evaluate("localStorage.getItem('tabelog.basemapStyle')") == "voyager"
        requests.clear()
        page.reload(wait_until="domcontentloaded")
        lib_browser.wait_ready(page, 90_000)
        first_tile = page.locator(".leaflet-tile").first.get_attribute("src") or ""
        assert PATHS["voyager"] in first_tile, urlparse(first_tile).path
        assert PATHS["voyager-nolabels"] not in first_tile
        context.close()
        browser.close()
    print("basemap_preferences.py OK: 4 styles, first-request restore, same layer/view, key + @2x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
