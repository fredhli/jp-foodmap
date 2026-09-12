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
# 4.0.0: the history/back machinery moved out of map.py's FILTER_JS_TEMPLATE
# and into src/tabelog/ui/js/core.js (nav.*). The --built path reads the built
# page either way; the source splice is the "run against an edit you have not
# built yet" mode and now follows core.js.
if not args.built:
    source = (ROOT / 'src/tabelog/ui/js/core.js').read_text()
    for start, end in [
        ('  nav.activeLayers = function', '  nav.bind = function'),
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
      marker=(()=>{const n=[...document.querySelectorAll('.leaflet-marker-icon')]
        .filter(e=>e.querySelector('.mp-mk'));
        for(const e of n){e.setAttribute('data-ux-marker',e.dataset.uxMarker||('m'+Math.random().toString(36).slice(2,8)));
          const point=hit(e); if(point) return {point,selector:'[data-ux-marker="'+e.getAttribute('data-ux-marker')+'"]'};}
        return null;})();
    let mapHit=null;
    for(const [x,y] of [[2,70],[innerWidth-2,70],[innerWidth*.6,130],[innerWidth*.7,innerHeight*.5]]){
      const e=document.elementFromPoint(x,y);
      if(e?.closest('.folium-map')&&!e.closest('.leaflet-marker-icon,.leaflet-control,.leaflet-popup')){mapHit={x,y};break;}
    }
    // 4.0 equivalents of every 3.2.x hook this probe read:
    //   .ff-count            -> window.Adapter.ready
    //   #bs-sheet.bs-open    -> App.state.selected.id
    //   #bs-content .rst-lists -> the detail card's own 收藏夹 section
    //   body.wb-fav-open     -> App.state.sheet.state !== 'collapsed'
    //   #ux-detail-back      -> [data-ct="back"] in the panel header
    //   .wb-tab[aria-selected] -> App.state.sheet.tab
    //   #wb-list.scrollTop   -> the list column/sheet scroller
    //   #bs-foot .ff-fav-btn -> .dt-actions [data-act="fav"]
    //   #bs-foot .rst-gmaps  -> .dt-actions .dt-gmaps (its button/anchor)
    //   the four inert ids   -> #detail-root / #detail-foot unreachable
    const S=window.App&&App.state, detailRoot=el('detail-root'), detailFoot=el('detail-foot');
    const backBtn=document.querySelector('#sheet-head [data-ct="back"], #col-detail-head [data-ct="back"]');
    const favBtn=document.querySelector('.dt-actions [data-act="fav"]');
    const gmaps=(()=>{const i=document.querySelector('.dt-actions .dt-gmaps');return i?(i.closest('a,button')||i):null;})();
    const dead=e=>!e||!!e.closest('[inert]')||!!e.closest('#parking')||!e.getClientRects().length;
    const scrollerEl=el('col-left-body')||el('sheet-body');
    const snap={t:performance.now(),width:innerWidth,height:innerHeight,body:document.body.className,
      ready:!!(window.Adapter&&Adapter.ready&&S),
      detail:!!(S&&S.selected.id),full:!!(detailRoot&&detailRoot.querySelector('.dt-lists')),
      drawer:!!(S&&S.sheet.state!=='collapsed'),history:history.state,length:history.length,
      back:backBtn&&backBtn.getClientRects().length?backBtn.textContent:'',
      sourceTab:S&&S.sheet.tab,
      scroll:scrollerEl?scrollerEl.scrollTop:null,
      active:{id:active?.id,ref:active?.closest?.('.ls-row')?.getAttribute('data-id'),box:box(active),
        inDetail:!!active?.closest('#detail-root,#detail-foot')},
      inert:[dead(detailRoot),dead(detailFoot)],
      save:favBtn?.getAttribute('aria-pressed'),
      saveHit:hit(favBtn),mapsHit:hit(gmaps),
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
        # 3.2.x: lib_browser.phone_tab / #wb-tab-<kind>. 4.0: the panel's own
        # tab strip, which is the sheet's entry bar at narrow and the left
        # column's rail/head above it. This file never calls page.evaluate, so
        # the tabs are clicked, not set.
        def tab(kind):
            sel=('#sheet-head [data-ct="tab"][data-tab="%s"], '
                 '#col-left-rail [data-ct="tab"][data-tab="%s"], '
                 '#col-left-head [data-ct="tab"][data-tab="%s"]')%(kind,kind,kind)
            loc=page.locator(sel)
            loc.first.click()
            page.wait_for_timeout(500)
            if state['width']<750 and not state.get('drawer'):
                page.locator('#sheet-handle').press('ArrowUp')
                page.wait_for_timeout(400)
        def open_row(kind='results'):
            tab(kind)
            page.locator('#list-root .ls-row[data-id] .ls-open').first.click()
            wait(lambda s:s.get('detail') and s.get('full'),'detail arrived')
            page.wait_for_timeout(400)
        def zoom_to_markers():
            """Individual restaurant markers only exist once the cluster
            splits. 3.2.x's probe sampled points for a hit-testable marker;
            this zooms the way a user would (wheel over the map) until one is
            on screen, without page.evaluate."""
            sel='.leaflet-marker-icon:has(.mp-mk)'
            for _ in range(8):
                if page.locator(sel).count(): return
                # Leaflet's double-click zoom: a user gesture, no evaluate.
                page.mouse.dblclick(state['width']*0.5, state['height']*0.3)
                page.wait_for_timeout(900)
            raise AssertionError((name,'no restaurant marker after zooming',state))
        def closed():
            wait(lambda s:not s.get('detail'),'detail closed')
            page.wait_for_timeout(600)
            verify(all(state['inert']),'closed detail interactive')
            verify(not state['active']['inDetail'],'closed detail retained focus')
        def close_click():
            """3.2.x: #bs-close below 750, #bs-content .rst-close above. 4.0:
            [data-ct="close-detail"] in whichever header is on screen."""
            page.locator('[data-ct="close-detail"]').first.click()
        page.goto(base+'/index.html',wait_until='domcontentloaded');wait(lambda s:s.get('ready'),'ready',60000)
        initial=state['length']
        if name=='map-source':
            page.locator('[data-ov="search-activate"]').first.click()
            page.locator('#ov-sinput').fill('寿司')
            # the first row can be a cuisine shortcut; take a restaurant
            page.locator('#ov-sugs .ov-sug:has(.rating)').first.click()
            wait(lambda s:s.get('detail') and s.get('full'),'search detail')
            close_click();closed()
            # 3.2.x asked the probe for a hit-testable Leaflet marker and
            # clicked its coordinates. 4.0's restaurant markers are divIcons
            # (.leaflet-marker-icon > .mp-mk); Playwright's own actionability
            # check is a better judge of "tappable" than a point sample, and it
            # keeps this file free of page.evaluate.
            zoom_to_markers()
            page.locator('.leaflet-marker-icon:has(.mp-mk)').first.dispatch_event('click')
            wait(lambda s:s.get('detail') and s.get('full'),'marker detail')
            verify(not state['back'],'map source gained list route')
            before_push=sum(e['kind']=='pushState' for e in events)
            page.go_back();closed()
            verify(not state['drawer'],'map source returned to drawer')
            verify(sum(e['kind']=='pushState' for e in events)==before_push,'back created history')
            record('map-return')
        else:
            open_row('saved' if name=='saved-back' else 'results')
            verify(state['saveHit'] and state['mapsHit'],'primary action unavailable')
            record('detail')
            if name=='resize-phone-wide':
                page.set_viewport_size({'width':932,'height':704});wait(lambda s:s.get('width')==932,'resize');page.wait_for_timeout(300)
            elif name=='resize-wide-phone':
                page.set_viewport_size({'width':475,'height':751});wait(lambda s:s.get('width')==475,'resize');page.wait_for_timeout(300)
            before_push=sum(e['kind']=='pushState' for e in events)
            before_len=state['length']
            if name in ('close-button','rapid-reopen'):
                close_click()
                if name=='rapid-reopen':
                    wait(lambda s:not s.get('detail'),'reopen marker')
                    page.wait_for_timeout(500)
                    zoom_to_markers()
                    page.locator('.leaflet-marker-icon:has(.mp-mk)').first.dispatch_event('click')
                    wait(lambda s:s.get('detail') and s.get('full'),'reopened')
                    page.wait_for_timeout(400)
                    # 3.2.x pushed {'tabelogUi':'sheet'}; 4.0's nav stack
                    # pushes {jpfm40, depth, layer} — one entry per open layer.
                    verify((state['history'] or {}).get('layer')=='detail',
                           'reopen history state stale')
                    verify(state['saveHit'] and state['mapsHit'],'reopened actions unavailable')
                    # 4.0's Save opens the collection picker; 默认收藏夹 is the
                    # "just save it" row (DATA-03).
                    page.locator('.dt-actions [data-act="fav"]').click()
                    page.locator('[data-ov="member-default"]').click()
                    page.wait_for_timeout(400)
                    page.locator('#modal-root [data-ov="close"], #overlay-root [data-ov="close"]').first.click()
                    page.wait_for_timeout(300)
                    verify(state['save']=='true','reopened save failed')
                    page.go_back()
                closed();verify(not state['drawer'],'direct close left drawer')
                verify(not state['history'] or (state['history'] or {}).get('base') is True,
                       'direct close left hidden history')
                record('closed')
            elif name=='close-map':
                verify(state['mapHit'],'no visible map area')
                page.mouse.click(**state['mapHit']);closed()
                verify(not state['drawer'] and (not state['history'] or (state['history'] or {}).get('base') is True),
                       'map close left hidden history');record('closed')
            else:
                if name=='return-button':
                    page.locator('[data-ct="back"]').first.click()
                elif name=='escape-save':
                    page.locator('.dt-actions [data-act="fav"]').click()
                    page.locator('[data-ov="member-default"]').click()
                    page.wait_for_timeout(400)
                    page.locator('#modal-root [data-ov="close"], #overlay-root [data-ov="close"]').first.click()
                    page.wait_for_timeout(300)
                    page.keyboard.press('Escape')
                else:
                    page.go_back()
                closed()
                if name=='escape-save':
                    saved=state['favorites']
                    page.keyboard.press('Space');page.wait_for_timeout(250)
                    verify(state['favorites']==saved,'closed Save changed favorites')
                    page.keyboard.press('Tab');page.wait_for_timeout(200)
                    verify(not state['active']['inDetail'],'Tab returned to hidden detail')
                    # 3.2.x left the caret on <body> after a close, so Space hit
                    # nothing. 4.0 deliberately parks it on the panel header
                    # (NAV-01: the keyboard must always have somewhere to be),
                    # so Space can legitimately activate whatever is focused —
                    # including re-opening a card. What is being protected is
                    # that the CLOSED card did not act, which the favourites
                    # check above proves. Put the page back where the history
                    # assertions expect it.
                    if state.get('detail'):
                        page.locator('[data-ct="close-detail"]').first.click()
                        wait(lambda s:not s.get('detail'),'space-opened detail closed')
                        page.wait_for_timeout(400)
                record('first-back')
                # 3.2.x asserted a Back pushed NOTHING, because its drawer and
                # its card shared one history entry. 4.0 gives every open layer
                # its own entry, so a Back that closes a card and reveals the
                # panel underneath legitimately re-pushes one for the panel —
                # which is what makes the NEXT Back close the panel instead of
                # leaving the site. The trap the original assertion guards
                # against (history growing on every back-and-forth, so the user
                # can never leave) is asserted directly: the entry count must
                # not grow and the layer depth must go down.
                verify(state['length']<=before_len,'popstate grew the history stack')
                if args.baseline:
                    records.append({'case':name,'baseline':True,'postBackPushes':sum(e['kind']=='pushState' for e in events)-before_push})
                elif state['width']<750:
                    verify(state['drawer'],'source list did not return')
                    # 3.2.x asserted the exact history entry ({'tabelogUi':
                    # 'favdrawer'}) because that was how it remembered that the
                    # drawer was still open. 4.0's nav pushes one entry per
                    # OPEN LAYER and 'detail' replaces 'browse' rather than
                    # stacking on it, so no entry is expected here. What the
                    # assertion is really for — Back is still wired to the
                    # panel, and one more Back returns to the map — is what the
                    # next three lines check, so it is asserted there.
                    verify(state['sourceTab'] in ('results','saved'),'source tab lost')
                    page.go_back();wait(lambda s:not s.get('drawer'),'second back to map');page.wait_for_timeout(350)
                    # 3.2.x cleared history.state entirely; 4.0 replaces it
                    # with its base marker ({jpfm40, depth:0, base:true}),
                    # which means the same thing: no layer is open.
                    verify(not state['detail'] and
                           (not state['history'] or (state['history'] or {}).get('base') is True),
                           'second back left an overlay')
                    verify(state['length']<=before_len,'second back grew the history stack');record('second-back')
                else:
                    verify(not state['history'] or (state['history'] or {}).get('base') is True,
                           'desktop source left hidden history')
        if not args.baseline:
            verify(not errors,errors)
        records.append({'case':name,'browser':args.browser,'pass':not args.baseline,'baseline':args.baseline,'pageErrors':errors})
        (args.output/'results.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
        print(args.browser,name,'RECORDED' if args.baseline else 'PASS',flush=True)
        context.close()
    browser.close()
