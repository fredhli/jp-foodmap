"""Fold inner identity, density, map-width and virtual-list regression."""
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
parser.add_argument("--baseline", action="store_true")
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
lib_browser.DOCS = args.docs.resolve()
records, failures = [], []

CASES = [
    ("inner932", 932, 704, 932, 704, 2.625, True, True),
    ("inner704", 704, 932, 704, 932, 2.625, True, True),
    ("inner816", 816, 616, 816, 616, 3, True, True),
    ("inner616", 616, 816, 616, 816, 3, True, True),
    ("split591", 591, 689, 932, 704, 2.625, True, True),
    ("split688", 688, 704, 932, 704, 2.625, True, True),
    ("split-avd", 591, 688, 592, 689, 2.625, True, True),
    ("cover", 475, 751, 475, 751, 2.625, True, False),
    ("iphone", 402, 874, 402, 874, 3, True, False),
    ("tablet", 768, 1024, 768, 1024, 2, True, False),
    ("same-size-mouse", 932, 704, 932, 704, 2.625, False, False),
    ("desktop", 1440, 900, 1440, 900, 1, False, False),
]


def check(ok, message):
    if ok:
        return
    failures.append(message)
    (args.output / "results.json").write_text(
        json.dumps({"records": records, "failures": failures}, ensure_ascii=False, indent=2)
    )
    if not args.baseline:
        raise AssertionError(message)


PROFILE_PROBE = """() => ({
  inner:!!App.state.layout.foldInner, cover:!!App.state.layout.foldCover,
  attr:document.documentElement.hasAttribute('data-fold-inner'),
  mode:App.state.layout.mode, z:App.state.layout.z,
  identity:App.layout.foldInnerInfo(App.state.layout.W),
  overflow:document.documentElement.scrollWidth>innerWidth+1
})"""

FILTER_PROBE = """() => {
  const box=e=>{if(!e)return null;const r=e.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height,b:r.bottom}};
  const sc=Containers.scroller('filters'), sr=sc.getBoundingClientRect();
  const root=document.getElementById('filters-root'), form=root.querySelector('.ft-form');
  const full=e=>{const r=e.getBoundingClientRect();return r.top>=sr.top-1&&r.bottom<=sr.bottom+1};
  const budgets=[...root.querySelectorAll('.ft-budget')], options=[...root.querySelectorAll('.ft-budget,.ft-cui,.ft-award,.ft-sw-row')];
  const chips=[...root.querySelectorAll('.ft-chip')], xs=[...root.querySelectorAll('.ft-chip .chip-x')];
  const hr=root.querySelector('.ft-chips').getBoundingClientRect();
  const help=root.querySelector('[data-help="rating"]')?.getBoundingClientRect(), slider=root.querySelector('#ft-rating')?.getBoundingClientRect();
  const fab=document.getElementById('fab-root'), fabBox=box(fab);
  const hitOk=xs.filter(e=>{const r=e.getBoundingClientRect();return r.x+r.width/2>=hr.left&&r.x+r.width/2<=hr.right})
    .every(e=>{const r=e.getBoundingClientRect(), hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return hit===e||e.contains(hit)});
  return {
    formHeight:form.scrollHeight, bodyHeight:sc.clientHeight, body:box(sc), summary:box(root.querySelector('.ft-sec-top')),
    summaryFull:full(root.querySelector('.ft-sec-top')), regionFull:full(root.querySelector('[data-section="region"]')),
    ratingFull:full(root.querySelector('[data-section="rating"]')), budgetTitleFull:full(root.querySelector('[data-section="budget"] .ft-head')),
    budgetFull:budgets.filter(full).length, chipHost:box(root.querySelector('.ft-chips')),
    chipRows:new Set(chips.map(e=>Math.round(e.getBoundingClientRect().top))).size,
    chipTargets:xs.map(box), hitOk,
    chipVisualGaps:chips.map(e=>{const l=e.querySelector('.ft-chip-t').getBoundingClientRect(),i=e.querySelector('.chip-x .ic').getBoundingClientRect();return i.left-l.right}),
    chipOverflow:root.querySelector('.ft-chips').scrollWidth>root.querySelector('.ft-chips').clientWidth+1,
    chipCue:root.querySelector('.ft-chips').hasAttribute('data-scroll'),
    helpSliderClear:!help||!slider||help.bottom<=slider.top||slider.bottom<=help.top,
    optionMin:Math.min(...options.map(e=>e.getBoundingClientRect().height)),
    groups:[...root.querySelectorAll('.ft-grp')].map(e=>e.dataset.open),
    map:{...App.state.layout.mapRect}, columns:App.layout.columns(App.state),
    pageOverflow:document.documentElement.scrollWidth>innerWidth+1,
    panelOverflow:sc.scrollWidth>sc.clientWidth+1,
    footer:box(document.querySelector('#filters-foot .ft-foot')),
    fab:{box:fabBox,horizontal:fab&&fab.classList.contains('is-horizontal')}
  };
}"""

