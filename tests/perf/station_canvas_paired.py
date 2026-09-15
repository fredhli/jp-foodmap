#!/usr/bin/env python3
"""Paired Fold-outer calibration for station canvas rendering only."""
from __future__ import annotations

import json
import random
import statistics
from pathlib import Path

from playwright.sync_api import sync_playwright

import station_layer_bench as bench

OUT = bench.ROOT / "tests/perf/results/station-layer-4.3.0"


def pct(values, p):
    s = sorted(values)
    return round(s[min(len(s)-1, round((len(s)-1)*p/100))], 3)


def main() -> int:
    payload = bench.StationServer()
    profile = bench.PROFILES["fold-outer-6x"]
    trials = []
    with bench.serve_docs(payload) as base, sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-background-networking"])
        ctx = bench.new_context(browser, profile, True)
        run = bench.open_page(ctx, base, profile["cpu_rate"], True)
        handle = run.page.evaluate_handle("""() => {
          let found=null;
          MapMod.map.eachLayer(layer => {
            if (typeof layer.stationDetail==='function' && typeof layer.setStationsVisible==='function') found=layer;
          });
          if (!found || found.stationCount()!==8954) throw new Error('loaded TransitLayer not found');
          return found;
        }""")
        sequence = [False, True, True, False] * 3
        for index, visible in enumerate(sequence, 1):
            handle.evaluate("(layer,visible) => layer.setStationsVisible(visible)", visible)
            run.page.wait_for_timeout(1000)
            result = bench.pan_trial(run.page, run.cdp)
            result.update({"trial": index, "block": (index-1)//4+1, "stations": visible})
            trials.append(result)
            print(index, "on" if visible else "off", result["task_ms"], result["raf_p95_ms"], flush=True)
        handle.evaluate("layer => layer.setStationsVisible(true)")
        run.page.wait_for_timeout(500)
        ctx.close(); browser.close()

    blocks=[]
    for b in range(1,4):
        rows=[r for r in trials if r["block"]==b]
        on=statistics.mean(r["task_ms"] for r in rows if r["stations"])
        off=statistics.mean(r["task_ms"] for r in rows if not r["stations"])
        blocks.append({"block":b,"on_mean_ms":round(on,3),"off_mean_ms":round(off,3),
                       "delta_ms":round(on-off,3),"delta_pct":round(100*(on-off)/off,3)})
    onvals=[r["task_ms"] for r in trials if r["stations"]]
    offvals=[r["task_ms"] for r in trials if not r["stations"]]
    onraf=[r["raf_p95_ms"] for r in trials if r["stations"]]
    offraf=[r["raf_p95_ms"] for r in trials if not r["stations"]]
    rng=random.Random(430)
    boot=[]
    for _ in range(10000):
        bs=[rng.choice(blocks)["delta_pct"] for _ in blocks]
        boot.append(statistics.mean(bs))
    delta_pct=statistics.median([b["delta_pct"] for b in blocks])
    raf_delta=statistics.median(onraf)-statistics.median(offraf)
    verdict="PASS" if delta_pct<=8 and raf_delta<=2 else "FAIL"
    out={"verdict":verdict,"method":"same-page ABBA x3, loaded payload retained, Fold outer 475x751 at 6x CPU",
         "trials":trials,"blocks":blocks,
         "summary":{"on_task_median_ms":round(statistics.median(onvals),3),
                    "off_task_median_ms":round(statistics.median(offvals),3),
                    "paired_block_delta_pct_median":round(delta_pct,3),
                    "paired_block_delta_pct_bootstrap_95":[pct(boot,2.5),pct(boot,97.5)],
                    "on_task_range_ms":[round(min(onvals),3),round(max(onvals),3)],
                    "off_task_range_ms":[round(min(offvals),3),round(max(offvals),3)],
                    "raf_p95_delta_ms":round(raf_delta,3)}}
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/"paired-fold-outer.json").write_text(json.dumps(out,indent=2),encoding="utf-8")
    print(json.dumps(out["summary"],indent=2)); print(verdict)
    return 0 if verdict=="PASS" else 1

if __name__ == "__main__":
    raise SystemExit(main())
