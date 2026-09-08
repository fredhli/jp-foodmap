"""History/source regressions without observer-injected user activation.

All snapshots come from a navigation-time script and console events. This test
never calls page.evaluate, eval_on_selector, wait_for_function or CDP evaluation.
Browser history traversal is still not a substitute for the real Android Back test.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))
import lib_browser
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('--browser', choices=['chromium', 'webkit'], default='chromium')
parser.add_argument('--built', action='store_true')
parser.add_argument('--baseline', action='store_true')
parser.add_argument('--output', required=True, type=Path)
parser.add_argument('--cases', help='Comma separated scenario names')
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
html = (ROOT / 'docs/index.html').read_text()
if not args.built:
    source = (ROOT / 'src/tabelog/scrape/map.py').read_text()
    for start, end in [
        ('    var uiStack = [];', '    // ===== H2 / M-074'),
        ('    var uxDetailSource = null;', '    var bsGrip'),
        ('    function openSheet(d, opts)', '    // Close the bottom sheet'),
        ('    function closeFavDrawer(', '    // Re-uses the existing wb:mode'),
    ]:
        a, c = html.index(start), source.index(start)
        b, d = html.index(end, a), source.index(end, c)
        html = html[:a] + source[c:d] + html[b:]

seed_ref = json.loads((ROOT / 'docs/data/restaurants.json').read_text())[0]['detail_url']
probe = r"""(() => {
  localStorage.setItem('tabelog.lang','zh-CN');
  localStorage.setItem('tabelog.seenIntro','1');
  localStorage.setItem('tabelog.persistAsked','1');
  let events=[], seq=0;
  const log=(kind, detail)=>events.push({seq:++seq,kind,detail,t:performance.now(),
    active:navigator.userActivation?.isActive,everActive:navigator.userActivation?.hasBeenActive});
  for(const name of ['pushState','replaceState','back','go']){
    const fn=history[name].bind(history);
    history[name]=function(...a){log(name,a[0]);return fn(...a)};
  }
  addEventListener('popstate',e=>log('popstate',e.state));
  document.addEventListener('pointerdown',e=>log('pointer',e.target.closest('button,a')?.id||e.target.className),true);
  const box=e=>{if(!e)return null;const r=e.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height}};
  const hit=e=>{if(!e)return null;const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2,t=document.elementFromPoint(x,y);
    return r.width&&r.height&&t&&(t===e||e.contains(t))?{x,y}:null};
  setInterval(()=>{
    if(!document.body)return;
    const el=id=>document.getElementById(id), active=document.activeElement,
      map=Object.values(window).find(v=>v&&v._container&&v._layers&&v.getCenter),
      marker=map?Object.values(map._layers).filter(m=>m._d&&m._icon).map(m=>{
        m._icon.setAttribute('data-ux-marker',m._leaflet_id);
        const point=hit(m._icon);return point?{point,selector:'[data-ux-marker="'+m._leaflet_id+'"]'}:null;
      }).find(Boolean):null;
    let mapHit=null;
    for(const [x,y] of [[2,70],[innerWidth-2,70],[innerWidth*.6,130],[innerWidth*.7,innerHeight*.5]]){
      const e=document.elementFromPoint(x,y);
      if(e?.closest('.folium-map')&&!e.closest('.leaflet-marker-icon,.leaflet-control,.leaflet-popup')){mapHit={x,y};break;}
    }
    const snap={t:performance.now(),width:innerWidth,height:innerHeight,body:document.body.className,
      ready:!!document.querySelector('.ff-count')?.textContent.match(/\d/),
      detail:el('bs-sheet')?.classList.contains('bs-open'),full:!!el('bs-content')?.querySelector('.rst-lists'),
      drawer:document.body.classList.contains('wb-fav-open'),history:history.state,length:history.length,
      back:el('ux-detail-back')?.hidden?'':el('ux-detail-back')?.textContent,sourceTab:document.querySelector('.wb-tab[aria-selected=true]')?.id,
      scroll:el('wb-list')?.scrollTop,active:{id:active?.id,ref:active?.getAttribute('data-fav-ref'),box:box(active),
        inDetail:!!active?.closest('#bs-content,#bs-foot,#bs-head')},
      inert:['bs-content','bs-foot','ux-detail-back','bs-grip'].map(id=>!!el(id)?.inert),
      save:el('bs-foot')?.querySelector('.ff-fav-btn')?.getAttribute('aria-pressed'),
      saveHit:hit(el('bs-foot')?.querySelector('.ff-fav-btn')),mapsHit:hit(el('bs-foot')?.querySelector('.rst-gmaps')),
      favorites:JSON.parse(localStorage.getItem('omakase_state_cache_v2')||'{}').fav||[],
      marker:marker?.point,markerSelector:marker?.selector,mapHit,events:events.splice(0)};
    console.debug('UX_HISTORY '+JSON.stringify(snap));
  },80);
})()"""
records = []
scenarios = [
    ('results-back',475,751),('saved-back',475,751),('close-button',475,751),
    ('close-map',475,751),('map-source',475,751),('rapid-reopen',475,751),
    ('resize-phone-wide',475,751),('resize-wide-phone',1440,900),('desktop-return',1440,900),
    ('return-button',393,852),('escape-save',1440,900),
]
if args.baseline:
    scenarios = scenarios[:1]
if args.cases:
    scenarios = [s for s in scenarios if s[0] in args.cases.split(',')]

with lib_browser.serve_docs(8988 if args.browser=='chromium' else 8989) as base, sync_playwright() as p:
    browser=getattr(p,args.browser).launch()
    for name,w,h in scenarios:
        context=browser.new_context(viewport={'width':w,'height':h},is_mobile=w<1000,has_touch=w<1000,service_workers='block')
        page=context.new_page();page.set_default_timeout(12000)
        page.route('https://**/*',lambda r:r.abort())
        page.route('**/index.html',lambda r:r.fulfill(content_type='text/html',body=html))
        page.add_init_script(probe)
        if name=='saved-back':
            page.add_init_script("localStorage.setItem('omakase_state_cache_v2',JSON.stringify({fav:["+json.dumps(seed_ref)+"],black:[],dirty:false}))")
        state={};events=[];errors=[]
        def console(message):
            if message.text.startswith('UX_HISTORY '):
                snap=json.loads(message.text[len('UX_HISTORY '):]);events.extend(snap.pop('events'));state.clear();state.update(snap)
        page.on('console',console);page.on('pageerror',lambda e:errors.append(str(e)))
        def wait(condition,label,timeout=12000):
            deadline=time.monotonic()+timeout/1000
            while time.monotonic()<deadline:
                page.wait_for_timeout(100)
                if condition(state):return
            raise AssertionError((name,label,state))
        def record(label):
            records.append({'case':name,'label':label,**state,'events':events.copy()})
            (args.output/'results.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
            page.screenshot(path=str(args.output/(name+'-'+label+'.png')))
        def verify(condition,label):
            if not args.baseline:assert condition,(name,label,state)
        def tab(kind):
            if state['width']<750: lib_browser.phone_tab(page,kind)
            else: page.locator('#wb-tab-'+kind).click()
        def open_row(kind='results'):
            tab(kind)
            page.locator('.fv-row[data-fav-kind="rst"]' if kind=='fav' else '#wb-list .wb-row .wb-row-nm').first.click()
            wait(lambda s:s.get('detail') and s.get('full'),'detail arrived')
            page.wait_for_timeout(400)
        def closed():
            wait(lambda s:not s.get('detail'),'detail closed')
            page.wait_for_timeout(400)
            verify(all(state['inert']),'closed detail interactive')
            verify(not state['active']['inDetail'],'closed detail retained focus')
        page.goto(base+'/index.html',wait_until='domcontentloaded');wait(lambda s:s.get('ready'),'ready',60000)
        initial=state['length']
        if name=='map-source':
            page.locator('#ss-input').fill('寿司')
            page.locator('#ss-local .ss-row:not(.ss-empty)').first.click()
            wait(lambda s:s.get('detail') and s.get('full'),'search detail')
            page.locator('#bs-close' if state['width']<750 else '#bs-content .rst-close').click();closed()
            wait(lambda s:s.get('marker'),'visible marker')
            page.locator(state['markerSelector']).click();wait(lambda s:s.get('detail') and s.get('full'),'marker detail')
            verify(not state['back'],'map source gained list route')
            before_push=sum(e['kind']=='pushState' for e in events)
            page.go_back();closed()
            verify(not state['drawer'],'map source returned to drawer')
            verify(sum(e['kind']=='pushState' for e in events)==before_push,'back created history')
            record('map-return')
        else:
            open_row('fav' if name=='saved-back' else 'results')
            verify(state['saveHit'] and state['mapsHit'],'primary action unavailable')
            record('detail')
            if name=='resize-phone-wide':
                page.set_viewport_size({'width':932,'height':704});wait(lambda s:s.get('width')==932,'resize');page.wait_for_timeout(300)
            elif name=='resize-wide-phone':
                page.set_viewport_size({'width':475,'height':751});wait(lambda s:s.get('width')==475,'resize');page.wait_for_timeout(300)
            before_push=sum(e['kind']=='pushState' for e in events)
            if name in ('close-button','rapid-reopen'):
                page.locator('#bs-close' if state['width']<750 else '#bs-content .rst-close').click()
                if name=='rapid-reopen':
                    wait(lambda s:not s.get('detail') and s.get('marker'),'reopen marker')
                    page.locator(state['markerSelector']).click();wait(lambda s:s.get('detail') and s.get('full'),'reopened')
                    page.wait_for_timeout(400)
                    verify(state['history']=={'tabelogUi':'sheet'},'reopen history state stale')
                    verify(state['saveHit'] and state['mapsHit'],'reopened actions unavailable')
                    page.locator('#bs-foot .ff-fav-btn').click();page.wait_for_timeout(180)
                    verify(state['save']=='true','reopened save failed')
                    page.go_back()
                closed();verify(not state['drawer'],'direct close left drawer')
                verify(not state['history'],'direct close left hidden history')
                record('closed')
            elif name=='close-map':
                verify(state['mapHit'],'no visible map area')
                page.mouse.click(**state['mapHit']);closed()
                verify(not state['drawer'] and not state['history'],'map close left hidden history');record('closed')
            else:
                if name=='return-button':
                    page.locator('#ux-detail-back').click()
                elif name=='escape-save':
                    page.locator('#bs-foot .ff-fav-btn').click()
                    page.wait_for_timeout(150)
                    page.keyboard.press('Escape')
                else:
                    page.go_back()
                closed()
                if name=='escape-save':
                    saved=state['favorites']
                    page.keyboard.press('Space');page.wait_for_timeout(150)
                    verify(state['favorites']==saved,'closed Save changed favorites')
                    page.keyboard.press('Tab');page.wait_for_timeout(150)
                    verify(not state['active']['inDetail'],'Tab returned to hidden detail')
                record('first-back')
                verify(sum(e['kind']=='pushState' for e in events)==before_push,'popstate created new history')
                if args.baseline:
                    records.append({'case':name,'baseline':True,'postBackPushes':sum(e['kind']=='pushState' for e in events)-before_push})
                elif state['width']<750:
                    verify(state['drawer'],'source list did not return')
                    verify(state['history']=={'tabelogUi':'favdrawer'},'source history not restored')
                    page.go_back();wait(lambda s:not s.get('drawer'),'second back to map');page.wait_for_timeout(350)
                    verify(not state['detail'] and not state['history'],'second back left an overlay')
                    verify(sum(e['kind']=='pushState' for e in events)==before_push,'second back created history');record('second-back')
                else:
                    verify(not state['history'],'desktop source left hidden history')
        if not args.baseline:
            verify(not errors,errors)
        records.append({'case':name,'browser':args.browser,'pass':not args.baseline,'baseline':args.baseline,'pageErrors':errors})
        (args.output/'results.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
        print(args.browser,name,'RECORDED' if args.baseline else 'PASS',flush=True)
        context.close()
    browser.close()
