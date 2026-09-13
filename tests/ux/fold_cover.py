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
# Alternate DPR cases are physical equivalents, not readings from the user's device.
# Rounded case: audit_outputs/sol-09-06/android-2026-09-06/shell-rework/foldcover/natural-home.json.
cases = [('cover751',475,751,475,751,2.625,True,True),
         ('cover650',475,650,475,751,2.625,True,True),
         ('cover600',475,600,475,751,2.625,True,True),
         ('cover-rounded',475,650,476,752,2.625,True,True),
         ('cover-dpr3',416,600,416,657,3,True,True),
         ('cover-dpr2.5',499,650,499,789,2.5,True,True),
         ('cover-dpr2.75',454,650,454,717,2.75,True,True),
         ('cover-dpr3.5',357,500,357,563,3.5,True,True),
         ('old-false-positive',475,650,475,751,2,True,False),
         ('wrong-screen',475,650,480,800,2.625,True,False),
         ('mouse',475,751,475,751,2.625,False,False),
         ('phone375',375,812,375,812,3,True,False),
         ('phone393',393,852,393,852,3,True,False),
         ('phone402',402,874,402,874,3,True,False),
         ('phone430',430,932,430,932,3,True,False),
         ('split591',591,689,932,704,2.625,True,False),
         ('split688',688,704,932,704,2.625,True,False),
         ('inner704',704,932,704,932,2.625,True,False),
         ('inner932',932,704,932,704,2.625,True,False),
         ('not-full-width',416,650,475,751,2.625,True,False),
         ('landscape',751,475,751,475,2.625,True,False),
         ('desktop',1440,900,1920,1080,1,False,False)]
probe = """() => {
 const box=e=>{if(!e)return null;const r=e.getBoundingClientRect(),c=getComputedStyle(e);return {x:r.x,y:r.y,w:r.width,h:r.height,min:c.minHeight,pad:c.paddingTop}};
 const q=s=>box(document.querySelector(s));
 return {profile:!!App.state.layout.foldCover,mode:App.state.layout.mode,
  search:q('.ov-capsule,.ov-topfield'),region:q('#search-root [data-kind="regionPicker"]'),
  nearby:q('#search-root [data-ov="nearby"]'),avatar:q('#search-root .ov-avatar'),
  row:q('.ls-row .ls-open'),booking:q('.ls-cover-booking'),foot:q('#list-foot'),
  overflow:document.documentElement.scrollWidth>innerWidth,metrics:ListMod.metrics(),
  identity:App.layout.foldCoverInfo ? App.layout.foldCoverInfo(App.state.layout.W) : null};
}"""

def check(ok, message):
    if not ok:
        failures.append(message)
        (args.output/'results.json').write_text(json.dumps({'records':records,'failures':failures},indent=2))
        if not args.baseline:
            raise AssertionError(message)

