"""Regressions for place navigation, closed detail focus and nearby sort copy."""
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
      width:innerWidth,height:innerHeight,drawer:document.body.classList.contains('wb-fav-open'),
      mapVisible:getComputedStyle(document.querySelector('.folium-map')).visibility,
      active:{id:a.id,cls:a.className,tag:a.tagName,left:r.left,right:r.right,top:r.top,bottom:r.bottom},
      sort:document.getElementById('wb-sort')?.value,note:document.getElementById('ux-nearby')?.textContent??'',
      region:document.getElementById('ff-region').value}}""")
    records.append({'name':name,**data,**extra})
    page.screenshot(path=str(args.output/(name+'.png')))
    (args.output/'results.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))

def check(condition,message):
    if not args.baseline:
        assert condition,message

def focus_visible(page):
    return page.evaluate("""()=>{let e=document.activeElement,r=e.getBoundingClientRect();return e!==document.body
      && !e.closest('[inert]') && getComputedStyle(e).visibility==='visible'
      && r.width>0 && r.height>0 && r.right>0 && r.left<innerWidth && r.bottom>0 && r.top<innerHeight
      && !e.closest('#bs-foot,#bs-head,#bs-content')}""")

def open_search(page):
    page.locator('#ss-input').fill('寿司')
    page.wait_for_selector('#ss-local .ss-row:not(.ss-empty)')
    page.locator('#ss-local .ss-row:not(.ss-empty)').first.click()
    page.wait_for_function("document.getElementById('bs-sheet').classList.contains('bs-open')")
    page.wait_for_selector('#bs-foot .ff-fav-btn',state='attached')
    page.wait_for_timeout(650)

def cache(page):
    return page.evaluate("JSON.parse(localStorage.getItem('omakase_state_cache_v2')||'{}').fav||[]")

def open_marker(page):
    page.locator('#ss-input').fill('')
    page.keyboard.press('Escape')
    page.evaluate(MAP+'.setView([35.6717,139.764],17,{animate:false})')
    page.wait_for_function("""()=>{window.uxFocusMarker=Object.values("""+MAP+"""._layers).filter(l=>l._d&&l._icon).map(l=>l._icon).find(e=>{
      const r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2,t=document.elementFromPoint(x,y);
      return t&&(t===e||e.contains(t));});return !!window.uxFocusMarker}""")
    point=page.evaluate("""()=>{let e=window.uxFocusMarker,r=e.getBoundingClientRect();e.focus();return {x:r.left+r.width/2,y:r.top+r.height/2}}""")
    page.mouse.click(point['x'],point['y'])
    page.wait_for_function("document.getElementById('bs-sheet').classList.contains('bs-open')")
    page.wait_for_selector('#bs-foot .ff-fav-btn',state='attached')
    page.wait_for_timeout(650)

def closed_detail(page,name):
    page.wait_for_timeout(600)
    before=cache(page)
    save(page,name)
    check(focus_visible(page),'closed detail kept invisible focus')
    check(page.evaluate("['bs-content','bs-foot','ux-detail-back','bs-grip'].every(id=>document.getElementById(id).inert)"),'closed nodes remain interactive')
    page.keyboard.press('Space');page.wait_for_timeout(150)
    check(cache(page)==before,'Space altered closed restaurant')
    for _ in range(3):
        page.keyboard.press('Tab')
        check(page.evaluate("!document.activeElement.closest('#bs-foot,#bs-head,#bs-content')"),'Tab reached closed detail')
    if not args.baseline:
        page.locator('#ss-input').focus()
        page.locator('#bs-foot .ff-fav-btn').focus()
        check(focus_visible(page),'closed Save accepted focus')
        page.keyboard.press('Escape');page.keyboard.press('Escape')
        page.locator('#ss-input').evaluate('(e)=>e.blur()')

with lib_browser.serve_docs(8985 if args.browser=='webkit' else 8986) as base,sync_playwright() as p:
    browser=getattr(p,args.browser).launch()
    for w,h in [(393,852),(475,751),(667,375),(932,704),(1440,900)]:
        if w not in {int(n) for n in args.widths.split(',')}: continue
        context=browser.new_context(viewport={'width':w,'height':h},is_mobile=w<1000,has_touch=w<1000,service_workers='block')
        page=context.new_page();page.set_default_timeout(10000)
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
            if w<750: lib_browser.phone_tab(page,name)
            else: page.locator('#wb-tab-'+name).click()
        for kind in ['pin','sight']:
            tab('fav');page.locator('#fv-group').select_option('list')
            page.locator('.fv-row[data-fav-ref="bm-ux-'+kind+'"]').click()
            page.wait_for_timeout(1400)
            save(page,f'{args.browser}-{w}-saved-{kind}',center=page.evaluate(MAP+'.getCenter()'))
            check(page.locator('.folium-map').is_visible(),'saved place leaves map hidden')
            check(page.evaluate(MAP+'.getBounds().contains([35.6812,139.7671])'),'saved place is outside the visible map bounds')
            if w<750 and args.baseline: lib_browser.phone_tab(page,'map')
            tab('fav')
            check(page.locator('#fv-group').input_value()=='list','collection view changed')
            check(page.locator('.fv-row[data-fav-ref="bm-ux-'+kind+'"]').count()==1,'collection member disappeared')
        tab('results');page.wait_for_timeout(200)
        if w<750: lib_browser.phone_tab(page,'map')   # M-3.2-02: the search capsule is under the open drawer
        page.locator('#ss-input').fill('UX Tokyo Station')
        page.wait_for_selector('#ss-api .ss-row:not(.ss-empty)')
        page.locator('#ss-api .ss-row:not(.ss-empty)').click()
        page.wait_for_timeout(1400)
        save(page,f'{args.browser}-{w}-place-search',center=page.evaluate(MAP+'.getCenter()'))
        check(page.locator('.folium-map').is_visible(),'place search leaves map hidden')
        check(page.evaluate(MAP+'.getBounds().contains([35.6812,139.7671])'),'searched place is outside the visible map bounds')
        if w<750 and args.baseline: lib_browser.phone_tab(page,'map')
        page.locator('#ss-add-bm').click()
        page.locator('#bm-name').fill('UX searched place')
        page.locator('#bm-modal .bm-save').click()
        check(page.evaluate("JSON.parse(localStorage.getItem('tabelog.bookmarks')).some(b=>b.name_src==='UX searched place')"),'search popup save failed')
        tab('filter');page.locator('#ff-region').select_option('25');page.locator('#ux-filter-results').click()
        page.locator('#wb-sort').select_option('price');page.wait_for_timeout(400)
        page.evaluate(MAP+'.setView([35.01,135.77],11,{animate:false})')
        original=page.evaluate(MAP+'.getCenter()')
        if w<750: lib_browser.phone_tab(page,'map')   # M-3.2-02: the nearby entry is under the open drawer
        page.locator('#fab-locate').click();page.wait_for_function("document.getElementById('wb-sort').value==='distance'")
        check(page.locator('.sync-toast .sync-btn').filter(has_text='撤销').count()==1,'nearby switch has no undo toast')
        planned=page.evaluate("JSON.parse(localStorage.getItem('tabelog.listView')).planningContext")
        check(planned=={'region':25,'sort':'price','center':[original['lat'],original['lng']],'zoom':11},'planning context does not match original view')
        for sort in ['rating','price','distance']:
            if w<750: tab('results')   # M-3.2-05: locating leaves the map up; the sort control lives in the drawer
            page.locator('#wb-sort').select_option(sort);page.wait_for_timeout(150)
            if w<750: lib_browser.phone_tab(page,'map')   # and the chip row is under the drawer
            note=page.locator('#ux-nearby').text_content()
            save(page,f'{args.browser}-{w}-nearby-{sort}')
            check(page.locator('#ux-nearby').is_visible() and page.locator('#ux-nearby').get_attribute('aria-pressed')=='true','nearby chip not pressed')
            check(('按距离' in note)==(sort=='distance'),'nearby sort copy disagrees')
            if sort!='distance':check(('评分' if sort=='rating' else '价位') in note,'current sort absent from copy')
        if w<750: tab('results')
        page.locator('#wb-sort').select_option('rating');page.reload();lib_browser.wait_ready(page)
        check(page.evaluate("JSON.parse(localStorage.getItem('tabelog.listView')).planningContext")==planned,'planning coordinates changed on reload')
        check('按距离' not in page.locator('#ux-nearby').text_content(),'reload falsely claims distance sorting')
        save(page,f'{args.browser}-{w}-nearby-reload-rating')
        check(page.locator('#ux-restore-plan').is_visible(),'return plan action lost')
        page.locator('#ux-restore-plan').click();page.wait_for_timeout(300)
        center=page.evaluate(MAP+'.getCenter()')
        if w<750: tab('results')   # M-3.2-04: restoring does not open the drawer; #wb-sort is built with it
        check(page.locator('#ff-region').input_value()=='25' and page.locator('#wb-sort').input_value()=='price','planning region/sort lost')
        check(page.evaluate(MAP+'.getZoom()')==11,'planning zoom lost')
        error_px=page.evaluate('c=>'+MAP+'.project('+MAP+'.getCenter(),11).distanceTo('+MAP+'.project(c,11))',original)
        check(error_px<=1,'planning map moved by more than one rendered pixel')
        tab('filter');page.locator('#ff-region').select_option('');page.wait_for_timeout(200)
        hint=page.locator('#sync-hint')
        if w<750 and not args.baseline:
            if h<=500:
                check(hint.is_hidden(),'non-urgent hint occupies short filter screen')
            elif hint.is_visible():
                hb=hint.bounding_box();cb=page.locator('#ux-filter-done').bounding_box()
                check(hb['y']+hb['height']<=cb['y'],'hint visually covers filter CTA')
                page.locator('#sync-hint-signin').click(trial=True)
                page.locator('#sync-hint-ok').click(trial=True)
        save(page,f'{args.browser}-{w}-filter-hint')
        page.locator('#ux-filter-results').click()
        if w in [393,1440]:
            if w<750:lib_browser.phone_tab(page,'map')
            for action in ['.ff-fav-btn','.rst-gmaps','back']:
                open_search(page)
                selector=('#bs-close' if w<750 else '#bs-content .rst-close') if action=='back' else '#bs-foot '+action
                page.locator(selector).focus();page.keyboard.press('Escape')
                closed_detail(page,f'{args.browser}-{w}-close-focus-{action.replace(".","")}')
            for action,method in [('.ff-fav-btn','escape'),('.rst-gmaps','mouse'),('back','history')]:
                open_marker(page)
                selector=('#bs-close' if w<750 else '#bs-content .rst-close') if action=='back' else '#bs-foot '+action
                page.locator(selector).focus()
                if method=='escape':page.keyboard.press('Escape')
                elif method=='mouse':page.locator('#bs-close' if w<750 else '#bs-content .rst-close').click()
                else:page.evaluate('history.back()')
                page.wait_for_function("!document.getElementById('bs-sheet').classList.contains('bs-open')")
                closed_detail(page,f'{args.browser}-{w}-marker-close-{method}')
            tab('results');page.wait_for_selector('#wb-list .wb-row')
            page.locator('#wb-list').evaluate('(e)=>e.scrollTop=680');page.wait_for_timeout(250)
            row_id=page.evaluate("""()=>{let l=document.getElementById('wb-list').getBoundingClientRect();return [...document.querySelectorAll('#wb-list .wb-row')].find(e=>{let r=e.getBoundingClientRect();return r.top+r.height/2>l.top&&r.top+r.height/2<l.bottom}).id}""")
            page.evaluate("document.addEventListener('pointerdown',()=>window.uxSourceTop=document.getElementById('wb-list').scrollTop,{once:true})")
            page.locator('#'+row_id).click()
            page.wait_for_function("document.getElementById('bs-sheet').classList.contains('bs-open')");page.wait_for_timeout(650)
            if page.locator('#bs-foot .ff-fav-btn').get_attribute('aria-pressed')!='true':
                page.locator('#bs-foot .ff-fav-btn').click()
            page.locator('#bs-foot .ff-fav-btn').focus();page.keyboard.press('Escape');page.wait_for_timeout(500)
            check(page.evaluate('document.activeElement.id')==row_id,'result focus did not return to selected row')
            check(page.evaluate("Math.abs(document.getElementById('wb-list').scrollTop-window.uxSourceTop)<=1"),'source result scroll lost')
            tab('fav');page.locator('#fv-group').select_option('city');page.wait_for_timeout(250)
            saved=page.locator('.fv-row[data-fav-kind="rst"]').first
            saved_ref=saved.get_attribute('data-fav-ref');saved.click()
            page.wait_for_function("document.getElementById('bs-sheet').classList.contains('bs-open')");page.wait_for_timeout(650)
            page.locator('#bs-foot .rst-gmaps').focus();page.evaluate('history.back()');page.wait_for_timeout(500)
            save(page,f'{args.browser}-{w}-source-return',expectedRef=saved_ref,
                 activeRef=page.evaluate("document.activeElement.getAttribute('data-fav-ref')"),
                 sheetOpen=page.locator('#bs-sheet').get_attribute('class'))
            check(page.evaluate("document.activeElement.getAttribute('data-fav-ref')")==saved_ref,'saved history back lost source focus')
        if w==1440 and not args.baseline:
            open_search(page)
            page.evaluate("window.uxSameDetail=document.getElementById('bs-content')")
            page.set_viewport_size({'width':475,'height':751});page.wait_for_timeout(400)
            page.locator('#bs-foot .ff-fav-btn').focus();page.keyboard.press('Escape')
            closed_detail(page,f'{args.browser}-rotation-closed')
            page.set_viewport_size({'width':1440,'height':900});page.wait_for_timeout(400)
            check(page.evaluate("window.uxSameDetail===document.getElementById('bs-content') && window.uxSameDetail.inert"),'closed moved detail state lost')
            open_search(page)
            saved_before=page.locator('#bs-foot .ff-fav-btn').get_attribute('aria-pressed')
            page.locator('#bs-foot .ff-fav-btn').click()
            check(page.locator('#bs-foot .ff-fav-btn').get_attribute('aria-pressed')!=saved_before,'reopened dock remains inert')
            page.locator('#bs-content .rst-close').click()
        check(not errors,errors)
        records.append({'name':f'{args.browser}-{w}-assertions','pass':not args.baseline,'baseline':args.baseline,'pageErrors':errors})
        (args.output/'results.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
        print(args.browser,w,'RECORDED' if args.baseline else 'PASS',flush=True)
        context.close()
    browser.close()
