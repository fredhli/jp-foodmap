#!/usr/bin/env python3
"""Local cost diagnostic for the 4.3.1a station GridLayer.

This is deliberately a diagnostic, not a Fold hardware gate. It records the
actual headless renderer/backend, CPU throttle, new-view tile work, first
canvas visibility, live canvas counts, and pixel backing byte estimates.

    .venv-wsl/bin/python tests/perf/station_grid_diagnostic.py \
      --baseline /tmp/tabelog-4.2.8-c3eb761/docs \
      --output audit_output/4.3.1a/station-grid-performance.json
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import http.server
import json
import math
import re
import socket
import socketserver
import statistics
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Browser, Page, Route, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
STATION_URL_RE = re.compile(r"/(?:data/stations\.[0-9a-f]{12}|transit/japan-stations)\.json(?:\?|$)")
RAIL_URL_RE = re.compile(r"(?:assets\.jpfoodmap\.com|/transit/japan(?:-(?:low|mid))?\.geojson)")


@dataclass(frozen=True)
class Profile:
    width: int
    height: int
    dpr: float
    mobile: bool
    touch: bool
    cpu_rate: int


PROFILES = {
    "fold-outer-6x": Profile(475, 751, 2.625, True, True, 6),
    "fold-inner-4x": Profile(932, 704, 2.625, True, True, 4),
    "desktop-1x": Profile(1440, 900, 1.0, False, False, 1),
}


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


@contextmanager
def serve(directory: Path):
    class Handler(http.server.SimpleHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

        def log_message(self, *_args):
            pass

        def handle_one_request(self):
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = Server(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def fixture_tile(route: Route) -> None:
    parts = [part for part in urlparse(route.request.url).path.split("/") if part]
    label = "/".join(parts[-3:])
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256">
    <rect width="256" height="256" fill="#edf1f4"/><path d="M0 0H256V256H0ZM0 128H256M128 0V256"
    fill="none" stroke="#8da0ad" stroke-width="2"/><text x="8" y="22" font-family="monospace"
    font-size="14" fill="#34495e">{label}</text></svg>"""
    route.fulfill(status=200, body=svg, content_type="image/svg+xml")


def seed(stations: bool) -> str:
    values = {
        "tabelog.lang": "zh-CN",
        "tabelog.seenIntro": "1",
        "tabelog.oovHintDismissed": "1",
        "tabelog.syncHintDismissed": "1",
        "tabelog.installHint": json.dumps({"never": True}),
        "tabelog.showTransitLong": "0",
        "tabelog.showTransitCity": "0",
        "tabelog.showStations": "1" if stations else "0",
    }
    return "(() => {const v=%s;for(const k in v)localStorage.setItem(k,v[k]);})()" % json.dumps(values)


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * pct / 100) - 1))
    return round(ordered[index], 3)


def summarize(values: list[float]) -> dict:
    return {
        "n": len(values),
        "medianMs": round(statistics.median(values), 3) if values else None,
        "p95Ms": percentile(values, 95),
        "maxMs": round(max(values), 3) if values else None,
    }


def metrics(cdp) -> dict[str, float]:
    return {row["name"]: row["value"] for row in cdp.send("Performance.getMetrics")["metrics"]}


def delta(after: dict[str, float], before: dict[str, float]) -> dict[str, float]:
    keys = [
        "TaskDuration", "ScriptDuration", "LayoutDuration", "RecalcStyleDuration",
        "LayoutCount", "RecalcStyleCount", "JSHeapUsedSize", "Nodes",
    ]
    return {key: round(after.get(key, 0) - before.get(key, 0), 6) for key in keys}


def gpu_info(page: Page) -> dict:
    return page.evaluate("""() => {
      const c=document.createElement('canvas'), gl=c.getContext('webgl');
      const ext=gl && gl.getExtension('WEBGL_debug_renderer_info');
      return {userAgent:navigator.userAgent,platform:navigator.platform,
        hardwareConcurrency:navigator.hardwareConcurrency,
        webglVendor:ext?gl.getParameter(ext.UNMASKED_VENDOR_WEBGL):null,
        webglRenderer:ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):null};
    }""")


def page_detail(page: Page) -> dict | None:
    return page.evaluate("() => window.MapMod && typeof MapMod.stationDetail==='function' ? MapMod.stationDetail() : null")


def canvas_snapshot(page: Page) -> dict:
    return page.evaluate("""() => {
      const rows=Array.from(document.querySelectorAll('canvas[data-station-tile="1"]')).map(c=>({
        width:c.width,height:c.height,bytes:c.width*c.height*4,
        cssWidth:c.getBoundingClientRect().width,cssHeight:c.getBoundingClientRect().height}));
      return {count:rows.length,backingBytesEstimate:rows.reduce((n,x)=>n+x.bytes,0),canvases:rows};
    }""")


