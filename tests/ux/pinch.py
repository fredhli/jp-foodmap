"""3.2.3: page-level pinch zoom is off everywhere; the map's own pinch is not.

The owner's report: in the Android shell, a two-finger spread — especially in
the first seconds, with the map still at the whole-of-Japan zoom — scaled the
whole document, sidebar and all, instead of the map. 3.2.3 turns page zoom off
in four places (meta viewport, `touch-action` on html/body, document-level
`gesture*` guards for iOS, and the WebView's own zoom settings); this asserts
the three that live in the page.

What can and cannot be measured here
------------------------------------
Headless Chromium delivers a synthesized pinch to the page (Leaflet zooms from
it, which is assertion (d)) but never applies *browser* pinch zoom:
`visualViewport.scale` reads 1 even on a page that explicitly allows zoom, and
it read 1 on 3.2.2 too. `Emulation.setPageScaleFactor` does move it, so the
number is reported, not simulated — it is recorded as a regression tripwire,
never as the proof.

The proof is the mechanism Chrome actually consults before it starts a pinch:

  * the `<meta name=viewport>` string — `user-scalable=no, maximum-scale=1.0`
    pins the scale, and it is the only one of the three the Android WebView
    reads;
  * the effective `touch-action` at the gesture point, i.e. the intersection
    down the ancestor chain from `elementFromPoint`. A chain that still admits
    `pinch-zoom` (`auto` or `manipulation` all the way up) is a surface the
    browser will happily zoom; `pan-x pan-y` is not, and `.leaflet-container`
    is `none`, which is why the map's own pinch still works — Leaflet drives it
    from raw touch events;
  * for iOS, whether a `gesturestart` dispatched at the surface is
    `defaultPrevented` by the time it reaches the document. WebKit is the only
    engine that fires those events, and Playwright cannot synthesize a real
    trackpad/touch pinch in it, so the guard is checked by dispatch.

Cases, per viewport (932x704 and 475x751, DPR 2.625, mobile + touch):

  (a0) first frame  - an rAF probe installed before any page script records
                      the meta, html `touch-action` and the document guard at
                      the first painted frame, plus whether Leaflet existed yet
  (a)  pre-init     - `L.map` is poisoned so the container is never created:
                      the whole UI is up, the map is not, gestures land on the
                      chrome
  (b)  language gate- first visit, no `tabelog.lang`, gesture on the card
  (c)  home chrome  - intro bar, region chips, search pill, segmented pill, FABs
  (d)  the map      - whole-of-Japan (minZoom) and Tokyo z13: the one surface
                      where the gesture must change something, and what it
                      changes is `map.getZoom()`
  (e)  drawer open  - gesture on the result list
  (f)  detail card  - gesture on the card
  (g)  desktop      - 1440x900 ctrl+wheel: zooms the map, and only the map

CPU throttling (4x at 932, 6x at 475) is applied around the gestures rather
than across the whole load: the point is to reproduce a busy main thread while
the compositor handles the pinch, and a 6x-throttled cold start of a 1 MB page
costs minutes for nothing.

    .venv-wsl/bin/python tests/ux/pinch.py --label after --json OUT.json
    .venv-wsl/bin/python tests/ux/pinch.py --browser webkit --label after
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import lib_browser                                    # noqa: E402
from playwright.sync_api import sync_playwright       # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--browser", choices=["chromium", "webkit"], default="chromium")
ap.add_argument("--docs", type=Path, default=ROOT / "docs")
ap.add_argument("--label", default="after",
                help="'before' records without asserting; 'after' gates")
ap.add_argument("--json", type=Path, help="write every record here")
args = ap.parse_args()
lib_browser.DOCS = args.docs.resolve()
GATE = args.label != "before"

MAP = ("window[Object.keys(window).find("
       "k => /^map_[a-f0-9]/.test(k) && window[k] && window[k].getZoom)]")

# The ancestor-chain intersection Chrome computes before it will start a
# pinch. `none` ends the walk (nothing is allowed below it, which is the
# Leaflet container's case); anything that is not `auto` / `manipulation` /
# an explicit `pinch-zoom` list drops the pinch bit.
PROBE = """(pt) => {
  const el = document.elementFromPoint(pt.x, pt.y);
  if (!el) return {hit: null, pinchAllowed: null, chain: []};
  const chain = [];
  let allowed = true;
  for (let n = el; n; n = n.parentElement) {
    const ta = getComputedStyle(n).touchAction;
    chain.push((n.id ? '#' + n.id : n.tagName.toLowerCase()) + ':' + ta);
    if (ta === 'none') { allowed = false; break; }
    if (ta !== 'auto' && ta !== 'manipulation' && ta.indexOf('pinch-zoom') < 0) {
      allowed = false;
    }
  }
  return {hit: (el.id ? '#' + el.id : el.tagName.toLowerCase()), pinchAllowed: allowed,
          chain: chain};
}"""

# Dispatched at the surface, cancelable and bubbling: the assertion is that
# something between there and the document cancels it. iOS Safari is the only
# engine that fires these for real.
GESTURE = """(sel) => {
  const t = sel ? document.querySelector(sel) : document;
  if (!t) return null;
  const ev = new Event('gesturestart', {cancelable: true, bubbles: true});
  return !t.dispatchEvent(ev);
}"""

VIEWPORT_META = ("() => { const m = document.querySelector('meta[name=viewport]');"
                 "        return m ? m.content.replace(/\\s+/g, ' ').trim() : null; }")

# Installed before any page script: the first animation frame is the earliest
# moment the page has been laid out, and on a throttled main thread it lands
# well before the folium map script at the end of the document.
FIRST_FRAME = """
requestAnimationFrame(function () {
  var meta = document.querySelector('meta[name=viewport]');
  var ev = new Event('gesturestart', {cancelable: true, bubbles: true});
  window.__pinchFirstFrame = {
    metaAtFirstFrame: meta ? meta.content.replace(/\\s+/g, ' ').trim() : null,
    htmlTouchAction: getComputedStyle(document.documentElement).touchAction,
    bodyTouchAction: document.body
      ? getComputedStyle(document.body).touchAction : null,
    gestureGuarded: !document.dispatchEvent(ev),
    leafletUp: !!document.querySelector('.leaflet-container'),
    readyState: document.readyState
  };
});
"""

# Leaflet loads and every piece of UI JS that needs it works; only the map
# constructor is poisoned, so the container is never created. That is the
# "app started, map has not" window the owner's report is about.
HOLD_MAP = """
(function () {
  var real;
  Object.defineProperty(window, 'L', {
    configurable: true,
    get: function () { return real; },
    set: function (v) {
      real = v;
      try {
        Object.defineProperty(v, 'map', {
          configurable: true,
          get: function () {
            return function () { throw new Error('pinch probe: map init held'); };
          },
          set: function () {}
        });
      } catch (_) {}
    }
  });
})();
"""

SEED = {"tabelog.lang": "zh-CN", "tabelog.seenIntro": "1"}
records: list[dict] = []
failures: list[str] = []


def check(ok: bool, msg: str) -> None:
    if ok:
        return
    if GATE:
        failures.append(msg)
    else:
        print(f"    (before) {msg}")


def centre(page, selector: str):
    box = page.evaluate("""(sel) => {
        const e = document.querySelector(sel);
        if (!e) return null;
        const r = e.getBoundingClientRect();
        if (!r.width || !r.height) return null;
        const s = getComputedStyle(e);
        if (s.visibility === 'hidden' || s.display === 'none') return null;
        return {x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)};
    }""", selector)
    return box


def pinch(page, cdp, case: str, vp: str, surface: str, point, *, map_expect=None):
    """One gesture. Records the page scale either side, the touch-action
    verdict at the point, the iOS guard, and what the map did.

    `map_expect` is 'zoom' on the map itself and 'hold' on a surface that
    fully covers it (the language gate, the drawer, the card). It is left
    None on the map-level chrome — the chip row, the pill, the FAB column —
    on purpose: a synthesized pinch spreads its two points well past a 32px
    tall strip, so the outer one lands on the map behind and Leaflet zooms.
    That is what a real finger does there too, and it is not what this suite
    is about; the page scale and the touch-action verdict are.
    """
    if point is None:
        records.append({"case": case, "viewport": vp, "surface": surface,
                        "skipped": "not on screen"})
        return
    probe = page.evaluate(PROBE, point)
    has_map = page.evaluate(f"() => !!({MAP})")
    zoom_before = page.evaluate(f"() => {MAP}.getZoom()") if has_map else None
    scale_before = page.evaluate("() => visualViewport.scale")
    if cdp is not None:
        cdp.send("Input.synthesizePinchGesture",
                 {"x": point["x"], "y": point["y"], "scaleFactor": 2,
                  "relativeSpeed": 400, "gestureSourceType": "touch"})
        page.wait_for_timeout(500)
    scale_after = page.evaluate("() => visualViewport.scale")
    zoom_after = page.evaluate(f"() => {MAP}.getZoom()") if has_map else None
    rec = {"case": case, "viewport": vp, "surface": surface, "point": point,
           "hit": probe["hit"], "pinchAllowed": probe["pinchAllowed"],
           "chain": probe["chain"], "scaleBefore": scale_before,
           "scaleAfter": scale_after, "zoomBefore": zoom_before,
           "zoomAfter": zoom_after, "synthesized": cdp is not None}
    records.append(rec)
    where = f"{vp} {case} {surface}"
    check(scale_after == 1, f"{where}: page scale went to {scale_after}")
    check(probe["pinchAllowed"] is False,
          f"{where}: the browser may still pinch-zoom here — {probe['chain']}")
    if cdp is None or zoom_before is None or map_expect is None:
        return
    if map_expect == "zoom":
        check(zoom_after > zoom_before,
              f"{where}: the map did not zoom ({zoom_before} -> {zoom_after})")
    else:
        check(zoom_after == zoom_before,
              f"{where}: a gesture on a surface that covers the map moved it "
              f"({zoom_before} -> {zoom_after})")


def guards(page, vp: str, case: str, surfaces: list[str]) -> None:
    for sel in [None] + surfaces:
        got = page.evaluate(GESTURE, sel)
        if got is None:
            continue
        name = sel or "document"
        records.append({"case": case, "viewport": vp, "surface": name,
                        "gestureGuarded": got})
        check(got is True, f"{vp} {case} {name}: gesturestart was not prevented")


def viewport_meta(page, vp: str, case: str) -> None:
    content = page.evaluate(VIEWPORT_META)
    records.append({"case": case, "viewport": vp, "surface": "meta[viewport]",
                    "content": content})
    check(bool(content) and "user-scalable=no" in content,
          f"{vp} {case}: viewport meta does not pin the scale — {content!r}")
    check(bool(content) and "maximum-scale=1.0" in content,
          f"{vp} {case}: viewport meta has no maximum-scale — {content!r}")


def new_context(browser, w: int, h: int, *, mobile: bool = True, seed=SEED,
                init: str | None = None):
    ctx = browser.new_context(viewport={"width": w, "height": h},
                              device_scale_factor=2.625 if mobile else 1,
                              is_mobile=mobile, has_touch=mobile,
                              service_workers="block")
    page = ctx.new_page()
    page.set_default_timeout(45000)
    errors: list[str] = []
    lib_browser.install_guards(page, errors)
    if seed:
        lib_browser.seed_local_storage(page, seed)
    if init:
        page.add_init_script(init)
    return ctx, page


def throttle(cdp, rate: float) -> None:
    if cdp is not None:
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": rate})


def run_phone_like(browser, p, w: int, h: int, rate: float) -> None:
    vp = f"{w}x{h}"
    print(f"  {vp} (CPU {rate}x around the gestures)")

    # (a0) first frame, and (a) the whole UI up with no map yet.
    ctx, page = new_context(browser, w, h, init=FIRST_FRAME + HOLD_MAP)
    cdp = ctx.new_cdp_session(page) if args.browser == "chromium" else None
    throttle(cdp, rate)
    page.goto(lib_browser_base + "/index.html", wait_until="domcontentloaded")
    page.wait_for_selector("#ss-box", state="attached")
    page.wait_for_timeout(600)
    first = page.evaluate("() => window.__pinchFirstFrame || null")
    records.append({"case": "a0-first-frame", "viewport": vp,
                    "surface": "document", **(first or {"missing": True})})
    if first:
        check(first["htmlTouchAction"] == "pan-x pan-y",
              f"{vp} a0: html touch-action at the first frame is "
              f"{first['htmlTouchAction']!r}")
        check(first["gestureGuarded"] is True,
              f"{vp} a0: the document gesture guard was not up at the first frame")
    check(page.evaluate("() => !!document.querySelector('.leaflet-container')") is False,
          f"{vp} a: the map container exists — the probe failed to hold L.map")
    viewport_meta(page, vp, "a-pre-init")
    guards(page, vp, "a-pre-init", ["#ss-box", "#intro-bar"])
    for sel in ("#ss-box", "#intro-bar", ".folium-map"):
        pinch(page, cdp, "a-pre-init", vp, sel, centre(page, sel))
    throttle(cdp, 1)
    ctx.close()

    # (b) the first-visit language gate. Phone/split only: it stays down in
    # column mode, and the record says so rather than pretending.
    ctx, page = new_context(browser, w, h, seed={})
    cdp = ctx.new_cdp_session(page) if args.browser == "chromium" else None
    lib_browser.boot(page, lib_browser_base)
    page.wait_for_timeout(500)
    if page.evaluate("() => { const g = document.getElementById('lang-gate');"
                     "        return !!g && !g.hidden; }"):
        throttle(cdp, rate)
        guards(page, vp, "b-lang-gate", ["#lang-gate .lg-card"])
        pinch(page, cdp, "b-lang-gate", vp, "#lang-gate .lg-card",
              centre(page, "#lang-gate .lg-card"), map_expect="hold")
        throttle(cdp, 1)
    else:
        records.append({"case": "b-lang-gate", "viewport": vp,
                        "surface": "#lang-gate", "skipped": "column mode"})
    ctx.close()

    # (c) through (f) on one normally booted page.
    ctx, page = new_context(browser, w, h)
    cdp = ctx.new_cdp_session(page) if args.browser == "chromium" else None
    lib_browser.boot(page, lib_browser_base)
    page.wait_for_timeout(1200)
    viewport_meta(page, vp, "c-home")
    guards(page, vp, "c-home", ["#ss-box", ".leaflet-container"])
    throttle(cdp, rate)
    for sel in ("#intro-bar", "#ss-chips", "#ss-box", "#wb-seg", ".map-fab-stack"):
        pinch(page, cdp, "c-home", vp, sel, centre(page, sel))

    # (d) the map itself, at the whole-of-Japan zoom the report names and at
    # a city zoom. `.leaflet-container` centre is the map only when nothing
    # is over it, which is the home state.
    map_pt = centre(page, ".leaflet-container")
    page.evaluate(f"() => {MAP}.setZoom({MAP}.getMinZoom())")
    page.wait_for_timeout(800)
    pinch(page, cdp, "d-map-minzoom", vp, ".leaflet-container", map_pt,
          map_expect="zoom")
    page.evaluate(f"() => {MAP}.setView([35.6812, 139.7671], 13)")
    page.wait_for_timeout(800)
    pinch(page, cdp, "d-map-z13", vp, ".leaflet-container", map_pt,
          map_expect="zoom")
    throttle(cdp, 1)

    # (e) the result list — the drawer below 750px, the resident left column
    # above it, reached the way a finger would reach it either way.
    if page.locator("#wb-seg").is_visible():
        lib_browser.phone_tab(page, "results")
    page.wait_for_selector("#wb-list .wb-row")
    page.wait_for_timeout(400)
    throttle(cdp, rate)
    guards(page, vp, "e-list", ["#wb-list"])
    pinch(page, cdp, "e-list", vp, "#wb-list", centre(page, "#wb-list"),
          map_expect="hold")
    throttle(cdp, 1)

    # (f) the detail card. Below 750px that is the bottom sheet and above it
    # the column's detail pane — `#bs-sheet.bs-open` is attached in both and
    # visible in only one, while `#bs-content` is the card body either way.
    page.locator("#wb-list .wb-row").first.click()
    page.wait_for_selector("#bs-sheet.bs-open", state="attached")
    page.wait_for_selector("#bs-content .rst-title")
    page.wait_for_timeout(600)
    throttle(cdp, rate)
    guards(page, vp, "f-detail", ["#bs-content"])
    pinch(page, cdp, "f-detail", vp, "#bs-content", centre(page, "#bs-content"),
          map_expect="hold")
    throttle(cdp, 1)
    ctx.close()


def run_desktop(browser) -> None:
    """(g) 1440x900: ctrl+wheel is the desktop pinch. It must zoom the map and
    nothing else — the guard for it stays on the container (a non-passive
    wheel listener on the document would tax every scroll on the page)."""
    vp = "1440x900"
    print(f"  {vp} ctrl+wheel")
    ctx, page = new_context(browser, 1440, 900, mobile=False)
    lib_browser.boot(page, lib_browser_base)
    page.wait_for_timeout(1200)
    viewport_meta(page, vp, "g-desktop")

    def ctrl_wheel(selector: str):
        pt = centre(page, selector)
        if pt is None:
            records.append({"case": "g-desktop", "viewport": vp,
                            "surface": selector, "skipped": "not on screen"})
            return
        before = page.evaluate(f"() => {MAP}.getZoom()")
        page.mouse.move(pt["x"], pt["y"])
        page.keyboard.down("Control")
        page.mouse.wheel(0, -240)
        page.keyboard.up("Control")
        page.wait_for_timeout(700)
        after = page.evaluate(f"() => {MAP}.getZoom()")
        scale = page.evaluate("() => visualViewport.scale")
        records.append({"case": "g-desktop", "viewport": vp, "surface": selector,
                        "zoomBefore": before, "zoomAfter": after,
                        "scaleAfter": scale})
        return before, after

    got = ctrl_wheel(".leaflet-container")
    if got:
        check(got[1] > got[0],
              f"{vp}: ctrl+wheel over the map did not zoom it ({got[0]} -> {got[1]})")
    got = ctrl_wheel("#wb-left")
    if got:
        check(got[1] == got[0],
              f"{vp}: ctrl+wheel over the left column moved the map "
              f"({got[0]} -> {got[1]})")
    ctx.close()


def run_webkit(browser) -> None:
    """WebKit fires the real `gesture*` events but Playwright cannot
    synthesize a pinch in it, so this is the guard check the iOS layer
    actually depends on, at the first frame and once the map is up."""
    for w, h in ((475, 751), (932, 704)):
        vp = f"{w}x{h}"
        print(f"  {vp} webkit gesture guards")
        ctx, page = new_context(browser, w, h, init=FIRST_FRAME)
        lib_browser.boot(page, lib_browser_base)
        page.wait_for_timeout(800)
        first = page.evaluate("() => window.__pinchFirstFrame || null")
        records.append({"case": "a0-first-frame", "viewport": vp,
                        "surface": "document", **(first or {"missing": True})})
        if first:
            check(first["gestureGuarded"] is True,
                  f"{vp} a0: the document gesture guard was not up at the "
                  f"first frame (webkit)")
            check(first["htmlTouchAction"] == "pan-x pan-y",
                  f"{vp} a0: html touch-action at the first frame is "
                  f"{first['htmlTouchAction']!r} (webkit)")
        viewport_meta(page, vp, "webkit-home")
        guards(page, vp, "webkit-home",
               ["#ss-box", "#wb-seg", ".map-fab-stack", ".leaflet-container"])
        ctx.close()


def main() -> int:
    global lib_browser_base
    with lib_browser.serve_docs(8978) as base, sync_playwright() as p:
        lib_browser_base = base
        browser = getattr(p, args.browser).launch()
        print(f"pinch: {args.browser}, label={args.label}, docs={lib_browser.DOCS}")
        if args.browser == "webkit":
            run_webkit(browser)
        else:
            run_phone_like(browser, p, 932, 704, 4)
            run_phone_like(browser, p, 475, 751, 6)
            run_desktop(browser)
        browser.close()

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(records, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print(f"  wrote {len(records)} records to {args.json}")

    allowed = [r for r in records if r.get("pinchAllowed") is True]
    scaled = [r for r in records if isinstance(r.get("scaleAfter"), (int, float))
              and r["scaleAfter"] != 1]
    unguarded = [r for r in records if r.get("gestureGuarded") is False]
    print(f"  surfaces the browser may still pinch-zoom: {len(allowed)}")
    print(f"  gesturestart not prevented:                {len(unguarded)}")
    print(f"  page scale left at 1:                      {not scaled}")
    if failures:
        print(f"\nFAIL ({len(failures)}):")
        for f in failures:
            print("  -", f)
        return 1
    print("pinch: OK" if GATE else "pinch: recorded (no gate on --label before)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
