#!/usr/bin/env python3
"""Full-data stations=0/1 performance gate for the 4.3.0 station layer.

The benchmark always serves the production candidate station payload, gzip
encoded and throttled like a modest mobile connection.  Both sides use the
same docs/ build and differ only in ``tabelog.showStations``.  Railway toggles
are forced off and every railway-LOD request is recorded as a hard failure.

Default run (about 8 minutes because every A/B side includes a real 60-second
idle energy-proxy window)::

    .venv-wsl/bin/python tests/perf/station_layer_bench.py

Use ``--idle-seconds 3 --repeats 1`` only while developing the harness.  Such
a shortened run is labelled diagnostic and never returns PASS.
"""

from __future__ import annotations

import argparse
import gzip
import http.server
import json
import math
import socket
import socketserver
import statistics
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
STATIONS = DOCS / "data" / "stations.b2b32cdfaeb0.json"
DEFAULT_OUT = ROOT / "tests" / "perf" / "results" / "station-layer-4.3.0"

# Slow-but-usable mobile data.  The station response alone is shaped because
# throttling the 11 MiB restaurant/popup corpus would measure an unrelated
# application-data transfer rather than the marginal station-layer cost.
MOBILE_LATENCY_S = 0.150
MOBILE_BYTES_PER_S = 200_000  # 1.6 Mbit/s downstream
CHUNK_BYTES = 16_384

PROFILES = {
    "fold-inner-4x": {
        "viewport": {"width": 932, "height": 704},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "cpu_rate": 4,
    },
    "fold-outer-6x": {
        "viewport": {"width": 475, "height": 751},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "cpu_rate": 6,
    },
    "desktop-4x": {
        "viewport": {"width": 1440, "height": 900},
        "device_scale_factor": 1,
        "is_mobile": False,
        "has_touch": False,
        "cpu_rate": 4,
    },
}

READY_JS = (
    "() => !!(window.Adapter && Adapter.ready === true"
    " && window.Data && Array.isArray(Data.restaurants)"
    " && Data.restaurants.length > 0 && window.MapMod && MapMod.map)"
)

INIT_JS = r"""
(() => {
  const entries = %s;
  try { for (const k in entries) localStorage.setItem(k, entries[k]); } catch (_) {}

  const nativeRaf = window.requestAnimationFrame.bind(window);
  const nativeCaf = window.cancelAnimationFrame.bind(window);
  const pendingRaf = new Set();
  window.requestAnimationFrame = function (cb) {
    let id = nativeRaf(function (now) { pendingRaf.delete(id); cb(now); });
    pendingRaf.add(id); return id;
  };
  window.cancelAnimationFrame = function (id) { pendingRaf.delete(id); return nativeCaf(id); };

  const nativeSI = window.setInterval.bind(window);
  const nativeCI = window.clearInterval.bind(window);
  const intervals = new Set();
  window.setInterval = function () { const id = nativeSI(...arguments); intervals.add(id); return id; };
  window.clearInterval = function (id) { intervals.delete(id); return nativeCI(id); };

  window.__stationPerf = {
    pendingRaf, intervals, frames: [], frameOn: false, longTasks: []
  };
  try {
    new PerformanceObserver(list => {
      for (const e of list.getEntries()) window.__stationPerf.longTasks.push(e.duration);
    }).observe({entryTypes:['longtask']});
  } catch (_) {}
  window.__stationPerfStart = function () {
    const p = window.__stationPerf;
    p.frames = []; p.longTasks = []; p.frameOn = true;
    let last = performance.now();
    function tick(now) {
      if (!p.frameOn) return;
      p.frames.push(now - last); last = now;
      requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  };
  window.__stationPerfStop = function () {
    const p = window.__stationPerf; p.frameOn = false;
    return {frames:p.frames.slice(1), longTasks:p.longTasks.slice()};
  };
})()
"""


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, max(0, round((len(values) - 1) * p / 100)))
    return round(values[idx], 3)


def median(values: list[float]) -> float:
    return round(statistics.median(values), 3)


def perf_metrics(cdp) -> dict[str, float]:
    return {m["name"]: m["value"] for m in cdp.send("Performance.getMetrics")["metrics"]}


