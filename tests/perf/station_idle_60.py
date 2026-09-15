#!/usr/bin/env python3
"""Paired 60-second idle/energy proxy for the loaded station canvas."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
import station_layer_bench as bench

OUT=bench.ROOT/"tests/perf/results/station-layer-4.3.0"
with bench.serve_docs(bench.StationServer()) as base, sync_playwright() as p:
    browser=p.chromium.launch(args=["--disable-background-networking"])
    profile=bench.PROFILES["fold-outer-6x"]
    ctx=bench.new_context(browser,profile,True)
    run=bench.open_page(ctx,base,profile["cpu_rate"],True)
    layer=run.page.evaluate_handle("""() => {let x=null; MapMod.map.eachLayer(l=>{if(typeof l.stationDetail==='function'&&typeof l.setStationsVisible==='function')x=l}); return x}""")
    results={}
    for visible in (False,True):
        layer.evaluate("(l,v)=>l.setStationsVisible(v)",visible)
        run.page.wait_for_timeout(1000)
        key="on" if visible else "off"
        results[key]=bench.trace_idle(run.page,run.cdp,60,OUT/("idle60-"+key+".json.gz"),lambda:0)
        print(key,results[key],flush=True)
    layer.evaluate("l=>l.setStationsVisible(true)")
    ctx.close();browser.close()
d=round(results["on"]["task_ms"]-results["off"]["task_ms"],3)
out={"verdict":"PASS" if d<=30 else "FAIL","task_delta_ms":d,"results":results}
(OUT/"idle60-paired.json").write_text(json.dumps(out,indent=2),encoding="utf-8")
print(json.dumps(out,indent=2))
raise SystemExit(0 if out["verdict"]=="PASS" else 1)
