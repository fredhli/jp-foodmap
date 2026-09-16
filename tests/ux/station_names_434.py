#!/usr/bin/env python3
"""Focused 4.3.4a fixed-label and station-tooltip regression check."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "audit_output" / "4.3.4a"
sys.path.insert(0, str(ROOT / "tests"))
import lib_browser  # noqa: E402

TOKYO = [35.681236, 139.767125]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with lib_browser.serve_docs(8996) as base, sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1000, "height": 760}, device_scale_factor=2,
            service_workers="block",
        )
        context.add_init_script("""localStorage.setItem('tabelog.lang','zh-CN');
          localStorage.setItem('tabelog.seenIntro','1');
          localStorage.setItem('tabelog.showStations','1');
          localStorage.setItem('tabelog.showTransitLong','0');
          localStorage.setItem('tabelog.showTransitCity','0');
          window.__stationFillTexts=[];
          const nativeFill=CanvasRenderingContext2D.prototype.fillText;
          CanvasRenderingContext2D.prototype.fillText=function(text){
            window.__stationFillTexts.push(String(text));
            return nativeFill.apply(this,arguments);
          };""")
        page = context.new_page()
        page.route("**api.jpfoodmap.com/**", lambda route: route.abort())
        page.route("**accounts.google.com/**", lambda route: route.abort())
        page.goto(base + "/index.html", wait_until="domcontentloaded", timeout=90_000)
        lib_browser.wait_ready(page, 90_000)
        page.evaluate("() => { window.__stationFillTexts=[]; }")
        page.evaluate("([c,z]) => MapMod.map.setView(c,z,{animate:false})", [TOKYO, 12])
        page.wait_for_function("""() => {
          const d=MapMod.stationDetail();
          return d.loaded&&d.placementStatus==='ready'&&!d.labelsSuppressed&&d.liveCanvasCount>0;
        }""", timeout=60_000)
        page.wait_for_timeout(400)
        z12 = page.evaluate("""() => {
          const transit=Object.values(MapMod.map._layers).find(x=>x&&x._stationGridLayer);
          const z=12, bit=z-transit._stationPayloadMeta.placement.zoomMin;
          const bounds=MapMod.map.getBounds(),center=MapMod.map.getCenter();
          const candidates=transit._allStations.filter(s=>bounds.contains([s.lat,s.lon])&&transit._stationShown(s,z));
          candidates.sort((a,b)=>center.distanceTo([a.lat,a.lon])-center.distanceTo([b.lat,b.lon]));
          const s=candidates[0]; if(!s)throw new Error('no visible z12 station');
          return {station:{name:transit._stationName(s),lat:s.lat,lon:s.lon},
            drawn:Array.from(new Set(window.__stationFillTexts)),
            detail:MapMod.stationDetail(),maxMask:Math.max(...transit._stationPlacement.visibleMaskByItem),
            fixedMasks:transit._stationPlacement.visibleMaskByItem.filter(m=>(m&(1<<bit))!==0).length,
            tiers:[12,13,14].map(z=>transit._allStations.filter(s=>transit._stationShown(s,z)).length)};
        }""")
        assert not z12["drawn"] and z12["fixedMasks"] == 0, z12
        assert z12["maxMask"] > 63, z12
        assert z12["detail"]["placementStatus"] == "ready" and not z12["detail"]["labelsSuppressed"], z12
        assert z12["tiers"] == [72, 607, 8954], z12
        page.screenshot(path=str(OUT / "station-z12-names.png"), full_page=False)

        z12_lifecycle = page.evaluate("""async station => {
          const map=MapMod.map,ll=L.latLng(station.lat,station.lon);
          const wait=ms=>new Promise(r=>setTimeout(r,ms));
          const transit=Object.values(map._layers).find(x=>x&&x._stationGridLayer);
          const event=()=>({latlng:ll,containerPoint:map.latLngToContainerPoint(ll),layerPoint:map.latLngToLayerPoint(ll)});
          const read=()=>Array.from(document.querySelectorAll('.transit-station-label')).map(x=>x.textContent.trim());
          const size=map.getSize(); let empty=null;
          for(let y=20;y<size.y-20&&!empty;y+=40)for(let x=20;x<size.x-20&&!empty;x+=40){
            const p=L.point(x,y),candidate=map.containerPointToLatLng(p);
            if(!transit._hitStation(candidate))empty={latlng:candidate,containerPoint:p,layerPoint:map.containerPointToLayerPoint(p)};
          }
          if(!empty)throw new Error('no empty map point');
          map.fire('click',event()); await wait(50); const click=read();
          map.fire('click',empty); await wait(250); const blank=read();
          map.fire('click',event()); await wait(50); map.fire('movestart'); await wait(250); const pan=read();
          map.fire('click',event()); await wait(50); map.fire('zoomstart'); await wait(250); const zoom=read();
          map.fire('mousemove',event()); await wait(50); const hover=read();
          transit._hideStationHover(); await wait(250);
          const marker=document.querySelector('.leaflet-marker-icon,.leaflet-interactive,.marker-cluster');
          if(!marker)throw new Error('restaurant/cluster marker unavailable');
          map.fire('click',Object.assign(event(),{originalEvent:{target:marker}})); await wait(50); const markerPriority=read();
          map.fire('click',event()); await wait(50);
          return {click,blank,pan,zoom,hover,markerPriority};
        }""", z12["station"])
        name = z12["station"]["name"]
        assert z12_lifecycle["click"] == [name] and z12_lifecycle["hover"] == [name], z12_lifecycle
        assert not z12_lifecycle["blank"] and not z12_lifecycle["pan"] and not z12_lifecycle["zoom"], z12_lifecycle
        assert not z12_lifecycle["markerPriority"], z12_lifecycle

        page.evaluate("() => { window.__stationFillTexts=[]; }")
        page.evaluate("([c,z]) => MapMod.map.setView(c,z,{animate:false})", [TOKYO, 13])
        page.wait_for_function("() => window.__stationFillTexts.length>0", timeout=30_000)
        page.wait_for_timeout(250)
        z13 = page.evaluate("""() => {
          const transit=Object.values(MapMod.map._layers).find(x=>x&&x._stationGridLayer),z=13,bit=1;
          const bounds=MapMod.map.getBounds(),center=MapMod.map.getCenter();
          const visible=transit._allStations.filter(s=>bounds.contains([s.lat,s.lon])&&transit._stationShown(s,z));
          const fixed=visible.filter(s=>(transit._stationPlacement.visibleMaskByItem[s._placementIndex]&(1<<bit))!==0);
          const tier2=visible.filter(s=>s.line_count>=3&&s.line_count<6);
          fixed.sort((a,b)=>center.distanceTo([a.lat,a.lon])-center.distanceTo([b.lat,b.lon]));
          tier2.sort((a,b)=>center.distanceTo([a.lat,a.lon])-center.distanceTo([b.lat,b.lon]));
          if(!fixed[0]||!tier2[0])throw new Error('z13 fixture lacks fixed tier3 or temporary tier2');
          const pack=s=>({name:transit._stationName(s),lat:s.lat,lon:s.lon});
          return {fixed:pack(fixed[0]),tier2:pack(tier2[0]),drawn:Array.from(new Set(window.__stationFillTexts)),
            fixedMasks:transit._stationPlacement.visibleMaskByItem.filter(m=>(m&2)!==0).length};
        }""")
        assert z13["fixed"]["name"] in z13["drawn"], z13
        assert z13["tier2"]["name"] not in z13["drawn"], z13
        assert z13["fixedMasks"] > 0, z13
        z13_interaction = page.evaluate("""async data => {
          const map=MapMod.map,wait=ms=>new Promise(r=>setTimeout(r,ms));
          const read=()=>Array.from(document.querySelectorAll('.transit-station-label')).map(x=>x.textContent.trim());
          const fire=s=>{const ll=L.latLng(s.lat,s.lon);map.fire('click',{latlng:ll,containerPoint:map.latLngToContainerPoint(ll),layerPoint:map.latLngToLayerPoint(ll)});};
          fire(data.fixed); await wait(50); const fixed=read();
          fire(data.tier2); await wait(50); const tier2=read();
          return {fixed,tier2};
        }""", z13)
        assert not z13_interaction["fixed"], z13_interaction
        assert z13_interaction["tier2"] == [z13["tier2"]["name"]], z13_interaction
        page.screenshot(path=str(OUT / "station-z13-names.png"), full_page=False)

        page.evaluate("() => { window.__stationFillTexts=[]; MapMod.map.setZoom(14,{animate:false}); }")
        page.wait_for_function("() => window.__stationFillTexts.length>0", timeout=30_000)
        page.wait_for_timeout(250)
        z14 = page.evaluate("""() => {
          const transit=Object.values(MapMod.map._layers).find(x=>x&&x._stationGridLayer);
          const s=transit._allStations.find(x=>MapMod.map.getBounds().contains([x.lat,x.lon]));
          const ll=L.latLng(s.lat,s.lon);
          MapMod.map.fire('click',{latlng:ll,containerPoint:MapMod.map.latLngToContainerPoint(ll),layerPoint:MapMod.map.latLngToLayerPoint(ll)});
          return {drawn:Array.from(new Set(window.__stationFillTexts)),tooltips:document.querySelectorAll('.transit-station-label').length,
            clickBound:!!transit._onStationClickBound,hoverBound:!!transit._onStationMouseMoveBound,
            detail:MapMod.stationDetail()};
        }""")
        assert z14["drawn"], z14
        assert not z14["tooltips"] and not z14["clickBound"] and not z14["hoverBound"], z14
        assert z14["detail"]["placementStatus"] == "ready" and not z14["detail"]["labelsSuppressed"], z14
        page.screenshot(path=str(OUT / "station-z14-names.png"), full_page=False)

        page.locator('[data-fab="layers"]').click()
        page.wait_for_selector('[data-ov="layer-toggle"][data-layer="stations"]')
        order = page.locator('[data-ov="layer-toggle"]').evaluate_all("els=>els.map(x=>x.dataset.layer)")
        assert order[:3] == ["stations", "long", "city"], order

        report = {"z12": z12, "z12Lifecycle": z12_lifecycle, "z13": z13,
                  "z13Interaction": z13_interaction, "z14": z14, "layerOrder": order}
        (OUT / "station-names.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        context.close(); browser.close()
    print(f"station_names_434.py OK: {OUT / 'station-names.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