def force_gc(cdp, page: Page) -> None:
    cdp.send("HeapProfiler.collectGarbage")
    page.wait_for_timeout(250)
    cdp.send("HeapProfiler.collectGarbage")
    page.wait_for_timeout(250)


class StationServer:
    def __init__(self) -> None:
        self.raw = STATIONS.read_bytes()
        self.gzip = gzip.compress(self.raw, compresslevel=9, mtime=0)
        self.lock = threading.Lock()
        self.station_requests: list[dict[str, Any]] = []

    def log_station(self, path: str) -> None:
        with self.lock:
            self.station_requests.append({"path": path, "wall": time.time()})


class ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


@contextmanager
def serve_docs(payload: StationServer):
    class Handler(http.server.SimpleHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(DOCS), **kwargs)

        def log_message(self, *_args):
            pass

        def do_GET(self):
            clean = self.path.split("?", 1)[0]
            if clean == "/data/stations.b2b32cdfaeb0.json":
                payload.log_station(self.path)
                time.sleep(MOBILE_LATENCY_S)
                body = payload.gzip
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public,max-age=31536000,immutable")
                self.end_headers()
                try:
                    for start in range(0, len(body), CHUNK_BYTES):
                        piece = body[start : start + CHUNK_BYTES]
                        self.wfile.write(piece)
                        self.wfile.flush()
                        time.sleep(len(piece) / MOBILE_BYTES_PER_S)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            return super().do_GET()

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = ThreadingServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def seed(stations: bool) -> dict[str, str]:
    return {
        "tabelog.lang": "zh-CN",
        "tabelog.seenIntro": "1",
        "tabelog.oovHintDismissed": "1",
        "tabelog.syncHintDismissed": "1",
        "tabelog.installHint": json.dumps({"never": True}),
        "tabelog.showTransitLong": "0",
        "tabelog.showTransitCity": "0",
        "tabelog.showStations": "1" if stations else "0",
    }


def new_context(browser: Browser, profile: dict[str, Any], stations: bool) -> BrowserContext:
    ctx = browser.new_context(
        viewport=profile["viewport"],
        device_scale_factor=profile["device_scale_factor"],
        is_mobile=profile["is_mobile"],
        has_touch=profile["has_touch"],
        locale="zh-CN",
        service_workers="block",
    )
    ctx.add_init_script(INIT_JS % json.dumps(seed(stations), ensure_ascii=True))
    # Do not use Playwright context.route here: enabling routing disables the
    # browser HTTP cache and would turn the warm-cache half into a second cold
    # station transfer.  External traffic is blocked through CDP per page.
    return ctx


@dataclass
class PageRun:
    page: Page
    cdp: Any
    ready_ms: float
    station_ms: float | None
    station_requests: list[dict[str, Any]]
    rail_requests: list[str]
    errors: list[str]