with lib_browser.serve_docs(8992) as base, sync_playwright() as p:
    browser = p.chromium.launch()
    for name,w,h,sw,sh,dpr,touch,expected in cases:
        context = browser.new_context(viewport={'width':w,'height':h},screen={'width':sw,'height':sh},device_scale_factor=dpr,has_touch=touch,is_mobile=touch,service_workers='block')
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.route('https://**/*', lambda r: r.abort())
        lib_browser.seed_local_storage(page, seed)
        lib_browser.boot(page, base)
        if name == 'cover751' and not args.baseline:
            invalid = page.evaluate('''() => {
              const old = Object.getOwnPropertyDescriptor(window, 'devicePixelRatio');
              const results = [];
              for (const value of [0, -1, NaN, Infinity, undefined, '2.625']) {
                Object.defineProperty(window, 'devicePixelRatio', {value, configurable:true});
                results.push(App.layout.foldCoverInfo(475));
              }
              if (old) Object.defineProperty(window, 'devicePixelRatio', old);
              else delete window.devicePixelRatio;
              for (const value of [0, -1, NaN, Infinity, undefined, '475']) results.push(App.layout.foldCoverInfo(value));
              for (const field of ['width', 'height']) {
                const original = Object.getOwnPropertyDescriptor(screen, field);
                for (const value of [0, -1, NaN, Infinity, undefined, '475']) {
                  Object.defineProperty(screen, field, {value, configurable:true});
                  results.push(App.layout.foldCoverInfo(475));
                }
                if (original) Object.defineProperty(screen, field, original);
                else delete screen[field];
              }
              return results;
            }''')
            check(all(not item['matched'] and item['reason'] == '屏幕或缩放读数无效' for item in invalid), 'invalid identity readings')
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
            keyboard = context.new_cdp_session(page)
            keyboard.send('Emulation.setDeviceMetricsOverride', {'width':w,'height':350,
                'screenWidth':sw,'screenHeight':sh,'deviceScaleFactor':dpr,'mobile':True})
            page.wait_for_timeout(350)
            check(page.evaluate('App.state.layout.foldCover'),name+': keyboard height preserves identity')
            keyboard.send('Emulation.setDeviceMetricsOverride', {'width':w,'height':h,
                'screenWidth':sw,'screenHeight':sh,'deviceScaleFactor':dpr,'mobile':True})
            for lang,scale in [('en',130),('ja',130),('ja',100),('zh',100)]:
                page.evaluate("([lang,scale])=>{App.i18n.setLang(lang);App.i18n.setFontScale(scale)}",[lang,scale])
                page.wait_for_timeout(300)
                localized = page.evaluate(probe)
                check(not localized['overflow'],name+': language/font overflow')
                buttons = [localized[k] for k in ['search','region','nearby','avatar']]
                check(all(b['w']>=44 and b['h']>=44 and b['x']>=0 and b['x']+b['w']<=w+1 for b in buttons), name+': localized touch bounds')
                check(max(b['y']+b['h']/2 for b in buttons)-min(b['y']+b['h']/2 for b in buttons)<1,name+': localized header row')
        page.evaluate("App.act.setTab('saved');App.act.setSheet('expanded')")
        page.wait_for_timeout(450)
        saved = page.evaluate(probe)
        check(saved['booking'] is None,name+': no saved booking filter')
        if saved['row'] and w<750:
            check(saved['row']['min']=='96px' and saved['row']['pad']=='12px',name+': saved geometry unchanged')
        if name in ('cover751', 'cover-dpr3.5', 'old-false-positive') and not args.baseline:
            check(page.locator('.ov-layout-diagnostics').count()==0,name+': no ordinary page diagnostics')
            page.evaluate('''() => {
              Object.defineProperty(navigator, 'clipboard', {value:{writeText:text=>{window.copiedDiagnostic=text;return Promise.resolve()}},configurable:true});
              App.act.openOverlay('help', {topic:'about'});
            }''')
            diagnostic = page.locator('.ov-layout-diagnostics')
            diagnostic.wait_for()
            check(not diagnostic.evaluate('(e)=>e.open'),name+': diagnostics collapsed by default')
            check(diagnostic.locator('pre').text_content()=='',name+': readings lazy')
            diagnostic.locator('summary').click()
            check(diagnostic.evaluate('(e)=>e.open'),name+': diagnostic click opens')
            diagnostic.locator('summary').press('Enter')
            check(not diagnostic.evaluate('(e)=>e.open'),name+': diagnostic keyboard closes')
            diagnostic.locator('summary').press('Space')
            check(diagnostic.evaluate('(e)=>e.open'),name+': diagnostic keyboard opens')
            reading = json.loads(diagnostic.locator('pre').text_content())
            check(set(reading)=={'appVersion','screen','availableScreen','layoutViewport','innerViewport','visualViewport','devicePixelRatio','physicalScreen','touch','foldCover'},name+': diagnostic whitelist')
            check(reading['devicePixelRatio']==dpr and reading['screen']=={'width':sw,'height':sh},name+': diagnostic dimensions')
            check(reading['foldCover']['matched']==expected and reading['foldCover']['enabled']==expected and reading['foldCover']['attributeApplied']==expected and bool(reading['foldCover']['reason']),name+': diagnostic match/applied/reason')
            check(reading['appVersion']==page.evaluate('App.state.buildMeta.appVersion'),name+': diagnostic version')
            diagnostic.locator('[data-ov="copy-layout-diagnostics"]').click()
            copied = page.evaluate('window.copiedDiagnostic')
            check(json.loads(copied)==reading,name+': copied readings match')
            check(not any(secret in copied for secret in ['http:', 'https:', 'detail_url', 'fav', '@']),name+': diagnostic excludes private fields')
        check(not errors,name+': page errors '+str(errors))
        records.append({'name':name,'results':result,'saved':saved})
        context.close()
    browser.close()
(args.output/'results.json').write_text(json.dumps({'records':records,'failures':failures},indent=2))
print(f'{len(cases)} profiles; {len(failures)} failures')