def wait_ready(page: Page) -> None:
    page.wait_for_function(
        "() => !!(window.Adapter && Adapter.ready===true && window.MapMod && MapMod.map && window.Data && Data.restaurants && Data.restaurants.length)",
        timeout=120_000,
    )


def run_one(browser: Browser, base: str, profile: Profile, mode: str) -> dict:
    stations_on = mode == "candidate-on"
    context = browser.new_context(
        viewport={"width": profile.width, "height": profile.height},
        screen={"width": profile.width, "height": profile.height},
        device_scale_factor=profile.dpr,
        is_mobile=profile.mobile,
        has_touch=profile.touch,
        locale="zh-CN",
        service_workers="block",
    )
    context.add_init_script(seed(stations_on))
    page = context.new_page()
    page.set_default_timeout(120_000)
    page.route("**basemaps.cartocdn.com/**", fixture_tile)
    for blocked in (
        "accounts.google.com", "tblg.k-img.com", "emojicdn.elk.sh",
        "translate.googleapis.com", "nominatim.openstreetmap.org",
        "api.jpfoodmap.com", "www.google-analytics.com",
    ):
        page.route(f"**{blocked}**", lambda route: route.abort())

    cdp = context.new_cdp_session(page)
    cdp.send("Performance.enable")
    cdp.send("Network.enable")
    if profile.cpu_rate != 1:
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": profile.cpu_rate})
    requests: dict[str, dict] = {}
    station_requests: list[dict] = []
    rail_requests: list[str] = []
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    def request_seen(event) -> None:
        url = event["request"]["url"]
        if STATION_URL_RE.search(url):
            rec = {"requestId": event["requestId"], "encodedDataLength": None}
            requests[event["requestId"]] = rec
            station_requests.append(rec)
        if RAIL_URL_RE.search(url):
            rail_requests.append(url)

    def loading_finished(event) -> None:
        rec = requests.get(event["requestId"])
        if rec is not None:
            rec["encodedDataLength"] = event.get("encodedDataLength")

    cdp.on("Network.requestWillBeSent", request_seen)
    cdp.on("Network.loadingFinished", loading_finished)
    t_nav = time.perf_counter()
    page.goto(base + "/index.html", wait_until="domcontentloaded", timeout=120_000)
    wait_ready(page)
    ready_ms = (time.perf_counter() - t_nav) * 1000
    environment = gpu_info(page)

    t0 = page.evaluate("performance.now()")
    page.evaluate("() => MapMod.map.setView([35.681236,139.767125],14,{animate:false})")
    first_canvas_ms = None
    if stations_on:
        page.wait_for_function(
            "() => {const d=MapMod.stationDetail();return d.loaded&&d.total===8954&&d.liveCanvasCount>0;}"
        )
        first_canvas_ms = page.evaluate("start => performance.now()-start", t0)
    else:
        page.wait_for_timeout(350)
    if mode.startswith("candidate"):
        detail = page_detail(page)
        if stations_on:
            assert detail and detail.get("renderer") == "grid", detail
        else:
            assert not station_requests, station_requests
    assert not rail_requests, rail_requests
    initial_detail = page_detail(page)

    before = metrics(cdp)
    peak = canvas_snapshot(page)
    peak_summary = {"count": peak["count"], "backingBytesEstimate": peak["backingBytesEstimate"]}
    gesture_samples: list[dict] = []
    gestures = page.evaluate("""async () => {
      const map=MapMod.map, wait=ms=>new Promise(r=>setTimeout(r,ms)), out=[];
      for(const offset of [[190,0],[190,120],[-210,0],[-210,-120]]){
        map.panBy(offset,{animate:false}); await wait(140);
        out.push({kind:'pan',offset,detail:typeof MapMod.stationDetail==='function'?MapMod.stationDetail():null});
      }
      map._moveStart(true,false);
      map._move(L.latLng(35.6904,139.7427),14.625,{pinch:true,round:false}); await wait(100);
      map._move(L.latLng(35.681236,139.767125),14,{pinch:true,round:false}); map._moveEnd(true); await wait(180);
      out.push({kind:'fractional-pinch',detail:typeof MapMod.stationDetail==='function'?MapMod.stationDetail():null});
      for(const z of [15,16,14]){map.setZoom(z,{animate:false});await wait(220);out.push({kind:'zoom',z,detail:typeof MapMod.stationDetail==='function'?MapMod.stationDetail():null});}
      map.flyTo([35.6896,139.7006],15,{duration:.22});await wait(500);
      out.push({kind:'flyTo',detail:typeof MapMod.stationDetail==='function'?MapMod.stationDetail():null});
      return out;
    }""")
    for item in gestures:
        snap = canvas_snapshot(page)
        gesture_samples.append({"gesture": item, "canvas": snap})
        observed = item.get("detail") or {}
        peak_summary["count"] = max(peak_summary["count"], int(observed.get("liveCanvasCount") or 0))
        peak_summary["backingBytesEstimate"] = max(
            peak_summary["backingBytesEstimate"], int(observed.get("liveCanvasBytes") or 0)
        )
    page.wait_for_timeout(1000)
    after = metrics(cdp)
    detail = page_detail(page)
    canvases = canvas_snapshot(page)
    durations = list((detail or {}).get("tileDrawDurationMs") or [])
    counter_delta = {}
    if initial_detail and detail:
        for key, value in (detail.get("counters") or {}).items():
            counter_delta[key] = value - (initial_detail.get("counters") or {}).get(key, 0)
    result = {
        "mode": mode,
        "readyMs": round(ready_ms, 3),
        "firstStationCanvasVisibleMs": round(first_canvas_ms, 3) if first_canvas_ms is not None else None,
        "environment": environment,
        "cpuThrottleRate": profile.cpu_rate,
        "network": {
            "stationRequestCount": len(station_requests),
            "stationEncodedBytes": sum(x.get("encodedDataLength") or 0 for x in station_requests),
            "railRequestCount": len(rail_requests),
        },
        "metricDelta": delta(after, before),
        "stationDetail": detail,
        "stationCounterDeltaDuringGestures": counter_delta,
        "tileDrawDurations": summarize([float(x) for x in durations]),
        "liveCanvas": canvases,
        "peakObservedCanvas": peak_summary,
        "gestureSamples": gesture_samples,
        "pageErrors": errors,
    }
    context.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, default=ROOT / "docs")
    parser.add_argument("--baseline", type=Path, default=Path("/tmp/tabelog-4.2.8-c3eb761/docs"))
    parser.add_argument("--profiles", default=",".join(PROFILES))
    parser.add_argument("--output", type=Path,
                        default=ROOT / "audit_output" / "4.3.1a" / "station-grid-performance.json")
    args = parser.parse_args()
    selected = [name.strip() for name in args.profiles.split(",") if name.strip()]
    unknown = sorted(set(selected) - set(PROFILES))
    if unknown:
        parser.error(f"unknown profiles: {unknown}")
    if not (args.candidate / "index.html").exists() or not (args.baseline / "index.html").exists():
        parser.error("candidate and baseline must each point to a built docs directory")

    report: dict = {
        "schema": 1,
        "verdict": "DIAGNOSTIC",
        "limitations": [
            "Headless Chromium is not Fold hardware and cannot pass a Fold performance gate.",
            "liveCanvas.backingBytesEstimate is width*height*4, not measured VRAM/GPU memory.",
            "The labelled SVG basemap fixture makes render work deterministic; it is not CARTO network/decode QA.",
            "New-view pans call GridLayer createTile JavaScript; the counters quantify that work.",
            "One run per cell describes this machine only; no confidence interval is reported.",
            "The localhost server sends the station JSON uncompressed; artifact gzip size is reported separately.",
            "Headless scheduling made first-canvas polling take seconds, so that field is not a network latency gate.",
        ],
        "candidate": str(args.candidate.resolve()),
        "baseline": str(args.baseline.resolve()),
        "profiles": {},
    }
    renderer = (args.candidate / "transit-layer.js").read_bytes()
    html = (args.candidate / "index.html").read_text(encoding="utf-8")
    asset_match = re.search(r"transit-layer\.js\?v=([0-9a-f]+)", html)
    report["renderer"] = {
        "sha256": hashlib.sha256(renderer).hexdigest(),
        "md5": hashlib.md5(renderer).hexdigest(),
        "indexAssetVersionMd5Prefix": asset_match.group(1) if asset_match else None,
    }
    names = sorted(set(re.findall(r"data/(stations\.[0-9a-f]{12}\.json)", html)))
    payloads = [args.candidate / "data" / names[0]] if len(names) == 1 else []
    if payloads and payloads[0].exists():
        raw = payloads[0].read_bytes()
        report["stationPayload"] = {
            "name": payloads[0].name,
            "rawBytes": len(raw),
            "gzipBytes": len(gzip.compress(raw, compresslevel=9, mtime=0)),
        }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        report["browserVersion"] = browser.version
        for profile_name in selected:
            profile = PROFILES[profile_name]
            cells = []
            with serve(args.baseline.resolve()) as base:
                print(f"perf {profile_name}: baseline", flush=True)
                cells.append(run_one(browser, base, profile, "baseline"))
            with serve(args.candidate.resolve()) as base:
                print(f"perf {profile_name}: candidate-off", flush=True)
                cells.append(run_one(browser, base, profile, "candidate-off"))
                print(f"perf {profile_name}: candidate-on", flush=True)
                cells.append(run_one(browser, base, profile, "candidate-on"))
            report["profiles"][profile_name] = {"profile": profile.__dict__, "runs": cells}
        browser.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"station_grid_diagnostic.py DIAGNOSTIC: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
