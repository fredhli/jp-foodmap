#!/usr/bin/env python3
"""Focused station visibility, tooltip and layer-order check."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
# 4.3.6: evidence moved out of the 4.3.5a directory so a rerun does not
# overwrite the screenshots that release cites.
OUT = ROOT / "audit_output" / "4.3.6"
sys.path.insert(0, str(ROOT / "tests"))
import lib_browser  # noqa: E402

TOKYO = [35.681236, 139.767125]


def published_icon_counts() -> dict[int, int]:
    """Per-zoom icon counts read straight from the published v4 payload.

    4.3.6 replaced the 4.3.5a tier rule (z12 = 90 tier-3 hubs, z13 = 610
    tier-2+, z14 = all 8,954) with the build-time Poisson-disk selection in
    icons.maskByItem, so the expected counts now come from the payload the
    page references rather than from constants of the old rule.
    """
    html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    names = sorted(set(re.findall(r"data/(stations\.[0-9a-f]{12}\.json)", html)))
    assert len(names) == 1, names
    payload = json.loads((ROOT / "docs" / "data" / names[0]).read_bytes())
    assert payload["v"] == 4, payload["v"]
    masks = payload["icons"]["maskByItem"]
    return {z: sum(1 for m in masks if m & (1 << (z - 12))) for z in range(12, 20)}


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
        icon_counts = published_icon_counts()
        # Replaces ((12, 90), (13, 610), (14, 8954)): see published_icon_counts.
        # The z12 range guards the spec's density target (1,500-2,800 nationwide,
        # 4.3.5a drew 90) so a payload that silently falls back cannot pass.
        assert 1500 <= icon_counts[12] <= 2800, icon_counts
        assert icon_counts[12] < icon_counts[13] < icon_counts[14] <= 8954, icon_counts
        for zoom in (12, 13, 14):
            expected = icon_counts[zoom]
            page.evaluate("([c,z]) => MapMod.map.setView(c,z,{animate:false})", [TOKYO, zoom])
            page.wait_for_function("() => MapMod.stationDetail().loaded", timeout=60_000)
            page.wait_for_timeout(350)
            detail = page.evaluate("""z => {
              const transit=Object.values(MapMod.map._layers).find(x=>x&&x._stationGridLayer);
              // 4.3.6: badges per station follow the zoom (1 at z12-13, at most
              // 2 at z14) and the size tier applies from z12 up; 4.3.5a drew one
              // 11px rail badge for every station through z14.
              const shown=transit._allStations.filter(s=>transit._stationShown(s,z));
              const perStation=[0,0,0,0];
              let wrongSize=0;
              const scale=transit._stationStyleScale();
              for(const s of shown){
                perStation[transit._stationBadgeKinds(s,z).length]++;
                const t=transit._stationTier(s);
                if(Math.abs(transit._stationBadgeSize(s,z)-[11,14,18][t]*scale)>1e-9)wrongSize++;
              }
              return {global:shown.length,perStation,wrongSize,
                iconStatus:MapMod.stationDetail().iconStatus,
                scale:MapMod.scaleMetres(),canvas:MapMod.stationDetail().liveCanvasCount};
            }""", zoom)
            assert detail["iconStatus"] == "ready", (zoom, detail)
            assert detail["global"] == expected, (zoom, detail)
            assert detail["wrongSize"] == 0, (zoom, detail)
            assert detail["perStation"][0] == 0 and detail["perStation"][3] == 0, (zoom, detail)
            if zoom <= 13:
                assert detail["perStation"][2] == 0, (zoom, detail)
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
