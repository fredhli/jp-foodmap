"""4.2.0 nearby transactions, native callback identity, scope and tab-local preferences.

Run against a completed build with --browser chromium or --browser webkit.
Native geolocation is controlled in page closures; all external requests are blocked.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))
import lib_browser
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('--browser', choices=['chromium', 'webkit'], default='chromium')
parser.add_argument('--output', type=Path, default=ROOT / 'audit_outputs/4.2.0/nearby')
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
records = []
GEO = """(() => {
  window.geoRequests = [];
  Object.defineProperty(navigator, 'geolocation', {configurable:true, value:{
    getCurrentPosition(ok, fail, options) { geoRequests.push({ok, fail, options}); }
  }});
  window.giveFix = (index, lat=35.6812, lon=139.7671, ts=Date.now()) =>
    geoRequests[index].ok({coords:{latitude:lat, longitude:lon, accuracy:12}, timestamp:ts});
  window.rejectFix = (index, code) => geoRequests[index].fail({code});
})();"""
SNAP = """() => {
 const s=App.state,m=MapMod.map;
 return JSON.parse(JSON.stringify({filters:s.filters,sort:s.sort,onlyList:s.saved.onlyList,
   search:{query:s.search.query,placeFilter:s.search.placeFilter,dropLoc:s.search.dropLoc},
   center:[m.getCenter().lat,m.getCenter().lng],zoom:m.getZoom()},(k,v)=>v instanceof Set?[...v]:v));
}"""
PREFS = """() => Object.fromEntries(['tabelog.filterState','tabelog.listView','tabelog.mapView'].map(k=>[k,localStorage.getItem(k)]))"""


def check(page, name, expression):
    result = page.evaluate(expression)
    assert result is True, f'{name}: {result}'
    records.append({'case': name, 'status': 'PASS'})
    print('PASS', name, flush=True)


def settle(page):
    page.wait_for_timeout(650)


def load(context, base):
    page = context.new_page()
    page.route('https://**/*', lambda route: route.abort())
    page.add_init_script(GEO)
    page.add_init_script("localStorage.setItem('tabelog.lang','zh-CN');localStorage.setItem('tabelog.seenIntro','1')")
    errors = []
    lib_browser.install_guards(page, errors)
    lib_browser.boot(page, base)
    settle(page)
    return page, errors


def same_plan(page, before, after, label):
    # Leaflet drops its exact-center cache on invalidateSize, then reprojects rounded pixels.
    assert {k: v for k, v in before.items() if k != 'center'} == {k: v for k, v in after.items() if k != 'center'}, (label, before, after)
    delta = page.evaluate('([a,b,z])=>MapMod.map.project(a,z).distanceTo(MapMod.map.project(b,z))', [before['center'], after['center'], before['zoom']])
    assert delta <= 1, (label, 'map CSS pixel delta', delta, before['center'], after['center'])
    records.append({'case': label, 'status':'PASS','centerBefore':before['center'],'centerAfter':after['center'],'errorPx':delta,'zoom':before['zoom']})


def latest_fix(page, lat=35.6812, lon=139.7671):
    page.evaluate('([lat,lon])=>giveFix(geoRequests.length-1,lat,lon)', [lat, lon])
    settle(page)


with lib_browser.serve_docs(8997) as base, sync_playwright() as p:
    browser = getattr(p, args.browser).launch()
    context = browser.new_context(viewport={'width': 475, 'height': 751}, screen={'width': 475, 'height': 751},
                                  device_scale_factor=2.625, has_touch=True, is_mobile=True, service_workers='block',
                                  reduced_motion='reduce')
    page, errors = load(context, base)
    check(page, 'first load never requests permission', 'geoRequests.length===0')
    check(page, 'map keeps its single resize owner', 'MapMod.map.options.trackResize===false')
    check(page, 'Fold browse/detail/filter share the target including compact', """() => {
      const s=App.util.cloneState(App.state); s.layout.foldCover=true;
      return [false,true].every(c=>{s.layout.compact=c;return ['browse','detail','filter'].every(k=>App.layout.sheetTarget(k,s).target===Math.round(s.layout.H*.62));});
    }""")
    page.evaluate("""() => {
      const a=App.act,s=App.state; window.planRow=Data.restaurants.find(r=>r.pref===12).id;
      window.planList=a.createList('nearby test', '📁'); a.addToList(planList,planRow);a.setListFocus(planList);
      a.applyFilters({region:12,ratingMin:4.1,budgets:new Set(),cuisines:new Set(),awards:new Set(['gold','hyaku']),
        bookableOnly:true,favOnly:true,hideBlack:true,hideForeign:true,gcalOnly:true});
      App.set({sort:'awards',search:{query:'Tokyo ramen',placeFilter:'station-tokyo',dropLoc:true}});
      MapMod.restoreView([36.1,138.7],10);
    }""")
    settle(page)
    page.wait_for_function('MapMod.map.getZoom()===10')
    settle(page)
    before = page.evaluate(SNAP)
    page.locator('[data-ov="nearby"]:visible').first.click()
    check(page, 'top entry issues exactly one native request', 'geoRequests.length===1 && App.state.nearby.pending && !App.state.nearby.active')
    same_plan(page, before, page.evaluate(SNAP), 'request must not clear planning')
    latest_fix(page)
    check(page, 'successful entry clears every planning restriction', """() => {
      const s=App.state,f=s.filters,n=s.nearby;
      return n.active&&!n.pending&&n.radiusM===1000&&f.region===null&&f.ratingMin===3.4&&
        f.budgets.size===Data.config.PRICE_BUCKETS.length&&f.cuisines.size===Data.config.ALL_CUISINES.length&&f.awards.size===0&&
        ['bookableOnly','favOnly','hideBlack','hideForeign','gcalOnly'].every(k=>!f[k])&&s.saved.onlyList===null&&
        s.sort==='distance'&&s.search.query===''&&s.search.placeFilter===null&&!s.search.dropLoc;
    }""")
    check(page, 'snapshot clones Sets rather than sharing filter objects', """() => {
      const values=new Set(['gold']);App.act.applyFilters({awards:values});values.add('silver');
      return !App.state.nearby.planning.filters.awards.has('silver')&&App.state.nearby.planning.filters.awards.size===2;
    }""")
    page.evaluate("""() => {App.act.setNearbyRadius(2000);App.act.resetFilters();App.act.toggleFav(planRow);App.act.setBlack(planRow,true);}""")
    check(page, 'nearby reset retains radius and clears all conditions', "App.state.nearby.radiusM===2000&&!App.state.filters.hideBlack&&!App.state.filters.hideForeign&&App.state.filters.awards.size===0")
    page.locator('[data-ov="nearby"]:visible').first.click()
    settle(page)
    after = page.evaluate(SNAP)
    same_plan(page, before, after, 'complete planning restore')
    check(page, 'exit restores full planning and preserves live business edits', "!App.state.nearby.active&&App.state.nearby.planning===null&&!App.state.user.fav.has(planRow)&&App.state.user.black.has(planRow)")
    page.evaluate('App.act.toggleNearby()')
    latest_fix(page)
    page.evaluate('App.act.deleteList(planList);App.act.exitNearby()')
    settle(page)
    check(page, 'deleted planning folder restores as null', 'App.state.saved.onlyList===null')

    # Radius boundaries use temporary coordinates on real corpus identities.
    page.evaluate('App.act.toggleNearby()')
    latest_fix(page)
    check(page, 'all four radius boundaries agree with direct matcher and counts', """() => {
      const original=Data.restaurants; const saved=original.slice(0,8).map(r=>[r,App.util.cloneState(r)]);
      const distances=[199.999,200.001,499.999,500.001,999.999,1000.001,1999.999,2000.001];
      Data.restaurants=saved.map(([r],i)=>Object.assign(r,{lat:35.6812+distances[i]/6371000*180/Math.PI,lon:139.7671,
        rating:4,bucket:Data.config.PRICE_BUCKETS[0].key,categories:[Data.config.ALL_CUISINES[0]],foreign:0}));
      let ok=true;
      [200,500,1000,2000].forEach((radius,i)=>{
        App.act.setNearbyRadius(radius);const expected=i*2+1,ids=Data.M(App.state);
        const direct=Data.restaurants.filter(r=>Data.filterMatch(r,App.state.filters)).length;
        const count=Data.counts(App.state.filters).total;
        ok=ok&&ids.length===expected&&direct===expected&&count===expected&&Data.applyFilters(App.state.filters).length===expected;
      });
      saved.forEach(([r,v])=>Object.assign(r,v));Data.restaurants=original;
      App.act.setNearbyRadius(1000);return ok;
    }""")
    page.evaluate("""() => {window.originBefore=Data.locationOrigin();window.matchBefore=Data.M(App.state).slice();MapMod.restoreView([43.05,141.35],12)}""")
    settle(page)
    check(page, 'dragging or moving map never changes radius center or M', 'JSON.stringify(originBefore)===JSON.stringify(Data.locationOrigin())&&JSON.stringify(matchBefore)===JSON.stringify(Data.M(App.state))')
    check(page, 'MV is precisely M intersect visible map', "JSON.stringify(Data.MV(Data.M(App.state),MapMod.bounds()))===JSON.stringify(MapMod.visibleIds())")
    page.evaluate("""() => {window.farId=Data.restaurants.find(r=>r.lat>43).id;App.act.toggleFav(farId);App.act.setTab('saved')}""")
    check(page, 'Saved remains global outside nearby circle', 'ListMod.rowsFor(App.state).includes(farId)&&!Data.M(App.state).includes(farId)')
    page.evaluate('window.originalPlanning=JSON.stringify(App.state.nearby.planning,(k,v)=>v instanceof Set?[...v]:v);App.act.locate()')
    latest_fix(page,35.682,139.768)
    check(page, 'FAB in nearby updates the circle without replacing planning', 'App.state.nearby.active&&App.state.nearby.fix.lat===35.682&&App.state.nearby.radiusM===1000&&JSON.stringify(App.state.nearby.planning,(k,v)=>v instanceof Set?[...v]:v)===originalPlanning')
    page.evaluate('window.fixAtExit=JSON.stringify(App.state.nearby.fix);App.act.locate();window.requestAtExit=geoRequests.length-1;App.act.exitNearby();giveFix(requestAtExit,35,135)')
    check(page, 'exiting nearby cancels a pending recenter and its late callback', '!App.state.nearby.active&&!App.state.nearby.pending&&JSON.stringify(App.state.nearby.fix)===fixAtExit')
    page.evaluate('App.act.resetFilters();App.act.setTab("results")')
    settle(page)

    # A FAB fix recenters planning without entering nearby or changing the plan.
    planning = page.evaluate(SNAP)
    page.locator('[data-fab="locate"]').click()
    latest_fix(page)
    now = page.evaluate(SNAP)
    assert {k: v for k, v in planning.items() if k not in ['center', 'zoom']} == {k: v for k, v in now.items() if k not in ['center', 'zoom']}
    check(page, 'FAB does not enter nearby', '!App.state.nearby.active&&Data.locationOrigin()!==null')
    page.evaluate('window.goodFix=JSON.stringify(App.state.nearby.fix);window.goodLast=localStorage.getItem("tabelog.lastLocation");window.goodView=JSON.stringify(App.state.mapView)')
    for code in [1, 2, 3]:
        page.evaluate(f'App.act.locate();rejectFix(geoRequests.length-1,{code})')
        check(page, f'error {code} preserves previous validated fix and view', 'JSON.stringify(App.state.nearby.fix)===goodFix&&localStorage.getItem("tabelog.lastLocation")===goodLast&&JSON.stringify(App.state.mapView)===goodView&&!App.state.nearby.pending')
    page.evaluate('App.act.toggleNearby();giveFix(geoRequests.length-1,52.52,13.4)')
    check(page, 'Japan validation precedes all writes and movement', 'App.state.nearby.error==="地图只覆盖日本"&&!App.state.nearby.active&&JSON.stringify(App.state.nearby.fix)===goodFix&&localStorage.getItem("tabelog.lastLocation")===goodLast&&JSON.stringify(App.state.mapView)===goodView')
    page.evaluate('App.act.toggleNearby();window.cancelled=geoRequests.length-1;App.act.toggleNearby();giveFix(cancelled,35,135)')
    check(page, 'cancelled callback cannot write or activate', '!App.state.nearby.active&&!App.state.nearby.pending&&JSON.stringify(App.state.nearby.fix)===goodFix')
    page.evaluate('App.act.locate();window.oldRequest=geoRequests.length-1;App.act.locate();giveFix(oldRequest,35,135)')
    check(page, 'superseded request cannot resolve new pending request', 'App.state.nearby.pending&&JSON.stringify(App.state.nearby.fix)===goodFix')
    latest_fix(page, 35.7, 139.8)
    page.evaluate('window.newFix=JSON.stringify(App.state.nearby.fix);giveFix(oldRequest,35,135)')
    check(page, 'out of order late callback cannot replace latest fix', 'JSON.stringify(App.state.nearby.fix)===newFix')
    page.evaluate('App.act.locate();rejectFix(geoRequests.length-1,3);giveFix(geoRequests.length-1,35,135)')
    check(page, 'callback after timeout cannot resurrect request', '!App.state.nearby.pending&&JSON.stringify(App.state.nearby.fix)===newFix')
    for stamp in ['null', 'Date.now()+1000', 'Date.now()-600001']:
        page.evaluate(f'App.act.locate();giveFix(geoRequests.length-1,35.7,139.8,{stamp})')
        check(page, f'invalid native timestamp {stamp} is never fabricated', 'JSON.stringify(App.state.nearby.fix)===newFix&&!App.state.nearby.pending')

    # Persist the temporary task in this tab, and leave another tab's preferences alone.
    page.evaluate('App.act.toggleNearby()')
    latest_fix(page)
    page.evaluate('App.act.setNearbyRadius(500);App.act.applyFilters({ratingMin:3.8});App.set({sort:"price"});MapMod.restoreView([35.69,139.77],14)')
    settle(page)
    page.wait_for_function('MapMod.map.getZoom()===14')
    settle(page)
    temp = page.evaluate(SNAP)
    normal = page.evaluate(PREFS)
    page.evaluate('window.dispatchEvent(new Event("pagehide"))')
    assert page.evaluate(PREFS) == normal, 'active pagehide must not write normal preferences'
    page.reload(wait_until='domcontentloaded')
    lib_browser.wait_ready(page)
    settle(page)
    check(page, 'refresh resumes nearby session without permission request', 'geoRequests.length===0&&App.state.nearby.active&&!App.state.nearby.needsLocation&&App.state.nearby.radiusM===500&&App.state.filters.ratingMin===3.8&&App.state.sort==="price"')
    page.wait_for_function('MapMod.map.getZoom()===14')
    settle(page)
    restored = page.evaluate(SNAP)
    same_plan(page, temp, restored, 'temporary session restore')
    assert page.evaluate(PREFS) == normal, 'active reload must not write normal preferences'
    for repeat in range(2):
        page.reload(wait_until='domcontentloaded')
        lib_browser.wait_ready(page)
        page.wait_for_function('MapMod.map.getZoom()===14')
        settle(page)
        same_plan(page, temp, page.evaluate(SNAP), f'repeated refresh {repeat + 1} has no cumulative drift')
        assert page.evaluate(PREFS) == normal, 'repeated refresh must not write normal preferences'
    second, second_errors = load(context, base)
    check(second, 'second tab does not inherit nearby task', '!App.state.nearby.active&&geoRequests.length===0')
    second.evaluate('App.act.applyFilters({region:26,ratingMin:4.0});App.set({sort:"name"});MapMod.restoreView([34.69,135.5],12)')
    settle(second)
    latest_normal = second.evaluate(PREFS)
    page.evaluate('App.act.exitNearby()')
    settle(page)
    page.evaluate('window.dispatchEvent(new Event("pagehide"))')
    assert page.evaluate(PREFS) == latest_normal, 'exit must not overwrite a newer normal plan from another tab'
    check(page, 'exit protects the other tab normal preferences', '!App.state.nearby.active&&App.state.nearby.planning===null')
    page.evaluate('App.act.applyFilters({ratingMin:3.9})')
    settle(page)
    check(page, 'new deliberate planning edit resumes normal preference writes', 'JSON.parse(localStorage.getItem("tabelog.filterState")).rating===3.9')
    second.close()

    # Corrupt/stale fixes suspend the full task and retain its complete planning snapshot.
    page.evaluate('App.act.toggleNearby()')
    latest_fix(page)
    page.evaluate('window.bfcachePlanning=JSON.stringify(App.state.nearby.planning,(k,v)=>v instanceof Set?[...v]:v);App.state.nearby.fix.ts=Date.now()-600001;App.act.locate();window.bfcacheRequests=geoRequests.length;window.dispatchEvent(new PageTransitionEvent("pageshow",{persisted:true}));window.bfcacheFix=JSON.stringify(App.state.nearby.fix);giveFix(geoRequests.length-1)')
    check(page, 'bfcache resumes stale task without permission and invalidates old native callback', 'geoRequests.length===bfcacheRequests&&!App.state.nearby.pending&&App.state.nearby.needsLocation&&Data.resultScope().paused&&JSON.stringify(App.state.nearby.fix)===bfcacheFix&&JSON.stringify(App.state.nearby.planning,(k,v)=>v instanceof Set?[...v]:v)===bfcachePlanning')
    page.evaluate('App.act.locate()')
    latest_fix(page)
    session = json.loads(page.evaluate('sessionStorage.getItem("tabelog.nearbySession.v1")'))
    for kind in ['expired', 'missing', 'future', 'bad']:
        broken = json.loads(json.dumps(session))
        if kind == 'expired': broken['fix']['ts'] = 1
        if kind == 'missing': broken['fix'].pop('ts', None)
        if kind == 'future': broken['fix']['ts'] = 9999999999999
        if kind == 'bad': broken['fix']['lat'] = 91
        stale = context.new_page()
        stale.route('https://**/*', lambda route: route.abort())
        stale.add_init_script(GEO)
        stale.add_init_script('sessionStorage.setItem("tabelog.nearbySession.v1",' + json.dumps(json.dumps(broken)) + ')')
        lib_browser.boot(stale, base)
        settle(stale)
        check(stale, f'{kind} persisted fix pauses instead of national/zero results', 'geoRequests.length===0&&App.state.nearby.active&&App.state.nearby.needsLocation&&!!App.state.nearby.planning&&Data.resultScope().paused&&Data.locationOrigin()===null')
        stale.close()
    page.evaluate('App.act.exitNearby()')
    settle(page)
    context.close()

    # Old 4.1.x contexts only have region/sort/view: restore what exists and migrate to normal.
    for spelling in ['award', 'awards']:
        context = browser.new_context(service_workers='block', reduced_motion='reduce')
        page = context.new_page()
        page.route('https://**/*', lambda route: route.abort())
        page.add_init_script(GEO)
        lib_browser.seed_local_storage(page, {'tabelog.lang': 'zh-CN', 'tabelog.seenIntro': '1',
            'tabelog.listView': json.dumps({'sort': 'distance', 'nearbyActive': True, 'planningContext': {'region': 12, 'sort': spelling, 'center': [36.2, 138.8], 'zoom': 9}}),
            'tabelog.filterState': json.dumps({'rating': 4, 'prices': [], 'genres': [], 'awards': ['gold'], 'hideBlack': False})})
        lib_browser.boot(page, base)
        settle(page)
        page.wait_for_function('MapMod.map.getZoom()===9')
        settle(page)
        check(page, f'legacy {spelling} context migrates to recoverable normal plan', 'geoRequests.length===0&&!App.state.nearby.active&&App.state.nearby.planning===null&&App.state.filters.region===12&&App.state.filters.ratingMin===4&&App.state.filters.budgets.size===0&&App.state.filters.cuisines.size===0&&App.state.sort==="awards"&&MapMod.map.getZoom()===9&&MapMod.map.project(MapMod.map.getCenter(),9).distanceTo(MapMod.map.project([36.2,138.8],9))<=1')
        context.close()
    browser.close()

(args.output / f'{args.browser}.json').write_text(json.dumps({'browser': args.browser, 'records': records, 'errors': errors + second_errors}, ensure_ascii=False, indent=2))
assert not errors + second_errors, errors + second_errors
print(f'{args.browser}: {len(records)} nearby checks passed')