DETAIL_PROBE = """() => {
  const box=e=>{if(!e)return null;const r=e.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height,b:r.bottom}};
  const sc=Containers.scroller('detail'), sr=sc.getBoundingClientRect();
  const q=s=>document.querySelector(s), gallery=q('.dt-gallery'), summary=q('.dt-summary'), place=q('[data-section="address"] h2');
  const actions=[...document.querySelectorAll('#detail-foot .dt-act')].map(e=>({label:e.getAttribute('aria-label')||e.textContent.trim(),box:box(e)}));
  const cand=q('#candidates-root .dt-cand');
  return {map:{...App.state.layout.mapRect},gallery:box(gallery),summary:box(summary),place:box(place),body:box(sc),
    summaryFull:summary&&summary.getBoundingClientRect().bottom<=sr.bottom+1,
    placeVisible:place&&place.getBoundingClientRect().top<sr.bottom,
    actions,candidate:cand?{collapsed:cand.classList.contains('dt-cand--collapsed'),box:box(cand)}:null,
    pageOverflow:document.documentElement.scrollWidth>innerWidth+1,panelOverflow:sc.scrollWidth>sc.clientWidth+1};
}"""


def open_filters(page):
    page.evaluate("""() => {
      App.act.applyFilters({region:13,regions:new Set([13]),areas:new Set(),ratingMin:4.25,hideForeign:true});
      App.act.setTab('filters');
      if(App.state.layout.mode==='narrow')App.act.setSheet('filter');
    }""")
    page.wait_for_function("() => App.state.sheet.tab==='filters' && Filters.countsReady()")
    page.wait_for_timeout(450)


def sweep_virtual_rows(page, name):
    page.evaluate("""() => {App.act.setTab('results');if(App.state.layout.mode==='narrow')App.act.setSheet('browse');}""")
    page.wait_for_timeout(450)
    page.evaluate("() => Containers.scroller('list').scrollTop=0")
    seen = {}
    last = None
    for _ in range(80):
        sample = page.evaluate("""() => {
          const sc=Containers.scroller('list'), m=ListMod.metrics();
          return {rows:[...document.querySelectorAll('.ls-row[data-id]')].map(e=>[e.dataset.id,e.offsetHeight]),
            metrics:m,scroll:sc.scrollTop,max:sc.scrollHeight-sc.clientHeight};
        }""")
        for rid, height in sample["rows"]:
            seen[rid] = height
        last = sample
        if sample["scroll"] >= sample["max"] - 2 and sample["metrics"]["window"][1] == sample["metrics"]["items"]:
            break
        page.evaluate("() => {const sc=Containers.scroller('list');sc.scrollTop+=Math.max(100,sc.clientHeight*.72)}")
        page.wait_for_timeout(45)
    check(last is not None and last["metrics"]["window"][1] == last["metrics"]["items"], name + ": virtual list did not reach the last item")
    check(len(seen) == last["metrics"]["items"], name + ": virtual sweep missed rows")
    check(abs(sum(seen.values()) - last["metrics"]["total"]) <= 2, name + ": virtual total differs from measured rows")
    page.evaluate("() => Containers.scroller('list').scrollTop=1e9")
    page.wait_for_timeout(120)
    stable = page.evaluate("""() => {const sc=Containers.scroller('list'),m=ListMod.metrics();return {total:m.total,scroll:sc.scrollTop,max:sc.scrollHeight-sc.clientHeight}}""")
    check(abs(stable["total"] - last["metrics"]["total"]) <= 2 and abs(stable["scroll"] - stable["max"]) <= 2,
          name + ": virtual bottom changed after calibration")
    return {"seen": len(seen), "total": stable["total"]}


