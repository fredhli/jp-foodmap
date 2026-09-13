"""Fold cover identity, touch controls, and scoped row geometry regression."""
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
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--baseline', action='store_true')
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
lib_browser.DOCS = args.docs.resolve()
records, failures = [], []
rows = json.loads((args.docs / 'data/restaurants.json').read_text())
seed = {'tabelog.lang': 'zh-CN', 'tabelog.seenIntro': '1',
        'omakase_state_cache_v2': json.dumps({'fav': [r['detail_url'] for r in rows[:20]], 'black': [], 'dirty': False})}
cases = [('cover751',475,751,475,751,True,True), ('cover650',475,650,475,751,True,True),
         ('cover600',475,600,475,751,True,True), ('wrong-screen',475,650,480,800,True,False),
         ('mouse',475,751,475,751,False,False), ('phone375',375,812,375,812,True,False),
         ('phone393',393,852,393,852,True,False), ('phone402',402,874,402,874,True,False),
         ('phone430',430,932,430,932,True,False), ('split591',591,689,932,704,True,False),
         ('split688',688,704,932,704,True,False), ('inner704',704,932,704,932,True,False),
         ('inner932',932,704,932,704,True,False), ('desktop',1440,900,1920,1080,False,False)]
probe = """() => {
 const box=e=>{if(!e)return null;const r=e.getBoundingClientRect(),c=getComputedStyle(e);return {x:r.x,y:r.y,w:r.width,h:r.height,min:c.minHeight,pad:c.paddingTop}};
 const q=s=>box(document.querySelector(s));
 return {profile:!!App.state.layout.foldCover,mode:App.state.layout.mode,
  search:q('.ov-capsule,.ov-topfield'),region:q('#search-root [data-kind="regionPicker"]'),
  nearby:q('#search-root [data-ov="nearby"]'),avatar:q('#search-root .ov-avatar'),
  row:q('.ls-row .ls-open'),booking:q('.ls-cover-booking'),foot:q('#list-foot'),
  overflow:document.documentElement.scrollWidth>innerWidth,metrics:ListMod.metrics()};
}"""

def check(ok, message):
    if not ok:
        failures.append(message)
        (args.output/'results.json').write_text(json.dumps({'records':records,'failures':failures},indent=2))
        if not args.baseline:
            raise AssertionError(message)

with lib_browser.serve_docs(8992) as base, sync_playwright() as p:
    browser = p.chromium.launch()
    for name,w,h,sw,sh,touch,expected in cases:
        context = browser.new_context(viewport={'width':w,'height':h},screen={'width':sw,'height':sh},has_touch=touch,is_mobile=touch,service_workers='block')
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.route('https://**/*', lambda r: r.abort())
        lib_browser.seed_local_storage(page, seed)
        lib_browser.boot(page, base)
        page.evaluate("App.act.setTab('results'); App.act.setSheet('expanded')")
        page.wait_for_timeout(650)
        result = page.evaluate(probe)
        check(result['profile'] == expected, name + ': profile identity')
        check(not result['overflow'], name + ': page overflow')
        if result['row']:
            check(result['row']['min'] == ('84px' if expected else '96px' if w<750 else '76px'),name+': row min')
            if w<750: check(result['row']['pad'] == ('6px' if expected else '12px'), name+': row padding')
        if expected and result['profile']:
            buttons = [result[k] for k in ['search','region','nearby','avatar']]
            check(max(b['y']+b['h']/2 for b in buttons)-min(b['y']+b['h']/2 for b in buttons)<1,name+': header same row')
            check(all(b['w']>=44 and b['h']>=44 for b in buttons),name+': touch bounds')
            check(result['booking'] is not None and result['booking']['h']>=44,name+': summary booking')
            check(result['foot']['h']==0,name+': empty booking footer')
            page.locator('[data-ov="search-activate"]').tap()
            page.wait_for_timeout(350)
            check(page.locator('#ov-sinput').is_visible(),name+': full search')
            page.evaluate("App.set({search:{active:false}})")
            page.wait_for_timeout(350)
            page.evaluate("window.coverBudgets=new Set(App.state.filters.budgets);App.act.applyFilters({bookableOnly:true,budgets:new Set()})")
            page.wait_for_timeout(350)
            booking=page.locator('.ls-cover-booking')
            check(booking.get_attribute('aria-pressed')=='true' and booking.is_enabled(),name+': active zero results exit')
            booking.tap()
            check(not page.evaluate('App.state.filters.bookableOnly'),name+': booking toggle off')
            page.evaluate("App.act.applyFilters({budgets:window.coverBudgets})")
            for lang,scale in [('en',130),('ja',100),('zh',100)]:
                page.evaluate("([lang,scale])=>{App.i18n.setLang(lang);App.i18n.setFontScale(scale)}",[lang,scale])
                page.wait_for_timeout(300)
                check(not page.evaluate(probe)['overflow'],name+': language/font overflow')
        page.evaluate("App.act.setTab('saved');App.act.setSheet('expanded')")
        page.wait_for_timeout(450)
        saved = page.evaluate(probe)
        check(saved['booking'] is None,name+': no saved booking filter')
        if saved['row'] and w<750:
            check(saved['row']['min']=='96px' and saved['row']['pad']=='12px',name+': saved geometry unchanged')
        check(not errors,name+': page errors '+str(errors))
        records.append({'name':name,'results':result,'saved':saved})
        context.close()
    browser.close()
(args.output/'results.json').write_text(json.dumps({'records':records,'failures':failures},indent=2))
print(f'{len(cases)} profiles; {len(failures)} failures')
