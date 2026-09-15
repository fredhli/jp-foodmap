#!/usr/bin/env python3
"""Separate station request, parse, tile creation and DOM attachment timings."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "perf"))
from station_grid_diagnostic import fixture_tile, gpu_info, seed, serve  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs", type=Path, default=ROOT / "docs")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "audit_output" / "4.3.1a" / "station-first-visible.json")
    args = parser.parse_args()
    docs = args.docs.resolve()

    with serve(docs) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": 475, "height": 751},
            screen={"width": 475, "height": 751},
            device_scale_factor=2.625,
            is_mobile=True,
            has_touch=True,
            locale="zh-CN",
            service_workers="block",
        )
        context.add_init_script(seed(True))
        page = context.new_page()
        page.set_default_timeout(120_000)
        page.route("**basemaps.cartocdn.com/**", fixture_tile)
        for blocked in (
            "accounts.google.com", "tblg.k-img.com", "emojicdn.elk.sh",
            "translate.googleapis.com", "nominatim.openstreetmap.org",
            "api.jpfoodmap.com", "www.google-analytics.com",
        ):
            page.route(f"**{blocked}**", lambda route: route.abort())
        page.goto(base + "/index.html", wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_function(
            "() => window.Adapter&&Adapter.ready===true&&window.MapMod&&MapMod.map&&Data.restaurants.length"
        )
        environment = gpu_info(page)
        page.evaluate("""() => {
          const map=MapMod.map;
          const transit=Object.values(map._layers).find(x=>x&&typeof x._loadStations==='function');
          if(!transit) throw new Error('transit layer unavailable below station min zoom');
          const q=window.__stationTiming={t0:null,stationLoadStart:null,stationLoad:null,
            firstCreateStart:null,firstCreateEnd:null,firstTileLoad:null,firstCanvasAttached:null};
          transit.on('stationloadstart',()=>{if(q.stationLoadStart===null)q.stationLoadStart=performance.now();});
          transit.on('stationload',()=>{if(q.stationLoad===null)q.stationLoad=performance.now();});
          const nativeCreate=transit._createStationTile;
          transit._createStationTile=function(){
            if(q.firstCreateStart===null)q.firstCreateStart=performance.now();
            const out=nativeCreate.apply(this,arguments);
            if(q.firstCreateEnd===null)q.firstCreateEnd=performance.now();
            return out;
          };
          const nativeFire=L.GridLayer.prototype.fire;
          L.GridLayer.prototype.fire=function(type,data,propagate){
            if(type==='tileload'&&this.options&&this.options.pane===transit.options.stationPane&&q.firstTileLoad===null)
              q.firstTileLoad=performance.now();
            return nativeFire.call(this,type,data,propagate);
          };
          const pane=map.getPane(transit.options.stationPane);
          new MutationObserver(()=>{
            if(q.firstCanvasAttached===null&&pane.querySelector('canvas[data-station-tile="1"]'))
              q.firstCanvasAttached=performance.now();
          }).observe(pane,{childList:true,subtree:true});
          q.t0=performance.now();
          map.setView([35.681236,139.767125],14,{animate:false});
        }""")
        page.wait_for_function(
            "() => {const q=window.__stationTiming,d=MapMod.stationDetail();return q.firstCanvasAttached!==null&&d.loaded&&d.liveCanvasCount>0;}"
        )
        page.wait_for_timeout(100)
        browser_timing = page.evaluate(r"""() => {
          const q=window.__stationTiming,t0=q.t0;
          const relative={}; for(const [k,v] of Object.entries(q)) relative[k]=v===null?null:v-t0;
          const resource=performance.getEntriesByType('resource').find(x=>/\/data\/stations\.[0-9a-f]{12}\.json/.test(x.name));
          return {absolute:q,relative,resource:resource?{
            startTime:resource.startTime-t0,responseStart:resource.responseStart-t0,
            responseEnd:resource.responseEnd-t0,duration:resource.duration,
            transferSize:resource.transferSize,encodedBodySize:resource.encodedBodySize,
            decodedBodySize:resource.decodedBodySize}:null,detail:MapMod.stationDetail()};
        }""")
        browser_version = browser.version
        context.close()
        browser.close()

    renderer = (docs / "transit-layer.js").read_bytes()
    html = (docs / "index.html").read_text(encoding="utf-8")
    match = re.search(r"transit-layer\.js\?v=([0-9a-f]+)", html)
    output = {
        "schema": 1,
        "verdict": "DIAGNOSTIC",
        "profile": {"viewport": [475, 751], "dpr": 2.625, "cpuThrottleRate": 1,
                    "mobileEmulation": True, "touch": True},
        "browserVersion": browser_version,
        "environment": environment,
        "renderer": {"sha256": hashlib.sha256(renderer).hexdigest(),
                     "md5": hashlib.md5(renderer).hexdigest(),
                     "indexAssetVersionMd5Prefix": match.group(1) if match else None},
        "timing": browser_timing,
        "interpretation": [
            "Times use performance.now in the page, with listeners installed before setView.",
            "firstCanvasAttached is DOM attachment, not a confirmed presented frame.",
            "The localhost station response is uncompressed and the basemap is a deterministic fixture.",
            "This headless SwiftShader run is not Fold hardware performance evidence.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(browser_timing, ensure_ascii=False))
    print(f"station_first_visible_probe.py DIAGNOSTIC: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