with lib_browser.serve_docs(8994) as base, sync_playwright() as p:
    browser = p.chromium.launch()
    for name, w, h, sw, sh, dpr, touch, expected in CASES:
        context = browser.new_context(
            viewport={"width": w, "height": h}, screen={"width": sw, "height": sh},
            device_scale_factor=dpr, has_touch=touch, is_mobile=touch, service_workers="block"
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("https://**/*", lambda route: route.abort())
        lib_browser.seed_local_storage(page, {"tabelog.lang": "zh-CN", "tabelog.seenIntro": "1"})
        lib_browser.boot(page, base)
        profile = page.evaluate(PROFILE_PROBE)
        check(profile["inner"] == expected and profile["attr"] == expected, name + ": profile/attribute")
        check(profile["cover"] is (name == "cover"), name + ": profile exclusivity")
        check(not profile["overflow"], name + ": page overflow")

        if name == "inner932":
            invalid = page.evaluate("""() => {
              const old=Object.getOwnPropertyDescriptor(window,'devicePixelRatio'),out=[];
              for(const v of [0,-1,NaN,Infinity,undefined,'2.625']){Object.defineProperty(window,'devicePixelRatio',{value:v,configurable:true});out.push(App.layout.foldInnerInfo(932));}
              if(old)Object.defineProperty(window,'devicePixelRatio',old);else delete window.devicePixelRatio;
              return out;
            }""")
            check(all(not x["matched"] and x["reason"] == "屏幕或缩放读数无效" for x in invalid), "invalid inner identity readings")

        record = {"name": name, "profile": profile}
        if expected:
            open_filters(page)
            density = page.evaluate(FILTER_PROBE)
            z = lib_browser.ui_z(page)
            check(not density["pageOverflow"] and not density["panelOverflow"], name + ": filter overflow")
            check(density["chipRows"] <= 1 and density["hitOk"], name + ": chip strip/hit testing")
            check(not density["chipOverflow"] or density["chipCue"], name + ": overflowing chips have no edge cue")
            check(density["helpSliderClear"], name + ": rating help overlaps slider")
            check(all(x["w"] >= 44 * z - .25 and x["h"] >= 44 * z - .25 for x in density["chipTargets"]), name + ": chip targets")
            check(max(density["chipVisualGaps"]) <= 13 * z, name + ": chip label-to-close spacing")
            check(density["optionMin"] >= 40 * z - .25, name + ": secondary option target")
            check(all(x == "false" for x in density["groups"]), name + ": initial cuisine groups")
            page.evaluate("""() => {const h=document.querySelector('.ft-chips');h.scrollLeft=h.scrollWidth;h.dispatchEvent(new Event('scroll'))}""")
            page.wait_for_timeout(60)
            chip_end = page.evaluate("""() => {const h=document.querySelector('.ft-chips').getBoundingClientRect(),x=document.querySelector('.ft-chip:last-of-type .chip-x').getBoundingClientRect();return {inside:x.left>=h.left-1&&x.right<=h.right+1,end:document.querySelector('.ft-chips').hasAttribute('data-end')}}""")
            check(chip_end["inside"] and chip_end["end"], name + ": last chip action cannot be revealed")
            page.evaluate("""() => {const h=document.querySelector('.ft-chips');h.scrollLeft=0;h.dispatchEvent(new Event('scroll'))}""")
            page.wait_for_timeout(60)
            limit = 1900 if name == "inner932" else 2050 if name == "inner816" else 1750
            check(density["formHeight"] <= limit, name + f': filter height {density["formHeight"]}>{limit}')
            if name == "inner932":
                check(density["summaryFull"] and density["regionFull"] and density["ratingFull"] and density["budgetFull"] >= 2,
                      name + ": first screen does not reach two budget options")
                check(density["map"]["w"] >= 500, name + ": browse map width")
            elif name == "inner816":
                check(density["summaryFull"] and density["regionFull"] and density["ratingFull"] and density["budgetFull"] >= 1,
                      name + ": first screen does not reach a budget option")
                check(density["map"]["w"] >= 450, name + ": browse map width")
            elif name in ("inner616", "split591"):
                check(density["ratingFull"] and density["budgetTitleFull"], name + ": narrow first screen density")
            if name == "split-avd":
                fbox, mbox = density["fab"]["box"], density["map"]
                check(density["fab"]["horizontal"] and fbox["y"] >= mbox["y"] - 1 and fbox["b"] <= mbox["y"] + mbox["h"] + 1,
                      name + ": FAB does not fit the visible map band")

            page.screenshot(path=str(args.output / f"{name}-filter.png"))

            first = page.locator('[data-grp-toggle]').first
            first.click()
            page.wait_for_timeout(80)
            check(page.evaluate("App.state.filtersUi.openGroupsTouched && document.querySelector('.ft-grp').dataset.open==='true'"), name + ": cuisine manual state")

            if name in ("inner932", "inner816"):
                virtual = sweep_virtual_rows(page, name)
                row_min = page.locator('.ls-row-c .ls-open').first.evaluate("e=>parseFloat(getComputedStyle(e).minHeight)")
                check(abs(row_min - 72 * z) < .1, name + ": result row floor")
                page.evaluate("""() => {const r=Data.restaurants.find(x=>x.name.includes('らいもん'))||Data.restaurants[0];App.act.openDetail(r.id,'results');}""")
                page.wait_for_function("() => !!Detail.detailFor(App.state.selected.id)")
                page.wait_for_timeout(500)
                detail = page.evaluate(DETAIL_PROBE)
                check(not detail["pageOverflow"] and not detail["panelOverflow"], name + ": detail overflow")
                check(detail["map"]["w"] >= (475 if name == "inner932" else 400), name + ": detail map width")
                check(abs(detail["gallery"]["h"] - (176 if h > 640 else 164) * z) < 1, name + ": gallery height")
                check(detail["summaryFull"] and detail["placeVisible"], name + ": detail first screen density")
                check(len(detail["actions"]) == 3 and all(a["label"] and a["box"]["h"] >= 44 * z - .25 for a in detail["actions"]), name + ": detail actions")
                if name == "inner816":
                    check(detail["candidate"] and detail["candidate"]["collapsed"] and detail["candidate"]["box"]["h"] >= 44 * z - .25,
                          name + ": short-height candidate default")
                    page.locator('[data-act="cand-open"]').click()
                    page.wait_for_timeout(120)
                    check(page.evaluate("App.state.detail.candidatesTouched && App.state.detail.candidatesOpen && !!document.querySelector('.dt-cand:not(.dt-cand--collapsed)')"),
                          name + ": candidate manual state")
                if name == "inner932":
                    clear = page.evaluate("""() => {const c=document.querySelector('#candidates-root .dt-cand').getBoundingClientRect(),q=s=>document.querySelector(s)?.getBoundingClientRect(),hit=r=>r&&c.left<r.right&&c.right>r.left&&c.top<r.bottom&&c.bottom>r.top;return !hit(q('.leaflet-control-scale'))&&!hit(q('.leaflet-control-attribution'))}""")
                    check(clear, name + ": candidate overlaps map scale/attribution")
                record.update(density=density, virtual=virtual, detail=detail)
            else:
                record["density"] = density
            page.screenshot(path=str(args.output / f"{name}.png"))
        elif name in ("iphone", "same-size-mouse", "tablet", "desktop"):
            page.evaluate("""() => {App.act.setTab('results');if(App.state.layout.mode==='narrow')App.act.setSheet('expanded');}""")
            page.wait_for_timeout(250)
            expected_style = page.evaluate("""() => {const f=getComputedStyle(document.querySelector('.ft-form'));return {top:parseFloat(f.paddingTop),gap:parseFloat(getComputedStyle(document.querySelectorAll('.ft-sec')[1]).marginTop),row:parseFloat(getComputedStyle(document.querySelector('.ls-row .ls-open')).minHeight)}}""")
            z = lib_browser.ui_z(page)
            if name == "iphone":
                check(abs(expected_style["top"] - 12 * z) < .1 and abs(expected_style["gap"] - 24 * z) < .1 and abs(expected_style["row"] - 96 * z) < .1,
                      name + ": computed-style regression")
            elif name in ("same-size-mouse", "tablet", "desktop"):
                check(abs(expected_style["top"] - 12 * z) < .1 and abs(expected_style["gap"] - 24 * z) < .1 and abs(expected_style["row"] - 76 * z) < .1,
                      name + ": computed-style regression")
        check(not errors, name + ": page errors " + str(errors))
        records.append(record)
        context.close()
    browser.close()

(args.output / "results.json").write_text(json.dumps({"records": records, "failures": failures}, ensure_ascii=False, indent=2))
print(f"{len(CASES)} profiles; {len(failures)} failures")