def open_page(ctx: BrowserContext, base: str, cpu_rate: int, stations: bool) -> PageRun:
    page = ctx.new_page()
    page.set_default_timeout(120_000)
    cdp = ctx.new_cdp_session(page)
    cdp.send("Performance.enable")
    cdp.send("HeapProfiler.enable")
    cdp.send("Network.enable")
    cdp.send("Network.setCacheDisabled", {"cacheDisabled": False})
    cdp.send("Network.setBlockedURLs", {"urls": [
        "*://*.basemaps.cartocdn.com/*", "*://accounts.google.com/*",
        "*://api.jpfoodmap.com/*", "*://*.jpfoodmap.com/*",
        "*://tblg.k-img.com/*", "*://emojicdn.elk.sh/*",
        "*://translate.googleapis.com/*", "*://nominatim.openstreetmap.org/*",
        "*://www.google-analytics.com/*",
    ]})
    if cpu_rate != 1:
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": cpu_rate})

    requests: list[dict[str, Any]] = []
    rail_requests: list[str] = []
    errors: list[str] = []

    def request_seen(ev):
        url = ev["request"]["url"]
        if "stations.b2b32cdfaeb0.json" in url:
            requests.append({"requestId": ev["requestId"], "url": url})
        if "assets.jpfoodmap.com" in url or "/transit/japan-" in url and url.endswith(".geojson"):
            rail_requests.append(url)

    def response_seen(ev):
        for rec in requests:
            if rec["requestId"] == ev["requestId"]:
                rsp = ev["response"]
                rec.update({
                    "fromDiskCache": bool(rsp.get("fromDiskCache")),
                    "fromServiceWorker": bool(rsp.get("fromServiceWorker")),
                    "encodedDataLengthHeaders": rsp.get("encodedDataLength", 0),
                })

    def loading_finished(ev):
        for rec in requests:
            if rec["requestId"] == ev["requestId"]:
                rec["encodedDataLength"] = ev.get("encodedDataLength", 0)

    cdp.on("Network.requestWillBeSent", request_seen)
    cdp.on("Network.responseReceived", response_seen)
    cdp.on("Network.loadingFinished", loading_finished)
    page.on("pageerror", lambda err: errors.append(str(err)))

    t0 = time.perf_counter()
    page.goto(base + "/index.html", wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_function(READY_JS, timeout=120_000)
    ready_ms = (time.perf_counter() - t0) * 1000
    # The generated page intentionally boots at the nationwide view.  A
    # default-on station layer must stay dormant there, so startup is recorded
    # first and the station transfer is then triggered by an explicit,
    # identical Tokyo z14 transition on both A/B sides.
    page.evaluate("() => MapMod.map.setView([35.6812,139.7671],14,{animate:false})")
    station_ms = None
    if stations:
        try:
            page.wait_for_function(
                "() => { const d=MapMod.stationDetail(); return d.loaded && d.total===8954; }",
                timeout=30_000,
            )
        except Exception:
            debug = page.evaluate("""() => ({
              zoom:MapMod.map.getZoom(), station:MapMod.stationDetail(),
              layers:App.state.layers,
              pref:localStorage.getItem('tabelog.showStations')
            })""")
            raise RuntimeError(
                "station payload did not become usable: " +
                json.dumps({"page": debug, "requests": requests,
                            "rail": rail_requests, "errors": errors}, ensure_ascii=False)
            )
        station_ms = (time.perf_counter() - t0) * 1000
    page.wait_for_timeout(750)
    return PageRun(page, cdp, round(ready_ms, 3),
                   round(station_ms, 3) if station_ms is not None else None,
                   requests, rail_requests, errors)


def drag(page: Page, x0: float, y0: float, x1: float, y1: float, steps: int = 10) -> None:
    page.mouse.move(x0, y0)
    page.mouse.down()
    for i in range(1, steps + 1):
        f = i / steps
        page.mouse.move(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f)
        page.wait_for_timeout(16)
    page.mouse.up()


def pan_trial(page: Page, cdp) -> dict[str, Any]:
    page.evaluate("() => MapMod.map.setView([35.6812,139.7671],14,{animate:false})")
    page.wait_for_timeout(700)
    rect = page.evaluate("() => {const r=MapMod.map._container.getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height}}")
    cx, cy = rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2
    dx = min(280, rect["w"] * 0.46)
    page.evaluate("""() => {
      const root = document.documentElement;
      const initial = document.querySelectorAll('*').length;
      window.__stMut = {adds:0, removes:0, current:initial, peak:initial};
      window.__stMo = new MutationObserver(list => {
        let adds=0, removes=0;
        for (const m of list) { adds += m.addedNodes.length; removes += m.removedNodes.length; }
        window.__stMut.adds += adds; window.__stMut.removes += removes;
        window.__stMut.current += adds - removes;
        window.__stMut.peak = Math.max(window.__stMut.peak, window.__stMut.current);
      });
      window.__stMo.observe(root,{subtree:true,childList:true});
      window.__stationPerfStart();
    }""")
    m0 = perf_metrics(cdp)
    for i in range(10):
        sign = 1 if i % 2 == 0 else -1
        drag(page, cx + sign * dx / 2, cy, cx - sign * dx / 2, cy)
        page.wait_for_timeout(220)
    page.wait_for_timeout(500)
    m1 = perf_metrics(cdp)
    timing = page.evaluate("() => window.__stationPerfStop()")
    mutations = page.evaluate("() => { window.__stMo.disconnect(); return window.__stMut; }")
    frames = timing["frames"]
    longs = timing["longTasks"]
    return {
        "task_ms": round((m1["TaskDuration"] - m0["TaskDuration"]) * 1000, 3),
        "script_ms": round((m1["ScriptDuration"] - m0["ScriptDuration"]) * 1000, 3),
        "layout_ms": round((m1["LayoutDuration"] - m0["LayoutDuration"]) * 1000, 3),
        "raf_n": len(frames),
        "raf_p95_ms": percentile(frames, 95),
        "raf_max_ms": round(max(frames), 3) if frames else None,
        "long_task_count": len(longs),
        "long_task_total_ms": round(sum(longs), 3),
        "long_task_max_ms": round(max(longs), 3) if longs else 0,
        "mutations": mutations,
    }


def trace_idle(page: Page, cdp, seconds: int, trace_path: Path,
               station_request_count: callable) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    complete = threading.Event()
    cdp.on("Tracing.dataCollected", lambda ev: events.extend(ev.get("value", [])))
    cdp.on("Tracing.tracingComplete", lambda _ev: complete.set())
    cdp.send("Tracing.start", {"categories": "devtools.timeline,toplevel,blink.user_timing"})
    m0 = perf_metrics(cdp)
    net0 = station_request_count()
    tracker0 = page.evaluate("""() => ({
      raf:__stationPerf.pendingRaf.size, intervals:__stationPerf.intervals.size,
      animations:document.getAnimations().filter(a=>a.playState==='running').length
    })""")
    page.wait_for_timeout(seconds * 1000)
    m1 = perf_metrics(cdp)
    tracker1 = page.evaluate("""() => ({
      raf:__stationPerf.pendingRaf.size, intervals:__stationPerf.intervals.size,
      animations:document.getAnimations().filter(a=>a.playState==='running').length
    })""")
    net1 = station_request_count()
    cdp.send("Tracing.end")
    deadline = time.time() + 15
    while not complete.is_set() and time.time() < deadline:
        page.wait_for_timeout(100)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_bytes(gzip.compress(json.dumps({"traceEvents": events}).encode(), 6, mtime=0))
    draw_names = {"DrawFrame", "BeginFrame", "RequestMainThreadFrame", "AnimationFrame"}
    draw_counts = {name: 0 for name in sorted(draw_names)}
    for event in events:
        if event.get("name") in draw_counts:
            draw_counts[event["name"]] += 1
    try:
        trace_label = str(trace_path.relative_to(ROOT))
    except ValueError:
        trace_label = str(trace_path)
    return {
        "seconds": seconds,
        "task_ms": round((m1["TaskDuration"] - m0["TaskDuration"]) * 1000, 3),
        "script_ms": round((m1["ScriptDuration"] - m0["ScriptDuration"]) * 1000, 3),
        "layout_ms": round((m1["LayoutDuration"] - m0["LayoutDuration"]) * 1000, 3),
        "station_network_requests": net1 - net0,
        "tracker_start": tracker0,
        "tracker_end": tracker1,
        "draw_events": draw_counts,
        "trace_gzip": trace_label,
    }


def full_measure(run: PageRun, idle_seconds: int, pan_trials: int, trace_path: Path,
                 station_request_count: callable) -> dict[str, Any]:
    page, cdp = run.page, run.cdp
    force_gc(cdp, page)
    stable = perf_metrics(cdp)
    dom = page.evaluate("""() => ({
      elements:document.querySelectorAll('*').length,
      stationMarkers:document.querySelectorAll('.transit-station-marker').length,
      stationLabels:document.querySelectorAll('.transit-station-label').length,
      stationDetail:MapMod.stationDetail()
    })""")
    pans = [pan_trial(page, cdp) for _ in range(pan_trials)]
    page.evaluate("() => MapMod.map.setView([35.6812,139.7671],14,{animate:false})")
    page.wait_for_timeout(1000)
    force_gc(cdp, page)
    end = perf_metrics(cdp)
    idle = trace_idle(page, cdp, idle_seconds, trace_path, station_request_count)
    return {
        "heap_used_mib": round(end["JSHeapUsedSize"] / 1048576, 3),
        "heap_total_mib": round(end["JSHeapTotalSize"] / 1048576, 3),
        "nodes_metric": end.get("Nodes"),
        "dom": dom,
        "pan_trials": pans,
        "pan_median": {
            "task_ms": median([p["task_ms"] for p in pans]),
            "raf_p95_ms": median([p["raf_p95_ms"] for p in pans if p["raf_p95_ms"] is not None]),
            "long_task_total_ms": median([p["long_task_total_ms"] for p in pans]),
        },
        "post_pan_heap_growth_mib": round((end["JSHeapUsedSize"] - stable["JSHeapUsedSize"]) / 1048576, 3),
        "idle": idle,
    }


def run_case(browser: Browser, base: str, payload: StationServer, profile_name: str,
             profile: dict[str, Any], stations: bool, repeats: int,
             idle_seconds: int, pan_trials: int, out: Path) -> dict[str, Any]:
    cold_ready, warm_ready, cold_station, warm_station = [], [], [], []
    navigations = []
    final_run = None
    final_ctx = None
    server_before_case = len(payload.station_requests)
    for rep in range(repeats):
        ctx = new_context(browser, profile, stations)
        server_before_cold = len(payload.station_requests)
        cold = open_page(ctx, base, profile["cpu_rate"], stations)
        server_after_cold = len(payload.station_requests)
        cold_ready.append(cold.ready_ms)
        if cold.station_ms is not None:
            cold_station.append(cold.station_ms)
        navigations.append({
            "cache": "cold", "repeat": rep + 1, "ready_ms": cold.ready_ms,
            "station_usable_ms": cold.station_ms, "station_request_events": cold.station_requests,
            "server_station_requests": server_after_cold - server_before_cold,
            "rail_requests": cold.rail_requests, "errors": cold.errors,
        })
        cold.page.close()

        server_before_warm = len(payload.station_requests)
        warm = open_page(ctx, base, profile["cpu_rate"], stations)
        server_after_warm = len(payload.station_requests)
        warm_ready.append(warm.ready_ms)
        if warm.station_ms is not None:
            warm_station.append(warm.station_ms)
        navigations.append({
            "cache": "warm", "repeat": rep + 1, "ready_ms": warm.ready_ms,
            "station_usable_ms": warm.station_ms, "station_request_events": warm.station_requests,
            "server_station_requests": server_after_warm - server_before_warm,
            "rail_requests": warm.rail_requests, "errors": warm.errors,
        })
        if rep == repeats - 1:
            final_run, final_ctx = warm, ctx
        else:
            warm.page.close()
            ctx.close()
    assert final_run is not None and final_ctx is not None
    label = "stations-on" if stations else "stations-off"
    trace_path = out / "traces" / f"{profile_name}-{label}-idle.json.gz"
    measurements = full_measure(final_run, idle_seconds, pan_trials, trace_path,
                                lambda: len(payload.station_requests))
    final_run.page.close()
    final_ctx.close()
    return {
        "profile": profile_name,
        "stations": stations,
        "startup": {
            "cold_ready_ms": cold_ready,
            "cold_ready_median_ms": median(cold_ready),
            "warm_ready_ms": warm_ready,
            "warm_ready_median_ms": median(warm_ready),
            "cold_station_usable_ms": cold_station,
            "cold_station_usable_median_ms": median(cold_station) if cold_station else None,
            "warm_station_usable_ms": warm_station,
            "warm_station_usable_median_ms": median(warm_station) if warm_station else None,
        },
        "navigations": navigations,
        "server_station_requests": len(payload.station_requests) - server_before_case,
        "measurements": measurements,
    }


def compare_profile(off: dict[str, Any], on: dict[str, Any], strict: bool) -> dict[str, Any]:
    om, nm = off["measurements"], on["measurements"]
    pan_off, pan_on = om["pan_median"], nm["pan_median"]
    task_delta = pan_on["task_ms"] - pan_off["task_ms"]
    task_pct = 100 * task_delta / pan_off["task_ms"] if pan_off["task_ms"] else math.inf
    cpu_rate = PROFILES[on["profile"]]["cpu_rate"]
    draw_off = sum(om["idle"]["draw_events"].values())
    draw_on = sum(nm["idle"]["draw_events"].values())
    checks = {
        "cold_start_delta_le_100ms": on["startup"]["cold_ready_median_ms"] - off["startup"]["cold_ready_median_ms"] <= 100,
        "warm_start_delta_le_100ms": on["startup"]["warm_ready_median_ms"] - off["startup"]["warm_ready_median_ms"] <= 100,
        "station_one_request_per_cold_nav": all(
            n["server_station_requests"] == 1 for n in on["navigations"] if n["cache"] == "cold"),
        "station_zero_server_requests_warm": all(
            n["server_station_requests"] == 0 for n in on["navigations"] if n["cache"] == "warm"),
        "station_off_zero_requests": off["server_station_requests"] == 0,
        "station_only_zero_rail_lod": not any(
            n["rail_requests"] for n in on["navigations"]),
        "pan_task_relative_le_8pct": task_pct <= 8,
        "pan_task_absolute_delta_le_15ms": task_delta <= 15,
        "raf_p95_delta_le_2ms": pan_on["raf_p95_ms"] - pan_off["raf_p95_ms"] <= 2,
        "heap_delta_le_5mib": nm["heap_used_mib"] - om["heap_used_mib"] <= 5,
        "dom_delta_le_750": nm["dom"]["elements"] - om["dom"]["elements"] <= 750,
        "idle_task_delta_le_30ms": nm["idle"]["task_ms"] - om["idle"]["task_ms"] <= 30,
        "idle_zero_station_network": nm["idle"]["station_network_requests"] == 0,
        "no_extra_pending_raf": nm["idle"]["tracker_end"]["raf"] <= om["idle"]["tracker_end"]["raf"],
        "no_extra_interval": nm["idle"]["tracker_end"]["intervals"] <= om["idle"]["tracker_end"]["intervals"],
        "no_extra_animation": nm["idle"]["tracker_end"]["animations"] <= om["idle"]["tracker_end"]["animations"],
        "no_extra_draw_frames": draw_on <= draw_off,
        "no_page_errors": not any(n["errors"] for n in off["navigations"] + on["navigations"]),
    }
    deltas = {
        "cold_start_ms": round(on["startup"]["cold_ready_median_ms"] - off["startup"]["cold_ready_median_ms"], 3),
        "warm_start_ms": round(on["startup"]["warm_ready_median_ms"] - off["startup"]["warm_ready_median_ms"], 3),
        "pan_task_ms_per_10_drags": round(task_delta, 3),
        "pan_task_normalized_ms_per_10_drags": round(task_delta / cpu_rate, 3),
        "pan_task_percent": round(task_pct, 3),
        "raf_p95_ms": round(pan_on["raf_p95_ms"] - pan_off["raf_p95_ms"], 3),
        "heap_mib": round(nm["heap_used_mib"] - om["heap_used_mib"], 3),
        "dom_elements": nm["dom"]["elements"] - om["dom"]["elements"],
        "idle_task_ms": round(nm["idle"]["task_ms"] - om["idle"]["task_ms"], 3),
        "idle_draw_events": draw_on - draw_off,
    }
    return {"deltas": deltas, "checks": checks,
            "pass": strict and all(checks.values())}


def markdown_report(result: dict[str, Any]) -> str:
    lines = [
        "# 4.3.0 station-layer performance gate",
        "",
        f"Verdict: **{result['verdict']}**",
        "",
        ("This is the release-grade run." if result["strict"] else
         "Diagnostic only: idle window/repeat count was shortened, so PASS is intentionally unavailable."),
        "",
        f"Payload: {result['payload']['stations']} stations, {result['payload']['raw_bytes']} B raw, "
        f"{result['payload']['gzip_bytes']} B gzip; one immutable request.",
        "",
        "| profile | cold start Δ | warm start Δ | pan CPU Δ / 10 drags | pan Δ | rAF p95 Δ | heap Δ | DOM Δ | idle CPU Δ | result |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for name, comp in result["comparisons"].items():
        d = comp["deltas"]
        lines.append(
            f"| {name} | {d['cold_start_ms']:.1f} ms | {d['warm_start_ms']:.1f} ms | "
            f"{d['pan_task_ms_per_10_drags']:.1f} ms | {d['pan_task_percent']:.1f}% | "
            f"{d['raf_p95_ms']:.2f} ms | {d['heap_mib']:.2f} MiB | {d['dom_elements']} | "
            f"{d['idle_task_ms']:.1f} ms | {'PASS' if comp['pass'] else 'FAIL'} |"
        )
    lines.extend(["", "## Failed checks", ""])
    failed = False
    for name, comp in result["comparisons"].items():
        for check, ok in comp["checks"].items():
            if not ok:
                failed = True
                lines.append(f"- {name}: `{check}`")
    if not failed:
        lines.append("None.")
    lines.extend([
        "", "## Method", "",
        "Same docs/ build, formal full payload, stations=0/1 localStorage A/B, railway buckets off. "
        "Cold and browser-cache-warm starts use medians. The station response is gzip + 150 ms RTT + "
        "1.6 Mbit/s shaping. Each steady page is force-GC'd, dragged 10 times in three trials, and left "
        f"idle for {result['idle_seconds']} seconds under tracing. Fold inner uses 4x CPU, Fold outer 6x, desktop 4x.",
        "",
        "Raw JSON and idle traces are beside this report.",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--idle-seconds", type=int, default=60)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--pan-trials", type=int, default=3)
    parser.add_argument("--profile", action="append", choices=sorted(PROFILES))
    args = parser.parse_args()
    profiles = args.profile or list(PROFILES)
    args.out.mkdir(parents=True, exist_ok=True)
    strict = (args.idle_seconds >= 60 and args.repeats >= 3 and
              args.pan_trials >= 3 and set(profiles) == set(PROFILES))

    payload = StationServer()
    parsed = json.loads(payload.raw)
    cases: dict[str, dict[str, Any]] = {}
    with serve_docs(payload) as base, sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-background-networking"])
        for profile_name in profiles:
            profile = PROFILES[profile_name]
            cases[profile_name] = {}
            # Alternate the feature side first/second across profiles to reduce
            # one-directional thermal or filesystem-cache bias.
            order = (True, False) if len(cases) % 2 else (False, True)
            for stations in order:
                label = "on" if stations else "off"
                print(f"[{profile_name}] stations={int(stations)}", flush=True)
                cases[profile_name][label] = run_case(
                    browser, base, payload, profile_name, profile, stations,
                    args.repeats, args.idle_seconds, args.pan_trials, args.out,
                )
            comp = compare_profile(cases[profile_name]["off"], cases[profile_name]["on"], strict)
            print(json.dumps(comp, ensure_ascii=False, indent=2), flush=True)
        browser.close()

    comparisons = {
        name: compare_profile(pair["off"], pair["on"], strict)
        for name, pair in cases.items()
    }
    all_checks = all(all(c["checks"].values()) for c in comparisons.values())
    verdict = "PASS" if strict and all_checks else ("FAIL" if strict else "DIAGNOSTIC")
    result = {
        "verdict": verdict,
        "strict": strict,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "idle_seconds": args.idle_seconds,
        "repeats": args.repeats,
        "pan_trials": args.pan_trials,
        "payload": {
            "path": str(STATIONS.relative_to(ROOT)),
            "stations": len(parsed["stations"]),
            "raw_bytes": len(payload.raw),
            "gzip_bytes": len(payload.gzip),
            "sha256_name": STATIONS.name,
            "mobile_latency_ms": round(MOBILE_LATENCY_S * 1000),
            "mobile_downlink_mbit_s": MOBILE_BYTES_PER_S * 8 / 1_000_000,
        },
        "profiles": {name: PROFILES[name] for name in profiles},
        "cases": cases,
        "comparisons": comparisons,
    }
    (args.out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / "REPORT.md").write_text(markdown_report(result), encoding="utf-8")
    print(f"{verdict}: {args.out / 'REPORT.md'}")
    return 0 if verdict in {"PASS", "DIAGNOSTIC"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
