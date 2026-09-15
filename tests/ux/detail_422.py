"""Results/detail hierarchy, one-exit controls, motion and focus."""
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
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
lib_browser.DOCS = ROOT / 'docs'
records = []


def check(value, label):
    assert value, label


with lib_browser.serve_docs(8994) as base, sync_playwright() as p:
    browser = getattr(p, args.browser).launch()
    for name, width, height, sw, sh, dpr in [
        ('phone375', 375, 812, 375, 812, 3),
        ('cover416', 416, 657, 416, 657, 3),
        ('cover475', 475, 751, 475, 751, 2.625),
    ]:
        context = browser.new_context(viewport={'width': width, 'height': height},
            screen={'width': sw, 'height': sh}, device_scale_factor=dpr,
            has_touch=True, is_mobile=True, service_workers='block')
        page = context.new_page(); errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.route('https://**/*', lambda route: route.abort())
        lib_browser.seed_local_storage(page, {'tabelog.lang':'zh-CN','tabelog.seenIntro':'1'})
        lib_browser.boot(page, base)
        page.evaluate("""() => {
          window.routeFrames={forward:[],back:[]};window.routeStyles={forward:[],back:[]};window.routeSettled='';
          function trace(direction){
            const ghost=document.querySelector('[data-route-ghost="'+direction+'"]'),sheet=document.querySelector('#sheet');
            const style=()=>routeStyles[direction].push({ghostTransform:ghost&&ghost.style.transform,
              sheetTransform:sheet.style.transform,ghostTransition:ghost&&ghost.style.transition,
              sheetTransition:sheet.style.transition});
            if(ghost){const observer=new MutationObserver(style);observer.observe(ghost,{attributes:true,attributeFilter:['style']});
              observer.observe(sheet,{attributes:true,attributeFilter:['style']});style();}
            function frame(){const ghost=document.querySelector('[data-route-ghost="'+direction+'"]'),sheet=document.querySelector('#sheet');
              if(!ghost)return;const g=ghost.getBoundingClientRect(),s=sheet.getBoundingClientRect();
              routeFrames[direction].push({ghostX:g.x,sheetX:s.x,ghostW:g.width,sheetW:s.width});
              requestAnimationFrame(frame);}
            requestAnimationFrame(frame);
          }
          App.on('detail:open',()=>trace('forward'));
          App.on('detail:close',p=>{if(p&&p.selected&&p.selected.origin==='results')trace('back')});
          App.on('detail:route-settled',p=>{routeSettled=p.direction});
        }""")
        page.evaluate("App.act.applyFilters({ratingMin:4.0});App.act.setTab('results');App.act.setSheet('browse')")
        page.wait_for_timeout(450)
        motion_info = page.evaluate("""() => ({reduced:App.motion.reduced,micro:App.motion.micro,
          media:matchMedia('(prefers-reduced-motion: reduce)').matches,
          css:getComputedStyle(document.documentElement).getPropertyValue('--m-micro')})""")
        row = page.locator('#list-root .ls-row[data-id]:visible').first
        ref = row.get_attribute('data-id')
        row.locator('.ls-open').click()
        page.wait_for_function('routeSettled==="forward"')
        forward = page.evaluate('routeFrames.forward')
        forward_styles = page.evaluate('routeStyles.forward')
        check(any(s['ghostTransform'].startswith('translateX(-100') and s['sheetTransform'].startswith('translateX(0')
                  and '160ms' in s['ghostTransition'] and '160ms' in s['sheetTransition'] for s in forward_styles),
              (name, 'forward transition contract', motion_info, forward_styles))
        if args.browser == 'chromium':
            check(any(f['ghostX'] < -1 and f['sheetX'] > 1 for f in forward),
                  (name, 'forward painted direction', motion_info, forward))
        page.wait_for_selector('#detail-root .dt-title-row')
        tools = page.locator('#detail-root .dt-title-row .dt-tools')
        check(tools.locator('[data-ct="back"]').count() == 1, (name, 'one back'))
        check(tools.locator('[data-ct="close-detail"]').count() == 0, (name, 'close remains'))
        check(tools.locator(':scope > *').last.get_attribute('data-ct') == 'back', (name, 'back not rightmost'))
        check('返回结果' in tools.locator('[data-ct="back"]').get_attribute('aria-label'), (name, 'back label'))
        page.evaluate("routeSettled=''")
        tools.locator('[data-ct="back"]').click()
        page.wait_for_function('routeSettled==="back"')
        backward = page.evaluate('routeFrames.back')
        backward_styles = page.evaluate('routeStyles.back')
        check(any(s['ghostTransform'].startswith('translateX(100') and s['sheetTransform'].startswith('translateX(0')
                  and '160ms' in s['ghostTransition'] and '160ms' in s['sheetTransition'] for s in backward_styles),
              (name, 'back transition contract', backward_styles))
        if args.browser == 'chromium':
            check(any(f['ghostX'] > 1 and f['sheetX'] < -1 for f in backward),
                  (name, 'back painted direction', backward))
        page.wait_for_function('!App.state.selected.id && App.state.sheet.tab==="results" && App.state.sheet.state==="browse" && App.state.filters.ratingMin===4.0')
        page.wait_for_timeout(250)
        restored = page.evaluate("""ref=>{const a=document.activeElement,r=a&&a.closest('.ls-row');
          return {ref:r&&r.dataset.id,visible:!!(a&&a.getClientRects().length),ghosts:document.querySelectorAll('[data-route-ghost]').length};}""", ref)
        check(restored['ref'] == ref and restored['visible'] and restored['ghosts'] == 0, (name, 'focus restore', restored))
        records.append({'case': name, 'motion': motion_info, 'forwardFrames': forward,
            'forwardStyles': forward_styles, 'backFrames': backward, 'backStyles': backward_styles,
            'restored': restored, 'errors': errors})
        page.screenshot(path=str(args.output / f'{args.browser}-{name}-restored.png'))
        check(not errors, (name, errors))
        context.close()

    # 4.2.6: column layouts use the same one-exit rule. A source return
    # replaces Close on the Fold inner screen and desktop; a map-opened card
    # still has Close because it has no source list to return to.
    for name, width, height, screen, dpr, mobile in [
        ('inner932', 932, 704, {'width': 932, 'height': 704}, 2.625, True),
        ('desktop1440', 1440, 900, {'width': 1440, 'height': 900}, 1, False),
    ]:
        context = browser.new_context(viewport={'width': width, 'height': height}, screen=screen,
            device_scale_factor=dpr, has_touch=mobile, is_mobile=mobile, service_workers='block')
        page = context.new_page(); errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.route('https://**/*', lambda route: route.abort())
        lib_browser.seed_local_storage(page, {'tabelog.lang':'zh-CN','tabelog.seenIntro':'1'})
        lib_browser.boot(page, base)
        page.evaluate("App.act.setTab('results')")
        page.wait_for_selector('#list-root .ls-row[data-id]:visible')
        page.locator('#list-root .ls-row[data-id]:visible .ls-open').first.click()
        page.wait_for_function("() => App.state.selected.origin==='results'")
        page.wait_for_timeout(300)
        head = page.locator('#detail-root .dt-title-row .dt-tools') if width < 1100 else page.locator('#col-detail-head')
        check(head.locator('[data-ct="back"]').count() == 1, (name, 'result detail lost back'))
        check(head.locator('[data-ct="close-detail"]').count() == 0, (name, 'result detail kept close'))
        page.screenshot(path=str(args.output / f'{args.browser}-{name}-result-one-exit.png'))
        head.locator('[data-ct="back"]').click()
        page.wait_for_function("() => !App.state.selected.id && App.state.sheet.tab==='results'")

        page.evaluate("() => App.act.openDetail(Data.restaurants[0].id,'map')")
        page.wait_for_function("() => App.state.selected.origin==='map'")
        page.wait_for_timeout(300)
        head = page.locator('#detail-root .dt-title-row .dt-tools') if width < 1100 else page.locator('#col-detail-head')
        check(head.locator('[data-ct="back"]').count() == 0, (name, 'map detail invented back'))
        check(head.locator('[data-ct="close-detail"]').count() == 1, (name, 'map detail lost close'))
        records.append({'case':name+'-one-exit','resultBack':1,'resultClose':0,'mapBack':0,'mapClose':1,'errors':errors})
        page.screenshot(path=str(args.output / f'{args.browser}-{name}-map-close.png'))
        check(not errors, (name, errors))
        context.close()

    # Reduced motion switches state immediately and creates no visual copy.
    context = browser.new_context(viewport={'width': 375, 'height': 812}, reduced_motion='reduce',
        has_touch=True, is_mobile=True, service_workers='block')
    page = context.new_page(); page.route('https://**/*', lambda route: route.abort())
    lib_browser.seed_local_storage(page, {'tabelog.lang':'zh-CN','tabelog.seenIntro':'1'})
    lib_browser.boot(page, base)
    page.evaluate("App.act.setTab('results');App.act.setSheet('browse')")
    page.wait_for_timeout(250)
    page.locator('#list-root .ls-row[data-id]:visible .ls-open').first.click()
    page.wait_for_selector('#detail-root .dt-title-row')
    check(page.locator('[data-route-ghost]').count() == 0, 'reduced motion created a route ghost')
    check(page.locator('[data-ct="close-detail"]').count() == 0, 'reduced motion kept result close')
    records.append({'case':'reduced-motion','ghosts':0})
    context.close(); browser.close()

(args.output/'results.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
print(f'{args.browser}: {len(records)} mobile result/detail routes passed')
