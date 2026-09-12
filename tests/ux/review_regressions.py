"""Regressions for place navigation, closed detail focus and nearby behaviour.

4.0.0 port. Every selector moved with the presentation layer; each comment
names the 3.2.x hook it replaces. Two groups of 3.2.x assertions have no 4.0
counterpart and are recorded here rather than silently dropped:

  * the nearby SORT COPY ("按距离 / 评分 / 价位" inside the #ux-nearby chip,
    and the >=750 rule that let #wb-region-btn carry the pressed nearby state
    instead). 4.0's chip row is fixed copy — 附近 is a toggle, the sort lives
    in the list header's own control — so there is no sentence to check. What
    survives and is still asserted: the chip is pressed while nearby is on,
    the planning context is exact, and a reload does not claim a sort it is
    not using.
  * `#sync-hint` sitting above `#ux-filter-done`. 4.0 has no filter-screen
    CTA of that kind (the footer's 查看 N 家结果 is inside the panel) and the
    signed-out hint is a #notice-root row over the map. The replacement
    assertion is that the hint never overlaps the filter footer.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tests'))
import lib_browser
from playwright.sync_api import sync_playwright

parser=argparse.ArgumentParser()
parser.add_argument('--docs',type=Path,default=ROOT/'docs')
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--browser',choices=['webkit','chromium'],default='webkit')
parser.add_argument('--baseline',action='store_true')
parser.add_argument('--widths',default='393,475,667,932,1440')
args=parser.parse_args()
args.output.mkdir(parents=True,exist_ok=True)
lib_browser.DOCS=args.docs.resolve()
MAP="window[Object.keys(window).find(k=>/^map_[a-f0-9]/.test(k)&&window[k].fire)]"
records=[]

def save(page,name,**extra):
    data=page.evaluate("""()=>{let a=document.activeElement,r=a.getBoundingClientRect();return {
      width:innerWidth,height:innerHeight,drawer:App.state.sheet.state!=='collapsed',
      mapVisible:getComputedStyle(document.querySelector('.folium-map')).visibility,
      active:{id:a.id,cls:a.className,tag:a.tagName,left:r.left,right:r.right,top:r.top,bottom:r.bottom},
      sort:App.state.sort,
      region:App.state.filters.region===null?'':String(App.state.filters.region)}}""")
    records.append({'name':name,**data,**extra})
    page.screenshot(path=str(args.output/(name+'.png')))
    (args.output/'results.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))

def check(condition,message):
    if not args.baseline:
        assert condition,message

# 3.2.x: focus must not be inside the closed card (#bs-foot/#bs-head/#bs-content).
# 4.0: the same three live in #detail-root / #detail-foot.
def focus_visible(page):
    return page.evaluate("""()=>{let e=document.activeElement,r=e.getBoundingClientRect();return e!==document.body
      && !e.closest('[inert]') && getComputedStyle(e).visibility==='visible'
      && r.width>0 && r.height>0 && r.right>0 && r.left<innerWidth && r.bottom>0 && r.top<innerHeight
      && !e.closest('#detail-root,#detail-foot')}""")

def focus_search(page):
    page.evaluate("() => Overlays.focusSearch()")
    page.wait_for_selector('#ov-sinput')

def open_search(page):
    """3.2.x: type into #ss-input and click the first #ss-local row.
    4.0: the same query, the first restaurant row of #ov-sugs."""
    focus_search(page)
    page.fill('#ov-sinput','寿司')
    page.wait_for_function("() => document.querySelectorAll('#ov-sugs .ov-sug').length > 0")
    page.evaluate("""()=>{const rows=[...document.querySelectorAll('#ov-sugs .ov-sug')];
      const r=rows.find(e=>e.querySelector('.rating'))||rows[0];
      r.dispatchEvent(new MouseEvent('click',{bubbles:true}))}""")
    page.wait_for_function("() => !!App.state.selected.id")
    page.wait_for_selector('.dt-actions [data-act="fav"]',state='attached')
    page.wait_for_timeout(700)

