#!/usr/bin/env python3
"""Focused station visibility, tooltip and layer-order check."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "audit_output" / "4.3.5a"
sys.path.insert(0, str(ROOT / "tests"))
import lib_browser  # noqa: E402

TOKYO = [35.681236, 139.767125]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {}
    with lib_browser.serve_docs(8995) as base, sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            viewport={"width": 402, "height": 874},
            screen={"width": 402, "height": 874},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            service_workers="block",
        )
        context.add_init_script("""localStorage.setItem('tabelog.lang','zh-CN');
          localStorage.setItem('tabelog.seenIntro','1');
          localStorage.setItem('tabelog.showStations','1');
          localStorage.setItem('tabelog.showTransitLong','0');
          localStorage.setItem('tabelog.showTransitCity','0');""")
        page = context.new_page()
        station_requests: list[str] = []
        page.on("request", lambda req: station_requests.append(req.url)
                if "/data/stations." in req.url else None)
        page.route("**api.jpfoodmap.com/**", lambda route: route.abort())
        page.route("**accounts.google.com/**", lambda route: route.abort())
        page.goto(base + "/index.html", wait_until="domcontentloaded", timeout=90_000)
        lib_browser.wait_ready(page, 90_000)

        page.evaluate("([c,z]) => MapMod.map.setView(c,z,{animate:false})", [TOKYO, 11])
        page.wait_for_timeout(300)
        assert not station_requests, station_requests

        rows: dict[str, object] = {}
        for zoom, expected in ((12, 90), (13, 610), (14, 8954)):
            page.evaluate("([c,z]) => MapMod.map.setView(c,z,{animate:false})", [TOKYO, zoom])
            page.wait_for_function("() => MapMod.stationDetail().loaded", timeout=60_000)
            page.wait_for_timeout(350)
            detail = page.evaluate("""z => {
              const transit=Object.values(MapMod.map._layers).find(x=>x&&x._stationGridLayer);
              return {global:transit._allStations.filter(s=>transit._stationShown(s,z)).length,
                scale:MapMod.scaleMetres(),canvas:MapMod.stationDetail().liveCanvasCount};
            }""", zoom)
            assert detail["global"] == expected, (zoom, detail)
            assert detail["canvas"] > 0, (zoom, detail)
            page.screenshot(path=str(OUT / f"station-z{zoom}.png"), full_page=False)
            rows[str(zoom)] = detail

        assert 2000 <= rows["12"]["scale"] <= 5000, rows
        assert 1000 <= rows["13"]["scale"] <= 2000, rows
        assert 300 <= rows["14"]["scale"] <= 1000, rows

        interaction = page.evaluate("""() => {
          const transit=Object.values(MapMod.map._layers).find(x=>x&&x._stationGridLayer);
          const s=transit._allStations.find(x=>x.name);
          const ll=L.latLng(s.lat,s.lon), cp=MapMod.map.latLngToContainerPoint(ll);
          MapMod.map.fire('click',{latlng:ll,containerPoint:cp,layerPoint:MapMod.map.latLngToLayerPoint(ll)});
          MapMod.map.fire('mousemove',{latlng:ll,containerPoint:cp});
          return {tooltips:document.querySelectorAll('.transit-station-label').length,
            clickBound:!!transit._onStationClickBound,hoverBound:!!transit._onStationMouseMoveBound,
            tip:!!transit._stationHoverTip};
        }""")
        assert interaction == {
            "tooltips": 0, "clickBound": False, "hoverBound": False, "tip": False
        }, interaction

        page.locator('[data-fab="layers"]').click()
        page.wait_for_selector('[data-ov="layer-toggle"][data-layer="stations"]')
        order = page.locator('[data-ov="layer-toggle"]').evaluate_all(
            "els => els.map(x=>x.dataset.layer)"
        )
        assert order[:3] == ["stations", "long", "city"], order
        report = {"visibility": rows, "interaction": interaction, "layerOrder": order,
                  "stationRequestsBelowZ12": 0}
        context.close()
        browser.close()

    (OUT / "station-visibility.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"station_visibility_433.py OK: {OUT / 'station-visibility.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
