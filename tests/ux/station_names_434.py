#!/usr/bin/env python3
"""Focused station fixed-label, tooltip and importance-tier regression check."""

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


def published_payload() -> dict:
    html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    names = sorted(set(re.findall(r"data/(stations\.[0-9a-f]{12}\.json)", html)))
    assert len(names) == 1, names
    payload = json.loads((ROOT / "docs" / "data" / names[0]).read_bytes())
    assert payload["v"] == 4, payload["v"]
    return payload


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    payload = published_payload()
    icon_counts = [
        sum(1 for m in payload["icons"]["maskByItem"] if m & (1 << (z - 12)))
        for z in (12, 13, 14)
    ]
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
        # Replaces `== [90, 610, 8954]` (4.3.5a's tier rule): 4.3.6 reads the
        # build-time Poisson selection, so the page must agree with the
        # payload's own icon bits, and z12 must be far denser than 4.3.5a.
        assert z12["tiers"] == icon_counts, (z12["tiers"], icon_counts)
        assert z12["tiers"][0] >= 1500, z12
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
          const tier2=visible.filter(s=>transit._stationTier(s)===1);
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

        importance = page.evaluate("""() => {
          const transit=Object.values(MapMod.map._layers).find(x=>x&&x._stationGridLayer);
          const wanted=['大阪','梅田','難波','なんば','札幌','仙台','博多','熊本'];
          return wanted.map(name=>{
            const s=transit._allStations.find(x=>x.name===name&&x.display_tier>=1);
            if(!s)throw new Error('missing station: '+name);
            const states=[12,13,14].map(z=>{
              const bit=z-transit._stationPayloadMeta.placement.zoomMin;
              const fixed=(transit._stationPlacement.visibleMaskByItem[s._placementIndex]&(1<<bit))!==0;
              return {zoom:z,shown:transit._stationShown(s,z),fixed,
                temporary:transit._stationTemporaryNameAllowed(s,z)};
            });
            return {name,raw:s.line_count,displayTier:s.display_tier,modes:s.modes,
              iconMask:transit._stationPayloadMeta.icons.maskByItem[s._placementIndex],
              effectiveTier:transit._stationTier(s)+1,states};
          });
        }""")
        # Replaces the 4.3.5a override check (all eight were forced to tier 3
        # and therefore shown at z12). 4.3.6 has no overrides: the tier is the
        # continuous-importance tier from the payload, and 梅田 legitimately
        # lands in tier 2 as a rail-only station. Visibility is the payload's
        # icon bit, so e.g. 大阪 yields z12 to 新大阪 3.5 km away.
        expected_tier = {"大阪": 3, "梅田": 2, "難波": 3, "なんば": 3,
                         "札幌": 3, "仙台": 3, "博多": 3, "熊本": 3}
        expected_modes = {"大阪": 3, "梅田": 1, "難波": 1, "博多": 7}
        for station in importance:
            name = station["name"]
            assert station["displayTier"] == expected_tier[name], station
            assert station["effectiveTier"] == station["displayTier"], station
            if name in expected_modes:
                assert station["modes"] == expected_modes[name], station
            for state in station["states"]:
                bit = 1 << (state["zoom"] - 12)
                assert state["shown"] == bool(station["iconMask"] & bit), station
                assert not state["fixed"] or state["shown"], station
            z12_state, z13_state, z14_state = station["states"]
            assert not z12_state["fixed"] and z12_state["temporary"] == z12_state["shown"], station
            if z13_state["shown"]:
                assert z13_state["fixed"] != z13_state["temporary"], station
            else:
                assert not z13_state["fixed"] and not z13_state["temporary"], station
            assert not z14_state["temporary"], station
        # The mega-hubs that head their own spacing disc still show at z12.
        for name in ("札幌", "仙台", "博多", "熊本"):
            assert next(s for s in importance if s["name"] == name)["states"][0]["shown"], name

        page.locator('[data-fab="layers"]').click()
        page.wait_for_selector('[data-ov="layer-toggle"][data-layer="stations"]')
        order = page.locator('[data-ov="layer-toggle"]').evaluate_all("els=>els.map(x=>x.dataset.layer)")
        assert order[:3] == ["stations", "long", "city"], order

        report = {"z12": z12, "z12Lifecycle": z12_lifecycle, "z13": z13,
                  "z13Interaction": z13_interaction, "z14": z14,
                  "importance": importance, "layerOrder": order}
        (OUT / "station-names.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        context.close(); browser.close()
    print(f"station name regression OK: {OUT / 'station-names.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