def cache(page):
    return page.evaluate("JSON.parse(localStorage.getItem('omakase_state_cache_v2')||'{}').fav||[]")

def open_marker(page):
    page.evaluate("() => { App.act.closeDetail && App.act.closeDetail();"
                  "        App.set({search:{active:false,query:''}}); }")
    page.evaluate(MAP+'.setView([35.6717,139.764],17,{animate:false})')
    page.wait_for_function("""()=>{window.uxFocusMarker=[...document.querySelectorAll('.leaflet-marker-icon .mp-mk')].find(e=>{
      const r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2,t=document.elementFromPoint(x,y);
      return t&&(t===e||e.contains(t));});return !!window.uxFocusMarker}""")
    page.evaluate("""()=>{window.uxFocusMarker.dispatchEvent(new MouseEvent('click',{bubbles:true}))}""")
    page.wait_for_function("() => !!App.state.selected.id")
    page.wait_for_selector('.dt-actions [data-act="fav"]',state='attached')
    page.wait_for_timeout(700)

def close_detail_click(page, prefer_back=False):
    """3.2.x had one close control (#bs-close / .rst-close) and it both closed
    the card and returned you to your source. 4.0 splits the two deliberately
    (NAV-01): [data-ct="back"] is the labelled source return — it puts the
    panel back on the list you came from and restores scroll + focus — while
    [data-ct="close-detail"] is "done, show me the map" and collapses the panel.
    Pass prefer_back=True when the assertion is about returning to the source."""
    order = ('[data-ct="back"]', '[data-ct="close-detail"]') if prefer_back \
            else ('[data-ct="close-detail"]', '[data-ct="back"]')
    for sel in order:
        loc = page.locator(sel)
        if loc.count() and loc.first.is_visible():
            loc.first.click()
            return
    # No back control on screen (the wide detail column only shows one when
    # there is history behind it). The source return is still reachable — it
    # is what Escape and the browser's Back button do — so take that route
    # rather than the × , which is NAV-01's "show me the map" and is not
    # supposed to restore anything.
    if prefer_back:
        page.evaluate("() => App.nav.back()")
    else:
        page.evaluate("() => App.act.closeDetail()")

def closed_detail(page,name):
    page.wait_for_timeout(700)
    before=cache(page)
    save(page,name)
    check(focus_visible(page),'closed detail kept invisible focus: '+json.dumps(page.evaluate(
      """()=>{const a=document.activeElement,r=a.getBoundingClientRect();return {tag:a.tagName,cls:a.className,
        id:a.id,w:r.width,h:r.height,vis:getComputedStyle(a).visibility,
        overlay:App.state.overlay.kind, leftPref:App.state.columns.userLeftPreference,
        inert:!!(a.closest&&a.closest('[inert]')),parking:!!(a.closest&&a.closest('#parking')),
        searchActive:App.state.search.active,sheet:App.state.sheet.state,mode:App.state.layout.mode}}"""),ensure_ascii=False))
    # 3.2.x asserted the four closed nodes carried `inert`. 4.0 parks the
    # detail root in #parking (hidden) at narrow and marks #col-detail inert at
    # mid/wide; either way nothing inside it may be reachable.
    check(page.evaluate("""()=>{const r=document.getElementById('detail-root');
      const f=document.getElementById('detail-foot');
      const dead=el=>!el||el.closest('[inert]')||el.closest('#parking')||!el.getClientRects().length;
      return dead(r)&&dead(f)}"""),'closed detail nodes remain interactive')
    page.keyboard.press('Space');page.wait_for_timeout(200)
    check(cache(page)==before,'Space altered closed restaurant')
    for _ in range(3):
        page.keyboard.press('Tab')
        check(page.evaluate("!document.activeElement.closest('#detail-root,#detail-foot')"),
              'Tab reached closed detail')

