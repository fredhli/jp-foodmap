"""UX regression checks against a generated page, with isolated browser state.

Usage: .venv-wsl/bin/python tests/ux/run.py --baseline --output <directory>
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))
import lib_browser
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument('--baseline', action='store_true')
ap.add_argument('--output', type=Path, required=True)
ap.add_argument('--docs', type=Path, default=ROOT / 'docs')
ap.add_argument('--browser', choices=['webkit','chromium'], default='webkit')
ap.add_argument('--widths', help='Optional comma-separated widths from the regression matrix')
args = ap.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
lib_browser.DOCS = args.docs.resolve()
records = []
MAP = "window[Object.keys(window).find(k=>/^map_[a-f0-9]/.test(k)&&window[k].fire)]"

def record(page, name, extra=None):
    page.wait_for_timeout(250)
    data = page.evaluate("""() => ({region:document.getElementById('ff-region').value,
      sort:document.getElementById('wb-sort')?.value,
      drawer:document.body.classList.contains('wb-fav-open'),
      width:innerWidth, height:innerHeight, scrollWidth:document.documentElement.scrollWidth,
      modals:['fl','imp'].map(k=>{let e=document.getElementById(k+'-modal'),r=e.getBoundingClientRect();
        return {id:k,top:r.top,bottom:r.bottom,height:r.height}})})""")
    records.append({'name':name, **data, **(extra or {})})
    page.screenshot(path=str(args.output / (name+'.png')))

def keyboard(page, height):
    page.evaluate("""h=>{Object.defineProperty(visualViewport,'height',{configurable:true,value:h});
      visualViewport.dispatchEvent(new Event('resize'));let e=document.createElement('div');e.id='test-keyboard';
      e.style.cssText='position:fixed;z-index:999999;left:0;right:0;bottom:0;top:'+h+'px;background:#c9ccd1';
      e.textContent='Synthetic keyboard';document.body.append(e)}""",height)

with lib_browser.serve_docs(8976) as base, sync_playwright() as p:
    browser=getattr(p,args.browser).launch()
    sizes = [(393,852),(667,375)] if args.baseline else [(320,568),(393,852),(475,751),(591,689),(667,375),(852,393),(932,704),(1440,900)]
    if args.widths:
        sizes=[s for s in sizes if s[0] in {int(n) for n in args.widths.split(',')}]
    for w,h in sizes:
        context=browser.new_context(viewport={'width':w,'height':h},is_mobile=True,has_touch=True,service_workers='block')
        page=context.new_page()
        page.set_default_timeout(5000)
        page.route('https://**/*',lambda r:r.abort())
        page.add_init_script("localStorage.setItem('tabelog.lang','zh-CN');localStorage.setItem('tabelog.seenIntro','1')")
        if not args.baseline:
            page.add_init_script("""navigator.geolocation.getCurrentPosition=(ok,bad)=>setTimeout(()=>window.uxDeny
              ?bad({code:1,message:'Synthetic permission denial'})
              :ok({coords:{latitude:35.6812,longitude:139.7671,accuracy:10},timestamp:Date.now()}),30)""")
            page.on('dialog',lambda d:d.accept())
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        lib_browser.boot(page,base)
        if not args.baseline:
            prefix=f'{args.browser}-{w}'
            page.wait_for_timeout(300)
            record(page,prefix+'-home')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), 'page overflows'
            def tab(name):
                if w<750: lib_browser.phone_tab(page,name)
                else: page.locator('#wb-tab-'+name).click()
            tab('filter')
            page.locator('#ff-region').select_option('25')
            page.wait_for_timeout(500)
            page.locator('#ux-filter-results').click()
            page.wait_for_selector('#wb-list .wb-row')
            assert page.locator('#ff-region').input_value()=='25'
            page.locator('#wb-sort').select_option('price')
            page.wait_for_timeout(500)
            page.locator('#wb-list').evaluate('(e)=>e.scrollTop=680')
            page.wait_for_timeout(200)
            record(page,prefix+'-results')
            row_id=page.evaluate("""()=>{let l=document.getElementById('wb-list').getBoundingClientRect();return [...document.querySelectorAll('#wb-list .wb-row')].find(e=>{let r=e.getBoundingClientRect();return r.top+r.height/2>=l.top&&r.top+r.height/2<=l.bottom}).id}""")
            old_scroll=page.locator('#wb-list').evaluate('(e)=>e.scrollTop')
            page.evaluate("""()=>{document.addEventListener('pointerdown',()=>{window.uxClickScroll=document.getElementById('wb-list').scrollTop},{once:true})}""")
            page.locator('#'+row_id).click()
            old_scroll=page.evaluate('window.uxClickScroll')
            page.wait_for_selector('#bs-sheet.bs-open',state='attached')
            page.wait_for_timeout(400)
            page.locator('#ux-detail-actions .ff-fav-btn').click(trial=True)
            page.locator('#ux-detail-actions .rst-gmaps').click(trial=True)
            page.locator('#ux-detail-actions .ff-fav-btn').click()
            record(page,prefix+'-detail')
            assert page.locator('#ux-detail-back').inner_text()=='返回结果'
            page.locator('#ux-detail-back').click()
            page.wait_for_timeout(250)
            assert abs(page.locator('#wb-list').evaluate('(e)=>e.scrollTop')-old_scroll)<=1, 'result scroll lost'
            tab('fav')
            page.locator('.fv-row').first.click()
            page.wait_for_selector('#bs-sheet.bs-open',state='attached')
            page.wait_for_timeout(200)
            assert page.locator('#ux-detail-back').inner_text()=='返回我的收藏'
            page.locator('#ux-detail-back').click()
            assert page.locator('#wb-tab-fav').get_attribute('aria-selected')=='true'
            if w in (393,1440):
                tab('results')
                page.evaluate(MAP+'.setView([35.01,135.77],11,{animate:false})')
                page.wait_for_timeout(200)
                before=page.evaluate(MAP+'.getCenter()')
                if w<750: tab('map')   # M-3.2-02: the nearby entry is under the open drawer
                page.locator('#ux-nearby').click()
                page.wait_for_function("document.getElementById('wb-sort').value==='distance'")
                assert page.locator('#ff-region').input_value()==''
                planned=page.evaluate("JSON.parse(localStorage.getItem('tabelog.listView')).planningContext")
                assert planned=={'region':25,'sort':'price','center':[before['lat'],before['lng']],'zoom':11}, planned
                page.wait_for_timeout(300)
                assert page.locator('.ux-distance').count()>0
                record(page,prefix+'-nearby')
                page.reload()
                lib_browser.wait_ready(page)
                page.wait_for_timeout(500)
                assert page.locator('#ux-restore-plan').is_visible(), 'planning route lost on reload'
                assert page.evaluate("JSON.parse(localStorage.getItem('tabelog.listView')).planningContext")==planned, 'planning coordinates changed on reload'
                page.locator('#ux-restore-plan').click()
                page.wait_for_timeout(300)
                assert page.locator('#ff-region').input_value()=='25'
                assert page.locator('#wb-sort').input_value()=='price'
                center=page.evaluate(MAP+'.getCenter()')
                assert page.evaluate(MAP+'.getZoom()')==11
                # Leaflet rounds its pixel origin and invalidates the cached geographic center on resize.
                error_px=page.evaluate('c=>'+MAP+'.project('+MAP+'.getCenter(),11).distanceTo('+MAP+'.project(c,11))',before)
                assert error_px<=1, (center,before,error_px)
                record(page,prefix+'-restored',{'planningContext':planned,'restoredCenter':center,'projectDistancePx':error_px})
                page.evaluate('window.uxDeny=true')
                if w<750: tab('map')   # uxRestorePlanning re-opened the drawer
                page.locator('#ux-nearby').click()
                page.wait_for_function("document.getElementById('ux-context-note').textContent.includes('未能取得位置')")
                assert page.locator('#ff-region').input_value()=='25'
                assert page.locator('#wb-sort').input_value()=='price'
                record(page,prefix+'-denied')
            if w in (320,393):
                tab('fav')
                page.locator('#fv-new').click()
                page.locator('#fl-name').fill('UX keyboard test')
                keyboard(page,220)
                page.locator('#fl-modal .fl-save').click(trial=True)
                record(page,prefix+'-keyboard',{'synthetic':True})
                assert page.locator('#fl-modal').bounding_box()['y']>=0
                page.locator('#fl-modal .fl-save').click()
                page.evaluate("delete visualViewport.height;visualViewport.dispatchEvent(new Event('resize'));document.getElementById('test-keyboard').remove()")
                if w<750: tab('map')
            if w in (667,852):
                if w<750: tab('map')
                payload={'favorites':['https://tabelog.com/tokyo/A1301/A130103/13015251/']}
                page.locator('#ssm-import-file').set_input_files({'name':'ux.json','mimeType':'application/json','buffer':json.dumps(payload).encode()})
                page.wait_for_selector('#imp-modal.imp-open')
                page.evaluate("""()=>{let a=[...document.querySelectorAll('#imp-modal,#imp-modal *')].map(e=>[e,parseFloat(getComputedStyle(e).fontSize)]);a.forEach(([e,s])=>e.style.setProperty('font-size',s*2+'px','important'))}""")
                page.locator('#imp-modal .imp-close').click(trial=True)
                page.locator('#imp-modal .imp-confirm').click(trial=True)
                box=page.locator('#imp-modal').bounding_box()
                assert box['y']>=0 and box['y']+box['height']<=h
                record(page,prefix+'-import-text200',{'synthetic':True})
                page.locator('#imp-modal .imp-cancel').click()
            assert not errors, errors
            records.append({'name':prefix+'-assertions','pass':True,'pageerrors':errors})
            (args.output/'results.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
            print(prefix,'PASS',flush=True)
            context.close()
            continue
        if w==393:
            page.locator('#wb-seg [data-ux-tab="filter"]').evaluate('(e)=>e.click()')
            page.locator('#ff-region').select_option('25')
            page.locator('#wb-tab-results').click()
            page.evaluate(MAP+".fire('locationfound',{latlng:L.latLng(35.6812,139.7671),accuracy:10})")
            record(page,'baseline-nearby-region')
            page.locator('#wb-list .wb-row').first.click()
            page.wait_for_selector('#bs-sheet.bs-open')
            page.locator('.rst-close').first.click()
            record(page,'baseline-detail-return')
            page.evaluate("window.__wbFavDrawer.open();window.__wbSetTab('fav')")
            page.locator('#fv-new').click()
            page.locator('#fl-name').fill('UX keyboard test')
            keyboard(page,220)
            try:
                page.locator('#fl-modal .fl-save').click(trial=True,timeout=1000)
                accessible=True
            except Exception:
                accessible=False
            record(page,'baseline-collection-keyboard',{'saveAccessible':accessible,'synthetic':True})
        else:
            payload={'favorites':['https://tabelog.com/tokyo/A1301/A130103/13015251/']}
            page.locator('#ssm-import-file').set_input_files({'name':'ux.json','mimeType':'application/json','buffer':json.dumps(payload).encode()})
            page.wait_for_selector('#imp-modal.imp-open')
            page.evaluate("""()=>{let a=[...document.querySelectorAll('#imp-modal,#imp-modal *')].map(e=>[e,parseFloat(getComputedStyle(e).fontSize)]);a.forEach(([e,s])=>e.style.setProperty('font-size',s*2+'px','important'))}""")
            record(page,'baseline-import-text200',{'synthetic':True})
        context.close()
    browser.close()
(args.output/'results.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
print(json.dumps(records,ensure_ascii=False,indent=2))
