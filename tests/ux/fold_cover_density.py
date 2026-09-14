"""Fold cover task-specific sheet stops and first-screen density regression."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import lib_browser
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--docs", type=Path, default=ROOT / "docs")
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
lib_browser.DOCS = args.docs.resolve()
records = []


def check(ok, message):
    if not ok:
        raise AssertionError(message)


with lib_browser.serve_docs(8995) as base, sync_playwright() as p:
    browser = p.chromium.launch()
    for name, w, h, dpr in (("cover475", 475, 751, 2.625), ("cover416", 416, 657, 3)):
        context = browser.new_context(
            viewport={"width": w, "height": h}, screen={"width": w, "height": h},
            device_scale_factor=dpr, has_touch=True, is_mobile=True, service_workers="block"
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("https://**/*", lambda route: route.abort())
        lib_browser.seed_local_storage(page, {"tabelog.lang": "zh-CN", "tabelog.seenIntro": "1"})
        lib_browser.boot(page, base)
        inset = 24 if name == "cover475" else 40
        page.evaluate("""inset => {
          document.documentElement.style.setProperty('--app-inset-top', inset + 'px');
          App.layout.measure(); App.flushNow();
        }""", inset)
        page.wait_for_timeout(120)
        stops = page.evaluate("""() => {const s=App.util.cloneState(App.state);return Object.fromEntries(['browse','detail','filter'].map(k=>[k,App.layout.sheetTarget(k,s).target/s.layout.H]))}""")
        check(abs(stops["browse"] - .62) < .002 and abs(stops["detail"] - .66) < .002 and abs(stops["filter"] - .68) < .002,
              name + ": task sheet ratios " + str(stops))
        page.evaluate("""() => {App.act.applyFilters({region:13,regions:new Set([13]),areas:new Set(),ratingMin:4.25,hideForeign:true});App.act.setTab('filters');App.act.setSheet('filter')}""")
        page.wait_for_function("() => App.state.sheet.tab==='filters' && Filters.countsReady()")
        page.wait_for_timeout(450)
        density = page.evaluate("""() => {
          const sc=Containers.scroller('filters'),sr=sc.getBoundingClientRect(),box=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height,b:r.bottom}};
          const full=e=>e.getBoundingClientRect().bottom<=sr.bottom+1;
          const chips=[...document.querySelectorAll('.ft-chip')],xs=[...document.querySelectorAll('.ft-chip .chip-x')];
          return {sheet:box(document.querySelector('#sheet')),body:box(sc),summary:box(document.querySelector('.ft-sec-top')),
            regionFull:full(document.querySelector('[data-section="region"]')),ratingFull:full(document.querySelector('[data-section="rating"]')),
            quickFull:full(document.querySelector('.ft-quick')),chipRows:new Set(chips.map(e=>Math.round(e.getBoundingClientRect().top))).size,
            targets:xs.map(box),footer:box(document.querySelector('#filters-foot .ft-see')),map:{...App.state.layout.mapRect},
            overflow:document.documentElement.scrollWidth>innerWidth+1||sc.scrollWidth>sc.clientWidth+1};
        }""")
        check(not density["overflow"] and density["chipRows"] == 1, name + ": filter overflow/chip rows")
        check(density["regionFull"] and density["ratingFull"] and density["quickFull"], name + ": filter first screen " + str(density))
        check(all(t["w"] >= 43.75 and t["h"] >= 43.75 for t in density["targets"]), name + ": chip remove targets")
        check(density["footer"]["h"] >= 44 and density["map"]["h"] >= 140, name + ": CTA/map band")
        page.screenshot(path=str(args.output / f"{name}-filter.png"))

        summary_font = page.locator(".ft-summary").evaluate("e=>parseFloat(getComputedStyle(e).fontSize)")
        page.evaluate("App.i18n.setFontScale(130)")
        page.wait_for_timeout(180)
        scaled_font = page.locator(".ft-summary").evaluate("e=>parseFloat(getComputedStyle(e).fontSize)")
        check(scaled_font >= summary_font * 1.29, name + ": summary ignores text scale")
        check(page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"), name + ": 130% overflow")
        page.evaluate("App.i18n.setFontScale(100)")
        page.wait_for_timeout(180)

        page.evaluate("""() => {const r=Data.restaurants.find(x=>x.name.includes('らいもん'))||Data.restaurants[0];App.act.openDetail(r.id,'results')}""")
        page.wait_for_function("() => !!Detail.detailFor(App.state.selected.id)")
        page.wait_for_timeout(500)
        detail = page.evaluate("""() => {
          const box=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height,b:r.bottom}};
          const sc=Containers.scroller('detail'),sr=sc.getBoundingClientRect(),gallery=document.querySelector('.dt-gallery'),summary=document.querySelector('.dt-summary');
          return {gallery:box(gallery),summary:box(summary),summaryFull:summary.getBoundingClientRect().bottom<=sr.bottom+1,
            actions:[...document.querySelectorAll('#detail-foot .dt-act')].map(box),map:{...App.state.layout.mapRect},
            overflow:document.documentElement.scrollWidth>innerWidth+1||sc.scrollWidth>sc.clientWidth+1};
        }""")
        check(not detail["overflow"] and 103.5 <= detail["gallery"]["h"] <= 112.5, name + ": detail gallery/overflow")
        check(detail["summaryFull"] and all(a["h"] >= 44 for a in detail["actions"]), name + ": summary/actions")
        check(detail["map"]["h"] >= 150, name + ": detail map band")
        check(not errors, name + ": page errors " + str(errors))
        records.append({"name": name, "insetTop": inset, "stops": stops, "filters": density, "detail": detail})
        page.screenshot(path=str(args.output / f"{name}-detail.png"))
        context.close()
    browser.close()

(args.output / "results.json").write_text(json.dumps(records, ensure_ascii=False, indent=2))
print("2 profiles; 0 failures")