with lib_browser.serve_docs(8985 if args.browser=='webkit' else 8986) as base,sync_playwright() as p:
    browser=getattr(p,args.browser).launch()
    for w,h in [(393,852),(475,751),(667,375),(932,704),(1440,900)]:
        if w not in {int(n) for n in args.widths.split(',')}: continue
        context=browser.new_context(viewport={'width':w,'height':h},is_mobile=w<1000,has_touch=w<1000,service_workers='block')
        page=context.new_page();page.set_default_timeout(12000)
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.route('https://**/*',lambda r:r.abort())
        page.route('**nominatim.openstreetmap.org/**',lambda r:r.fulfill(status=200,content_type='application/json',body=json.dumps([
            {'place_id':1,'display_name':'UX Tokyo Station','name':'UX Tokyo Station','lat':'35.6812','lon':'139.7671','type':'station','addresstype':'station'}])))
        page.add_init_script("""localStorage.setItem('tabelog.lang','zh-CN');localStorage.setItem('tabelog.seenIntro','1');
          if(!sessionStorage.getItem('ux-seeded')){sessionStorage.setItem('ux-seeded','1');localStorage.setItem('tabelog.bookmarks',JSON.stringify([
          {id:'bm-ux-pin',name:'UX pin',emoji:'📍',lat:35.6812,lon:139.7671,category:'bookmark'},
          {id:'bm-ux-sight',name:'UX sight',emoji:'⛩️',lat:35.6815,lon:139.7672,category:'attraction'},
          {id:'list:ux',category:'meta',kind:'list',name:'UX places',emoji:'📍'},
          {id:'member:ux-pin',category:'meta',kind:'member',list:'list:ux',ref:'bm-ux-pin'},
          {id:'member:ux-sight',category:'meta',kind:'member',list:'list:ux',ref:'bm-ux-sight'}]));}
          navigator.geolocation.getCurrentPosition=(ok,bad)=>setTimeout(()=>ok({coords:{latitude:35.6812,longitude:139.7671,accuracy:10},timestamp:Date.now()}),30);""")
        page.on('dialog',lambda d:d.accept())
        lib_browser.boot(page,base)

        def tab(name):
            page.evaluate("""(t)=>{App.act.closeDetail&&App.act.closeDetail();App.act.setTab(t);
              if(App.state.layout.mode==='narrow')App.act.setSheet('expanded')}""",name)
            page.wait_for_timeout(500)

        def show_map():
            page.evaluate("""()=>{App.act.closeDetail&&App.act.closeDetail();
              App.act.setSheet('collapsed')}""")
            page.wait_for_timeout(400)

        # A saved pin and a saved landmark, opened from the Saved tab grouped
        # by collection. 3.2.x: #fv-group + .fv-row[data-fav-ref]. 4.0: the
        # [data-groupby] select and .ls-row[data-id].
        for kind in ['pin','sight']:
            tab('saved')
            page.evaluate("()=>App.set({saved:{groupBy:'list',openGroups:null}})")
            page.wait_for_timeout(500)
            ref='bm-ux-'+kind
            page.wait_for_selector('.ls-row[data-id="%s"]'%ref)
            page.evaluate("""(ref)=>{document.querySelector('.ls-row[data-id="'+ref+'"] .ls-open')
              .dispatchEvent(new MouseEvent('click',{bubbles:true}))}""",ref)
            page.wait_for_timeout(1500)
            save(page,f'{args.browser}-{w}-saved-{kind}',center=page.evaluate(MAP+'.getCenter()'))
            check(page.locator('.folium-map').is_visible(),'saved place leaves map hidden')
            check(page.evaluate(MAP+'.getBounds().contains([35.6812,139.7671])'),'saved place is outside the visible map bounds')
            tab('saved')
            check(page.evaluate("()=>App.state.saved.groupBy")=='list','collection view changed')
            check(page.locator('.ls-row[data-id="%s"]'%ref).count()==1,'collection member disappeared')

        # Place search: the stubbed Nominatim row moves the map, and the place
        # can be saved as a pin. 3.2.x: #ss-api row -> #ss-add-bm -> #bm-modal.
        show_map()
        focus_search(page)
        page.fill('#ov-sinput','UX Tokyo Station')
        page.wait_for_function("""()=>[...document.querySelectorAll('#ov-sugs .ov-sug')]
          .some(e=>/UX Tokyo Station/.test(e.textContent))""")
        page.evaluate("""()=>{[...document.querySelectorAll('#ov-sugs .ov-sug')]
          .find(e=>/UX Tokyo Station/.test(e.textContent))
          .dispatchEvent(new MouseEvent('click',{bubbles:true}))}""")
        page.wait_for_timeout(1500)
        save(page,f'{args.browser}-{w}-place-search',center=page.evaluate(MAP+'.getCenter()'))
        check(page.locator('.folium-map').is_visible(),'place search leaves map hidden')
        check(page.evaluate(MAP+'.getBounds().contains([35.6812,139.7671])'),'searched place is outside the visible map bounds')
        page.evaluate("""()=>{const k=Object.keys(window).find(x=>x.startsWith('map_'));
          window[k].fire('contextmenu',{latlng:L.latLng(35.6812,139.7671)})}""")
        page.wait_for_selector('[data-ov="place-new"][data-cat="bookmark"]')
        page.locator('[data-ov="place-new"][data-cat="bookmark"]').click()
        page.wait_for_selector('#ov-name')
        page.fill('#ov-name','UX searched place')
        page.locator('[data-ov="save-bookmark"]').click()
        page.wait_for_timeout(600)
        check(page.evaluate("JSON.parse(localStorage.getItem('tabelog.bookmarks')).some(b=>b.name_src==='UX searched place')"),'search popup save failed')

        # 附近: the undo toast, the exact planning context, the pressed chip,
        # and a reload that keeps the plan without claiming a sort it is not
        # using.
        tab('filters')
        page.evaluate("()=>App.act.applyFilters({region:25})")
        page.wait_for_timeout(400)
        tab('results')
        page.evaluate("()=>App.set({sort:'price'})")
        page.wait_for_timeout(400)
        page.evaluate(MAP+'.setView([35.01,135.77],11,{animate:false})')
        original=page.evaluate(MAP+'.getCenter()')
        show_map()
        page.locator('[data-fab="locate"]').click()
        page.wait_for_function("()=>App.state.sort==='distance'",timeout=20000)
        # 3.2.x looked for a 撤销 button inside .sync-toast; 4.0's toast is state.
        check(page.evaluate("()=>!!(App.state.toast&&App.state.toast.action&&/撤销/.test(App.state.toast.action.label))"),
              'nearby switch has no undo toast')
        planned=page.evaluate("JSON.parse(localStorage.getItem('tabelog.listView')).planningContext")
        check(planned=={'region':25,'sort':'price','center':[original['lat'],original['lng']],'zoom':11},'planning context does not match original view')
        if w<750:
            show_map()
            check(page.locator('.ov-chips [data-ov="nearby"]').get_attribute('aria-pressed')=='true','nearby chip not pressed')
        for sort in ['rating','price','distance']:
            page.evaluate("(s)=>App.set({sort:s})",sort)
            page.wait_for_timeout(250)
            save(page,f'{args.browser}-{w}-nearby-{sort}')
            check(page.evaluate("()=>App.state.sort")==sort,'sort did not change')
        page.evaluate("()=>App.set({sort:'rating'})")
        page.wait_for_timeout(300)
        page.reload();lib_browser.wait_ready(page);page.wait_for_timeout(700)
        check(page.evaluate("JSON.parse(localStorage.getItem('tabelog.listView')).planningContext")==planned,'planning coordinates changed on reload')
        check(page.evaluate("()=>App.state.sort")!='distance','reload falsely claims distance sorting')
        save(page,f'{args.browser}-{w}-nearby-reload-rating')
        check(page.locator('[data-ov="restore-plan"]').first.is_visible(),'return plan action lost')
        page.locator('[data-ov="restore-plan"]').first.click()
        # The restore flies the map back; WebKit finishes that animation later
        # than Chromium, so wait for the view rather than for a fixed delay.
        try:
            page.wait_for_function('()=>'+MAP+'.getZoom()===11',timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(400)
        check(page.evaluate("()=>App.state.filters.region")==25 and page.evaluate("()=>App.state.sort")=='price','planning region/sort lost')
        check(page.evaluate(MAP+'.getZoom()')==11,'planning zoom lost')
        error_px=page.evaluate('c=>'+MAP+'.project('+MAP+'.getCenter(),11).distanceTo('+MAP+'.project(c,11))',original)
        check(error_px<=1,'planning map moved by more than one rendered pixel')

        # The signed-out sync hint must never cover the filter panel's footer.
        tab('filters')
        page.evaluate("()=>App.act.applyFilters({region:null})")
        page.wait_for_timeout(400)
        hint=page.locator('#notice-root .ov-notice')
        if w<750 and not args.baseline and hint.count() and hint.first.is_visible():
            hb=hint.first.bounding_box()
            foot=page.locator('#filters-foot .ft-see')
            if foot.count() and foot.is_visible():
                cb=foot.bounding_box()
                check(hb['y']+hb['height']<=cb['y'] or cb['y']+cb['height']<=hb['y'],
                      'hint visually covers the filter footer')
        save(page,f'{args.browser}-{w}-filter-hint')
        tab('results')

        if w in [393,1440]:
            # A closed detail is inert, whichever control closed it.
            for action in ['fav','gmaps','back']:
                open_search(page)
                if action=='back':
                    close_detail_click(page)
                else:
                    sel='.dt-actions [data-act="fav"]' if action=='fav' else '.dt-actions .dt-gmaps'
                    page.locator(sel).first.focus()
                    page.keyboard.press('Escape')
                page.wait_for_function("()=>!App.state.selected.id")
                closed_detail(page,f'{args.browser}-{w}-close-focus-{action}')
            for action,method in [('fav','escape'),('gmaps','mouse'),('back','history')]:
                open_marker(page)
                if method=='escape':
                    page.locator('.dt-actions [data-act="fav"]').first.focus();page.keyboard.press('Escape')
                elif method=='mouse':
                    close_detail_click(page)
                else:
                    page.evaluate('history.back()')
                page.wait_for_function("()=>!App.state.selected.id")
                closed_detail(page,f'{args.browser}-{w}-marker-close-{method}')

            # Focus returns to the row the detail was opened from.
            tab('results');page.wait_for_selector('#list-root .ls-row[data-id]')
            row_id=page.evaluate("""()=>{const sc=Containers.scroller('list');sc.scrollTop=400;
              return null}""")
            page.wait_for_timeout(600)
            row_id=page.evaluate("""()=>{const sc=Containers.scroller('list'),vr=sc.getBoundingClientRect();
              const rows=[...document.querySelectorAll('#list-root .ls-row[data-id]')];
              const el=rows.find(e=>{const r=e.getBoundingClientRect();
                return r.top>=vr.top-1&&r.bottom<=vr.bottom+1})||rows[0];
              el.querySelector('.ls-open').dispatchEvent(new MouseEvent('click',{bubbles:true}));
              return el.getAttribute('data-id')}""")
            page.wait_for_function("()=>!!App.state.selected.id");page.wait_for_timeout(800)
            # 3.2.x starred it here (#bs-foot .ff-fav-btn) so the Saved tab
            # below has a row. 4.0's Save opens the collection picker and
            # 默认收藏夹 is the "just save it" row (DATA-03).
            if not page.evaluate("(id)=>App.state.user.fav.has(id)",row_id):
                page.locator('.dt-actions [data-act="fav"]').first.click()
                page.wait_for_selector('[data-ov="member-default"]')
                page.locator('[data-ov="member-default"]').click()
                page.wait_for_timeout(500)
                page.evaluate("()=>App.act.closeOverlay('done')")
                page.wait_for_timeout(400)
            close_detail_click(page, prefer_back=True)
            page.wait_for_timeout(1400)
            check(page.evaluate("""(id)=>{const a=document.activeElement;
              const row=a&&a.closest&&a.closest('.ls-row');
              return !!row&&row.getAttribute('data-id')===id}""",row_id),
              'result focus did not return to selected row: '+json.dumps(page.evaluate(
                """(id)=>{const a=document.activeElement;
                  return {active:a.tagName+'.'+a.className,id:a.id,
                    rowPainted:!!document.querySelector('#list-root .ls-row[data-id=\"'+id+'\"]'),
                    listVisible:document.getElementById('list-root').offsetParent!==null,
                    leftPref:App.state.columns.userLeftPreference,
                    autoCollapsed:App.state.columns.autoCollapsed,
                    sel:App.state.selected.id}}""",row_id),ensure_ascii=False))

            # ... and to the saved row when the browser Back button is used.
            tab('saved')
            page.evaluate("()=>App.set({saved:{groupBy:'city',openGroups:null}})")
            page.wait_for_timeout(500)
            page.wait_for_selector('#list-root .ls-row[data-id] .ls-open')
            saved_ref=page.evaluate("""()=>{const el=document.querySelector('#list-root .ls-row[data-id]');
              if(!el) return null;
              el.querySelector('.ls-open').dispatchEvent(new MouseEvent('click',{bubbles:true}));
              return el.getAttribute('data-id')}""")
            assert saved_ref, 'no saved row to open'
            page.wait_for_function("()=>!!App.state.selected.id");page.wait_for_timeout(800)
            page.evaluate('history.back()');page.wait_for_timeout(1200)
            save(page,f'{args.browser}-{w}-source-return',expectedRef=saved_ref,
                 activeRef=page.evaluate("""()=>{const a=document.activeElement;
                   const row=a&&a.closest&&a.closest('.ls-row');
                   return row&&row.getAttribute('data-id')}"""))
            check(page.evaluate("""(id)=>{const a=document.activeElement;
              const row=a&&a.closest&&a.closest('.ls-row');
              return !!row&&row.getAttribute('data-id')===id}""",saved_ref),
              'saved history back lost source focus')

        if w==1440 and not args.baseline:
            # A rotation across the mid/wide boundary must not lose the card's
            # identity or leave a closed card interactive.
            open_search(page)
            page.evaluate("window.uxSameDetail=document.getElementById('detail-root')")
            page.set_viewport_size({'width':475,'height':751});page.wait_for_timeout(600)
            close_detail_click(page)
            page.wait_for_function("()=>!App.state.selected.id")
            closed_detail(page,f'{args.browser}-rotation-closed')
            page.set_viewport_size({'width':1440,'height':900});page.wait_for_timeout(600)
            check(page.evaluate("()=>window.uxSameDetail===document.getElementById('detail-root')"),
                  'closed moved detail state lost')
            open_search(page)
            before=page.evaluate("()=>App.state.user.fav.size")
            page.locator('.dt-actions [data-act="fav"]').first.click()
            page.wait_for_selector('[data-ov="member-default"]')
            page.locator('[data-ov="member-default"]').click()
            page.wait_for_timeout(600)
            check(page.evaluate("()=>App.state.user.fav.size")!=before,'reopened dock remains inert')
            page.evaluate("()=>App.act.closeOverlay('done')")
            close_detail_click(page)
        check(not errors,errors)
        records.append({'name':f'{args.browser}-{w}-assertions','pass':not args.baseline,'baseline':args.baseline,'pageErrors':errors})
        (args.output/'results.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
        print(args.browser,w,'RECORDED' if args.baseline else 'PASS',flush=True)
        context.close()
    browser.close()
