"""Restaurant reveal regression using real touch taps and protected pin geometry.

All HTTPS requests are blocked. --baseline records known failures without failing.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tests'))
import lib_browser
from playwright.sync_api import sync_playwright

parser=argparse.ArgumentParser()
parser.add_argument('--docs',type=Path,default=ROOT/'docs')
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--browser',choices=['chromium','webkit'],default='chromium')
parser.add_argument('--baseline',action='store_true')
parser.add_argument('--cases',default='393:600,393:852,475:751,591:689')
args=parser.parse_args()
args.output.mkdir(parents=True,exist_ok=True)
lib_browser.DOCS=args.docs.resolve()
html=(args.docs/'index.html').read_text()
(args.output/'index-under-test.html').write_text(html)
A='https://tabelog.com/toyama/A1601/A160101/16010653/'
B='https://tabelog.com/toyama/A1604/A160402/16000400/'
rows=json.loads((args.docs/'data/restaurants.json').read_text())
B=next(r['detail_url'] for r in rows if r['detail_url'].endswith('/16000400/'))
seed={'tabelog.lang':'zh-CN','tabelog.seenIntro':'1',
      'omakase_state_cache_v2':json.dumps({'fav':[A,B],'black':[],'dirty':False})}
records,failures=[],[]
probe="""()=>{
 const s=App.state,m=MapMod.map,r=s.layout.mapRect,pt=s.selected.id?m.latLngToContainerPoint([Data.byId(s.selected.id).lat,Data.byId(s.selected.id).lon]):null;
 const el=document.querySelector('.mp-mk.is-selected .mp-dot'),dot=el&&el.getBoundingClientRect();
 const pin=dot&&{left:dot.left-4,right:dot.right+4,top:dot.top-4,bottom:dot.bottom+4,x:dot.x-4,y:dot.y-4,width:dot.width+8,height:dot.height+8};
 const tagEl=document.querySelector('.mp-mk.is-selected .mp-tag'),tag=tagEl&&tagEl.getBoundingClientRect();
 const obstacles=[...document.querySelectorAll('#search-root,#sheet,#fab-root,.leaflet-control-attribution,.leaflet-control-scale,#candidates-host > #candidates-root,#notice-root > *')].map(e=>e.getBoundingClientRect()).filter(b=>b.width&&b.height);
 const overlap=(a,b)=>a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top;
 return {selected:s.selected.id,rect:r,point:pt&&[pt.x,pt.y],pan:window.panCalls.slice(),events:window.moveEvents.slice(),dragged:s.userDraggedMap,
  markerHit:!!pin&&!!el&&(()=>{const hit=document.elementFromPoint((pin.left+pin.right)/2,(pin.top+pin.bottom)/2);return hit===el||el.contains(hit)})(),
  tagVisible:!!tag&&tag.width>0&&tag.left>=r.x&&tag.right<=r.x+r.w&&tag.top>=r.y&&tag.bottom<=r.y+r.h&&!obstacles.some(b=>overlap(tag,b)),
  visible:!!pin&&pin.width>0&&pin.left>=r.x&&pin.right<=r.x+r.w&&pin.top>=r.y&&pin.bottom<=r.y+r.h&&!obstacles.some(b=>overlap(pin,b)),
  pin:pin&&{x:pin.x,y:pin.y,w:pin.width,h:pin.height},center:m.getCenter(),geometryBusy:App.motion.isGeometryBusy(),trace:window.gestureTrace,animating:!!(m._panAnim&&m._panAnim._inProgress)};
}"""

def check(ok,msg):
    if not ok:
        failures.append(msg)
        (args.output/'results.json').write_text(json.dumps({'records':records,'failures':failures},indent=2))
        if not args.baseline: raise AssertionError(msg)

def record(page,name,visible=True,maxpan=1):
    page.wait_for_timeout(1000)
    d=page.evaluate(probe)
    records.append({'name':name,**d})
    (args.output/'results.json').write_text(json.dumps({'records':records,'failures':failures},indent=2))
    print(name,'visible',d['visible'],'map height',d['rect']['h'],'pans',len(d['pan']),flush=True)
    if visible:
        check(d['visible'],name+': actual pin obscured '+str(d))
        check(d['markerHit'],name+': selected marker is not hit-testable '+str(d))
        check(d['tagVisible'],name+': selected price tag obscured '+str(d))
    check(len(d['pan'])<=maxpan,name+': repeated pan '+str(d['pan']))
    return d

def reset(page):
    page.evaluate("""()=>{App.act.closeDetail();MapMod.map.setView([35.6812,139.7671],15,{animate:false});App.set({userDraggedMap:false});App.act.setTab('saved');App.act.setSheet('expanded')}""")
    page.wait_for_timeout(550)
    page.evaluate('window.panCalls=[];window.moveEvents=[]')

with lib_browser.serve_docs(8993) as base,sync_playwright() as p:
    browser=getattr(p,args.browser).launch()
    for width,height in [tuple(map(int,c.split(':'))) for c in args.cases.split(',')]:
        context=browser.new_context(viewport={'width':width,'height':height},screen={'width':475 if width==475 else width,'height':751 if width==475 else height},has_touch=True,is_mobile=True,service_workers='block')
        page=context.new_page()
        page.route('https://**/*',lambda r:r.abort())
        page.route('**/index.html',lambda r:r.fulfill(content_type='text/html',body=html))
        lib_browser.seed_local_storage(page,seed)
        lib_browser.boot(page,base)
        check(page.evaluate('MapMod.map.options.trackResize===false'),'core must be the only resize owner')
        page.evaluate("""()=>{window.panCalls=[];window.moveEvents=[];window.gestureTrace=[];const m=MapMod.map,p=m.panBy;['pointerdown','pointerup','pointercancel'].forEach(n=>window.addEventListener(n,e=>gestureTrace.push([n,e.pointerId,performance.now()])));['movestart','moveend','zoomstart','zoomend','dragstart','dragend'].forEach(n=>m.on(n,()=>gestureTrace.push([n,performance.now()])));m.panBy=function(...a){window.panCalls.push(a[0]);return p.apply(this,a)};App.on('map:moveend',e=>window.moveEvents.push({byUser:e.byUser}));}""")
        reset(page)
        page.locator('[data-id="'+A+'"] .ls-open').tap()
        d=record(page,f'{width}x{height}-saved-tap')
        check(all(not e['byUser'] for e in d['events']),f'{width}: program pan reported as user')
        page.evaluate('window.panCalls=[];window.moveEvents=[]')
        page.evaluate('App.act.openDetail(App.state.selected.id,"results")')
        record(page,f'{width}-already-visible',maxpan=0)
        reset(page)
        page.evaluate("""id=>{window.releaseGeometry=App.motion.geometryBusy(new Promise(()=>{}));App.act.openDetail(id,'results')}""",B)
        page.wait_for_timeout(80)
        page.evaluate('App.act.closeDetail();window.releaseGeometry()')
        record(page,f'{width}-stale-close',visible=False,maxpan=0)
        reset(page)
        page.evaluate("""([a,b])=>{window.releaseGeometry=App.motion.geometryBusy(new Promise(()=>{}));App.act.openDetail(a,'results');App.act.openDetail(b,'search')}""",[A,B])
        page.wait_for_timeout(80)
        page.evaluate('window.releaseGeometry()')
        d=record(page,f'{width}-rapid-select')
        check(d['selected']==B,f'{width}: latest selection')
        if width>=750:
            reset(page)
            page.evaluate('MapMod.map.setView([36.86,137.4],15,{animate:false})')
            page.wait_for_timeout(400)
            page.evaluate('window.panCalls=[];window.moveEvents=[]')
            page.locator('[data-id="'+A+'"] .ls-open').tap()
            record(page,'candidate-strip-obstacle')
        reset(page)
        page.evaluate("id=>{App.act.openDetail(id,'search');App.act.setSheet('full')}",A)
        record(page,f'{width}-{height}-zero-space',visible=width>=750,maxpan=1 if width>=750 else 0)
        page.evaluate('App.act.setSheet("detail")')
        record(page,f'{width}-{height}-deferred-visible')
        if width==393 and height==852:
            page.evaluate('App.act.closeDetail();App.act.setSheet("collapsed")')
            page.wait_for_timeout(500)
            page.mouse.move(150,250);page.mouse.down();page.mouse.move(240,310,steps=12);page.mouse.up()
            page.wait_for_timeout(400)
            check(page.evaluate('App.state.userDraggedMap'), 'real drag did not reach Leaflet')
            page.evaluate('window.panCalls=[];window.moveEvents=[]')
            page.evaluate("id=>App.act.openDetail(id,'candidates')",A)
            page.wait_for_function('!MapMod.map._panAnim || !MapMod.map._panAnim._inProgress',timeout=10000)
            d=record(page,'drag-new-selection')
            check(not d['dragged'],'new selection retains old drag suppression')
            page.evaluate('window.panCalls=[];window.moveEvents=[]')
            page.set_viewport_size({'width':393,'height':600})
            record(page,'resize-short-strip')
            page.evaluate('window.panCalls=[];window.moveEvents=[];App.act.setSheet("expanded")')
            record(page,'sheet-expanded')
        if width==393 and height==852:
            page.set_viewport_size({'width':393,'height':852})
            reset(page)
            page.evaluate("id=>App.act.openDetail(id,'results')",A)
            page.wait_for_function('window.panCalls.length>0&&MapMod.map._panAnim&&MapMod.map._panAnim._inProgress',timeout=5000)
            page.set_viewport_size({'width':393,'height':640})
            d=record(page,'resize-during-program-pan',maxpan=2)
            check(all(not e['byUser'] for e in d['events']),'resize released program movement identity early')
            page.set_viewport_size({'width':393,'height':852})
            reset(page)
            page.evaluate("""()=>{const v=window.visualViewport;
              Object.defineProperty(v,'offsetLeft',{configurable:true,get:()=>13});
              Object.defineProperty(v,'offsetTop',{configurable:true,get:()=>23});
              document.documentElement.style.setProperty('--app-inset-top','24px');
              App.layout.schedule();}""")
            page.wait_for_timeout(500)
            page.evaluate('window.panCalls=[];window.moveEvents=[]')
            page.locator('[data-id="'+A+'"] .ls-open').tap()
            d=record(page,'nonzero-usable-offset')
            check(page.evaluate('App.state.layout.U.x>0&&App.state.layout.U.y>0'),'offset fixture is not active')
            check(page.evaluate("""()=>{const m=MapMod.map,r=App.state.layout.mapRect,b=m.getContainer().getBoundingClientRect(),p=m.containerPointToLatLng([r.x-b.left,r.y-b.top]),x=MapMod.bounds();return Math.abs(p.lat-x.north)<1e-8&&Math.abs(p.lng-x.west)<1e-8}"""),'visible bounds use the wrong origin')
        if height==852 or width>=750 or width==475:
            page.evaluate("""()=>{App.act.closeDetail();App.act.applyFilters({region:25});App.set({sort:'price'});App.act.setTab('results');if(App.state.layout.mode==='narrow')App.act.setSheet('collapsed');MapMod.map.setView([35.01,135.77],11,{animate:false});navigator.geolocation.getCurrentPosition=ok=>setTimeout(()=>ok({coords:{latitude:35.6812,longitude:139.7671,accuracy:10},timestamp:Date.now()}),30)}""")
            page.wait_for_timeout(500)
            planned_center=page.evaluate('MapMod.map.getCenter()')
            page.locator('[data-fab="locate"]').tap()
            page.wait_for_function("App.state.sort==='distance'",timeout=15000)
            page.wait_for_timeout(700)
            planned=page.evaluate("JSON.parse(localStorage.getItem('tabelog.listView')).planningContext")
            page.reload()
            lib_browser.wait_ready(page)
            page.evaluate("""()=>{window.restoreCalls=[];const m=MapMod.map;['setView','flyTo','setZoom','_stop','invalidateSize'].forEach(k=>{const old=m[k];m[k]=function(...a){restoreCalls.push({method:k,args:a,time:performance.now(),zoom:m.getZoom(),stack:new Error().stack});return old.apply(this,a)}});['movestart','moveend','zoomstart','zoomend'].forEach(k=>m.on(k,()=>restoreCalls.push({event:k,time:performance.now(),zoom:m.getZoom()})))}""")
            page.wait_for_timeout(650)
            check(page.evaluate("JSON.parse(localStorage.getItem('tabelog.listView')).planningContext")==planned,'nearby reload lost planning context')
            page.locator('[data-ov="restore-plan"]:visible').first.tap()
            page.evaluate("""()=>{window.restorePressure=[];[80,160,240,320,360,400,416,432].forEach(ms=>setTimeout(()=>{restorePressure.push(MapMod.map.getZoom());App.emit('layout:settled',App.state.layout)},ms))}""")
            try:
                page.wait_for_function('MapMod.map.getZoom()===11',timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(500)
            error=page.evaluate('p=>MapMod.map.project(MapMod.map.getCenter(),11).distanceTo(MapMod.map.project(p,11))',planned_center)
            pressure=page.evaluate('window.restorePressure')
            restored=page.evaluate('({zoom:MapMod.map.getZoom(),region:App.state.filters.region,sort:App.state.sort})')
            records.append({'name':str(width)+'-restore-view','errorPx':error,'planning':planned,'restored':restored,'pressureZooms':pressure,'trace':page.evaluate('window.restoreCalls')})
            check(any(z!=11 for z in pressure),'restore pressure missed the in-flight view')
            check(error<=1,'restore-plan changed the saved view centre by '+str(error)+'px')
            check(restored=={'zoom':11,'region':25,'sort':'price'},'restore-plan lost centre/zoom/filter semantics')
        if height==852 or width==475:
            for kind in ['point','view']:
                page.evaluate("""()=>{App.act.closeDetail();App.act.setSheet('collapsed');MapMod.map.setView([35.6812,139.7671],15,{animate:false});if(!window.locationWatch){locationWatch=true;const old=MapMod.map.flyTo;MapMod.map.flyTo=function(...a){window.locationMoves.push(a);return old.apply(this,a)};App.on('map:moveend',e=>window.locationEvents.push({byUser:e.byUser}))}window.locationMoves=[];window.locationEvents=[];MapMod.placeTempPin({lat:36.8562395,lon:136.9894408,label:'目的地'})}""")
                page.wait_for_timeout(500)
                page.evaluate('window.locationMoves=[];window.locationEvents=[]')
                page.mouse.move(125,250);page.mouse.down();page.mouse.move(185,285,steps=12)
                check(page.evaluate('App.state.userDraggedMap'),'held gesture did not reach Leaflet')
                page.evaluate("kind=>MapMod[kind==='point'?'flyTo':'restoreView']([36.8562395,136.9894408],15)",kind)
                page.wait_for_timeout(150)
                check(page.evaluate('window.locationMoves.length===0'),'location moved while a gesture was held')
                page.mouse.up()
                page.wait_for_function('window.locationEvents.some(e=>!e.byUser)',timeout=8000)
                result=page.evaluate("""kind=>{const m=MapMod.map,r=App.state.layout.mapRect,el=document.querySelector('.mp-pin.is-temp'),b=el&&el.getBoundingClientRect(),hit=b&&document.elementFromPoint(b.x+b.width/2,b.y+12);return {kind,moves:locationMoves.length,events:locationEvents,zoom:m.getZoom(),errorPx:m.project(m.getCenter(),15).distanceTo(m.project([36.8562395,136.9894408],15)),visible:!!b&&b.width>0&&b.left>=r.x&&b.right<=r.x+r.w&&b.top>=r.y&&b.bottom<=r.y+r.h&&!!hit&&(hit===el||el.contains(hit))}}""",kind)
                records.append({'name':str(width)+'-gesture-release-'+kind,**result})
                check(result['moves']==1 and result['zoom']==15,'released location did not execute exactly once')
                check(result['events'][0]['byUser'] and not result['events'][-1]['byUser'],'gesture release changed movement source')
                check(result['visible'] if kind=='point' else result['errorPx']<=1,'released location/view missed its target')
        context.close()
    browser.close()
(args.output/'results.json').write_text(json.dumps({'records':records,'failures':failures},indent=2))
print(f'{len(records)} scenarios; {len(failures)} failures')
