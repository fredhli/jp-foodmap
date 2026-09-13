"""Nearby controls and cover layout; all external requests are blocked."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))
import lib_browser
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('--docs', type=Path, default=ROOT / 'docs')
parser.add_argument('--audit', action='store_true')
parser.add_argument('--width', type=int)
parser.add_argument('--language')
parser.add_argument('--scale', type=int)
parser.add_argument('--layout-only', action='store_true')
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
lib_browser.DOCS = args.docs.resolve()
records = []
probe = '''() => {
 const box=e=>{if(!e)return null;const r=e.getBoundingClientRect(),c=getComputedStyle(e);return {x:r.x,y:r.y,w:r.width,h:r.height,border:c.borderTopWidth,text:e.textContent,aria:e.getAttribute('aria-label'),scroll:e.scrollWidth,client:e.clientWidth}};
 const q=s=>box(document.querySelector(s));
 const b=document.querySelector('.ov-cover-region'),parts=b?[...b.children].filter(e=>getComputedStyle(e).display!=='none').map(e=>e.getBoundingClientRect()):[];
 return {search:q('.ov-capsule'),region:q('.ov-cover-region'),nearby:q('.ov-cover-nearby'),avatar:q('.ov-avatar'),
 label:q('.ov-cover-near-label'),toolbar:q('.ls-cover-toolbar'),booking:q('.ls-cover-booking'),sort:q('.ls-sort'),multi:q('.ls-multi'),
 fabs:[...document.querySelectorAll('#fab-root .mp-fab')].filter(e=>e.getClientRects().length&&getComputedStyle(e).display!=='none').map(box),mapRect:App.state.layout.mapRect,
 regionCenter:parts.length?((Math.min(...parts.map(r=>r.left))+Math.max(...parts.map(r=>r.right)))/2-(b.getBoundingClientRect().left+b.getBoundingClientRect().width/2)):0,
 overflow:document.documentElement.scrollWidth>innerWidth,mode:Data.resultScope(),count:Data.M(App.state).length};
}'''
seed = {'tabelog.lang':'zh-CN','tabelog.seenIntro':'1'}
with lib_browser.serve_docs(8993) as base, sync_playwright() as p:
    browser=p.chromium.launch()
    for w,h,dpr in [(357,563,3.5),(416,657,3),(475,751,2.625)]:
        if args.width and w != args.width:
            continue
        context=browser.new_context(viewport={'width':w,'height':h},screen={'width':w,'height':h},device_scale_factor=dpr,has_touch=True,is_mobile=True,service_workers='block')
        page=context.new_page()
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.route('https://**/*',lambda r:r.abort())
        lib_browser.seed_local_storage(page,seed)
        lib_browser.boot(page,base)
        page.evaluate("App.act.setTab('results');App.act.setSheet('expanded');App.act.applyFilters({region:13});App.set({notices:{oov:{dismissedForever:true,visible:false}}});MapMod.flyTo([35.68,139.76],13)")
        for lang in ([args.language] if args.language else ['zh','tw','en','ja']):
            page.evaluate('''lang=>{const longest=Data.config.REGIONS.map((r,i)=>({i,name:Data.regionName(i,lang)})).sort((a,b)=>b.name.length-a.name.length)[0];App.act.applyFilters({region:longest.i});}''',lang)
            for scale in ([args.scale] if args.scale else [100,130,200]):
                page.evaluate("([lang,scale])=>{App.i18n.setLang(lang);document.documentElement.style.fontSize=(16*scale/100)+'px';document.documentElement.setAttribute('data-fs',String(scale));App.set({fontScale:scale});App.layout.schedule()}",[lang,scale])
                page.wait_for_timeout(300)
                data=page.evaluate(probe)
                records.append({'w':w,'lang':lang,'scale':scale,'layout':data})
                (args.output/'results.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
                assert not data['overflow'],(w,lang,scale,'overflow')
                buttons=[data[k] for k in ['search','region','nearby','avatar']]
                assert all(b['w']>=43.9 and b['h']>=43.9 and b['x']>=0 and b['x']+b['w']<=w+1 for b in buttons),(w,lang,scale,buttons)
                centers=[b['y']+b['h']/2 for b in buttons]
                assert max(centers)-min(centers)<1,(w,lang,scale,'same row')
                assert data['region']['border']=='0px' and data['nearby']['border']=='0px'
                assert abs(data['regionCenter'])<1,(w,lang,scale,'region center',data['regionCenter'])
                assert data['label']['w']>0 and data['label']['h']>0 and data['label']['scroll']<=data['label']['client']+1,(w,lang,scale,'near label clipped')
                if scale<=130 and not args.audit:
                    assert data['toolbar']['h']<=44.1,(w,lang,scale,'toolbar',data['toolbar'])
                assert data['sort']['aria'] and data['multi']['aria']
                if not args.audit:
                    rect=data['mapRect']
                    for fab in data['fabs']:
                        assert fab['y']>=rect['y']-1 and fab['y']+fab['h']<=rect['y']+rect['h']+1,(w,lang,scale,'fab vertical containment',fab,rect)
                        assert fab['x']>=rect['x']-1 and fab['x']+fab['w']<=rect['x']+rect['w']+1,(w,lang,scale,'fab horizontal containment',fab,rect)
                page.screenshot(path=str(args.output/f'cover-{w}-{lang}-{scale}.png'))
        if args.layout_only:
            context.close()
            continue
        page.evaluate("App.i18n.setLang('zh');document.documentElement.style.fontSize='16px';App.i18n.setFontScale(100)")
        page.locator('[data-act="sort"]').click()
        page.wait_for_timeout(100)
        assert page.locator('[data-ov="sort"]').count()==5
        page.locator('[data-ov="sort"]').nth(1).click()
        page.locator('[data-act="multi"]').click()
        assert page.locator('.ls-bulk').is_visible()
        page.locator('[data-act="cancel-multi"]').click()
        page.evaluate("App.act.applyFilters({bookableOnly:true,budgets:new Set()})")
        page.wait_for_timeout(150)
        page.locator('.ls-cover-booking').click()
        assert not page.evaluate('App.state.filters.bookableOnly')
        page.evaluate('''() => {
          App.act.resetFilters();
          window.uiFix=Data.restaurants.find(r=>r.lat&&r.lon);
          window.geoCallback=null;
          Object.defineProperty(navigator,'geolocation',{value:{getCurrentPosition:(success)=>{window.geoCallback=success}},configurable:true});
        }''')
        page.evaluate("App.emit('map:locate',{status:'error',message:'test-location-error'})")
        page.wait_for_timeout(100)
        assert page.locator('[data-notice="geo"]').is_visible()
        page.locator('[data-ov="nearby"]').click()
        page.wait_for_timeout(100)
        assert page.locator('[data-notice="geo"]').count()==0
        assert page.evaluate('App.state.nearby.pending')
        page.locator('[data-ov="nearby"]').click()
        assert not page.evaluate('App.state.nearby.pending || App.state.nearby.active')
        page.locator('[data-ov="nearby"]').click()
        page.evaluate("geoCallback({coords:{latitude:uiFix.lat,longitude:uiFix.lon,accuracy:10},timestamp:Date.now()})")
        page.wait_for_timeout(300)
        assert page.locator('[data-ov="nearby"]').get_attribute('aria-pressed')=='true'
        assert page.locator('[data-ov="restore-plan"]').count()==0
        page.screenshot(path=str(args.output/f'cover-{w}-nearby.png'))
        page.locator('[data-scope-picker]').click()
        page.wait_for_timeout(200)
        assert page.locator('[data-ov="radius"]').count()==4
        assert page.locator('[data-ov="region"]').count()==0
        if not args.audit:
            page.keyboard.press('Tab')
            assert page.evaluate('document.querySelector("#ov-pop").contains(document.activeElement)'), 'radius Tab escaped dialog'
            page.keyboard.press('Shift+Tab')
        page.keyboard.press('Home')
        page.keyboard.press('Enter')
        page.wait_for_timeout(300)
        assert page.evaluate('App.state.nearby.radiusM')==200
        assert page.evaluate('document.activeElement.hasAttribute("data-scope-picker")')
        page.evaluate("App.act.setTab('filters')")
        page.wait_for_timeout(200)
        assert page.locator('.ft-region').get_attribute('data-radius-picker') is not None
        page.locator('.ft-region').click()
        page.wait_for_timeout(150)
        page.keyboard.press('End')
        page.keyboard.press('Enter')
        page.wait_for_timeout(200)
        assert page.evaluate('App.state.nearby.radiusM')==2000
        assert page.locator('.ft-region').evaluate('(e)=>e===document.activeElement')
        page.evaluate("App.act.setTab('results');App.act.applyFilters({budgets:new Set()})")
        page.wait_for_timeout(200)
        assert '2 km' in page.locator('.ls-empty').text_content()
        page.screenshot(path=str(args.output/f'cover-{w}-nearby-empty.png'))
        page.locator('[data-act="reset-filters"]').click()
        assert page.evaluate('App.state.nearby.active && App.state.nearby.radiusM===2000')
        page.evaluate("App.set({nearby:{fix:null,needsLocation:true,revision:App.state.nearby.revision+1}});App.act.setTab('results')")
        page.wait_for_timeout(200)
        assert '重新定位' in page.locator('.ls-head').text_content()
        assert page.locator('[data-act="nearby-locate"]').is_visible()
        page.screenshot(path=str(args.output/f'cover-{w}-paused.png'))
        page.locator('[data-ov="nearby"]').click()
        page.wait_for_timeout(200)
        assert not page.evaluate('App.state.nearby.active')
        assert page.locator('[data-radius-picker]').count()==0
        assert not errors,errors
        context.close()
    browser.close()
print(f'{len(records)} cover language/scale layouts passed' + ('' if args.layout_only else ' plus nearby UI journeys'))
