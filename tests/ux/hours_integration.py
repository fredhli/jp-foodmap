"""Offline Google-hours states inside the real generated detail layout."""
import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))
import lib_browser

parser = argparse.ArgumentParser()
parser.add_argument('--preview', type=Path, default=Path('/tmp/tabelog-update-preview'))
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--browser', choices=['chromium','webkit'], default='chromium')
parser.add_argument('--viewport', default=None)
args = parser.parse_args()
assert (args.preview / 'index.html').exists()
args.output.mkdir(parents=True, exist_ok=True)
lib_browser.DOCS = args.preview

FAKE = r"""() => {
  window.hoursPermitCalls = [];
  window.hoursPermitMode = 'ok';
  window.hoursWidgetOutcome = 'ok';
  window.hoursWidgetCount = 0;
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (url, options) => {
    if (String(url) !== 'https://api.jpfoodmap.com/api/places/permit') return originalFetch(url, options);
    const request = JSON.parse(options.body);
    hoursPermitCalls.push({request, credentials:options.credentials, method:options.method});
    const errorCodes = {daily_limit:429, monthly_limit:429, unauthorized:401, unavailable:503};
    const mode = hoursPermitMode;
    const status = errorCodes[mode] || 200;
    return {ok:status===200,status,json:async()=>status===200
      ? {allowed:true,requestId:request.requestId,remainingDaily:19,resetAt:'2026-09-25T00:00:00Z'}
      : {error:mode,resetAt:'2026-09-25T00:00:00Z'}};
  };
  window.google = {maps:{importLibrary:async name => {
    if (name !== 'places') throw Error('unexpected Google library');
    return {};
  }}};
  if (!customElements.get('gmp-place-details')) {
    customElements.define('gmp-place-details', class extends HTMLElement {
      constructor() { super(); hoursWidgetCount++; }
      connectedCallback() {
        if (!this.querySelector('.fake-hours-list')) {
          const list = document.createElement('div');
          list.className = 'fake-hours-list';
          list.style.cssText = 'padding:16px;line-height:1.55;color:#111A43';
          list.innerHTML = ['月 11:30–21:00','火 11:30–21:00','水 11:30–21:00',
            '木 11:30–21:00','金 11:30–21:00','土 12:00–20:00','日 12:00–20:00']
            .map(day => '<div>' + day + '</div>').join('');
          this.appendChild(list);
        }
        const outcome = hoursWidgetOutcome;
        setTimeout(() => this.dispatchEvent(new Event(outcome === 'error' ? 'gmp-error' : 'gmp-load')), 15);
      }
    });
  }
  window.PLACES_UI_CONFIG = {
    apiKey:'test-only-key',permitUrl:'https://api.jpfoodmap.com/api/places/permit'
  };
  App.set({account:{signedIn:true,email:'hours-fixture@example.test'},signedIn:true});
  window.hoursRefs = Data.restaurants.filter(r => r.gpid && r.id.includes('/tokyo/')).slice(0, 12).map(r => r.id);
  window.hoursMissingRef = Data.restaurants.find(r => !r.gpid && r.id.includes('/tokyo/')).id;
  if (hoursRefs.length < 8) throw Error('insufficient matched fixture rows');
}"""


def open_row(page, ref):
    page.evaluate('(id) => App.act.openDetail(id, "results")', ref)
    page.wait_for_function('''(id) => {
      const section=document.querySelector('.dt-hours');
      return App.state.selected.id===id && section &&
        section.getAttribute('data-hours-key')===Hours.key(Data.byId(id),App.state.lang,App.state);
    }''', arg=ref)
    page.evaluate('document.querySelector(".dt-hours").scrollIntoView({block:"center"})')


def loaded(page):
    page.locator('[data-act="hours-open"]').click()
    page.wait_for_function("!!document.querySelector('.dt-hours-source')")
    page.evaluate('document.querySelector(".dt-hours").scrollIntoView({block:"center"})')


with lib_browser.serve_docs(8999) as base, sync_playwright() as p:
    browser = getattr(p, args.browser).launch()
    for name, w, h, mobile, dpr in [
        ('desktop-1440x900', 1440, 900, False, 1),
        ('fold8-outer-475x751', 475, 751, True, 2.625),
        ('fold8-inner-932x704', 932, 704, False, 2),
        ('iphone-402x874', 402, 874, True, 3),
    ]:
        if args.viewport and name != args.viewport: continue
        context = browser.new_context(viewport={'width':w,'height':h},
                                      screen={'width':w,'height':h},
                                      is_mobile=mobile,has_touch=mobile,
                                      device_scale_factor=dpr,service_workers='block',
                                      reduced_motion='no-preference')
        page = context.new_page()
        page.route('https://**/*', lambda route: route.abort())
        lib_browser.seed_local_storage(page, {'tabelog.lang':'zh-CN','tabelog.seenIntro':'1'})
        lib_browser.boot(page, base)
        assert page.evaluate('!!window.Hours && !!window.Detail')
        page.evaluate(FAKE)
        ref = page.evaluate('hoursRefs[0]')
        open_row(page, ref)
        assert page.locator('.dt-summary + .dt-hours + [data-section="address"]').count() == 1
        assert page.locator('[data-act="hours-open"]').count() == 1
        assert page.evaluate('hoursPermitCalls.length') == 0
        page.screenshot(path=str(args.output / f'{name}-initial.png'))
        loaded(page)
        assert page.evaluate('hoursPermitCalls.length') == 1
        assert page.evaluate('hoursWidgetCount') == 1
        assert page.locator('gmp-place-opening-hours').count() == 1
        assert page.locator('gmp-place-attribution').count() == 1
        assert page.evaluate("hoursPermitCalls[0].method==='POST' && hoursPermitCalls[0].credentials==='include' && !!hoursPermitCalls[0].request.requestId")
        check = page.evaluate('''() => {
          const s=document.querySelector('.dt-hours').getBoundingClientRect();
          const g=document.querySelector('gmp-place-details').getBoundingClientRect();
          return {overflow:document.documentElement.scrollWidth>innerWidth,sectionW:s.width,widgetW:g.width};
        }''')
        assert not check['overflow'] and 250 <= check['widgetW'] <= 400, (name, check)
        page.screenshot(path=str(args.output / f'{name}-loaded.png'))
        if mobile:
            page.evaluate("""() => {
              window.hoursGhosts = [];
              new MutationObserver(records => records.forEach(record => {
                record.addedNodes.forEach(node => {
                  if (node.nodeType === 1 && node.hasAttribute('data-route-ghost'))
                    hoursGhosts.push({widget:!!node.querySelector('gmp-place-details')});
                });
              })).observe(document.body,{childList:true,subtree:true});
            }""")
            page.locator('#detail-root [data-ct="back"]').click()
            page.wait_for_function('!App.state.selected.id && hoursGhosts.length > 0')
            assert page.evaluate('hoursGhosts.every(ghost => !ghost.widget) && hoursWidgetCount===1'), (name, 'route ghost cloned billed widget')
            open_row(page, ref)
            assert page.evaluate('hoursPermitCalls.length===1 && hoursWidgetCount===1'), (name, 'back/reopen recreated a billed widget')
        if name.startswith('desktop'):
            page.evaluate('window.hoursWidgetRef=document.querySelector("gmp-place-details")')
            page.evaluate('App.set({fontScale:115})')
            page.wait_for_timeout(50)
            assert page.evaluate('document.querySelector("gmp-place-details")===hoursWidgetRef')
            assert page.evaluate('hoursPermitCalls.length===1 && hoursWidgetCount===1')
            page.evaluate('I18N.setLang("en")')
            page.wait_for_function("document.querySelector('.dt-hours h2').textContent==='Opening hours'")
            assert page.evaluate('document.querySelector("gmp-place-details")===hoursWidgetRef')
            assert page.evaluate('hoursPermitCalls.length===1 && hoursWidgetCount===1')
            open_row(page, page.evaluate('hoursRefs[1]'))
            open_row(page, ref)
            assert page.evaluate('document.querySelector("gmp-place-details")===hoursWidgetRef')
            assert page.evaluate('hoursPermitCalls.length===1 && hoursWidgetCount===1')
        context.close()

    context = browser.new_context(viewport={'width':402,'height':874},screen={'width':402,'height':874},
                                  is_mobile=True,has_touch=True,service_workers='block')
    page = context.new_page()
    page.route('https://**/*', lambda route: route.abort())
    lib_browser.seed_local_storage(page, {'tabelog.lang':'zh-CN','tabelog.seenIntro':'1'})
    lib_browser.boot(page, base)
    page.evaluate(FAKE)
    open_row(page, page.evaluate('hoursMissingRef'))
    assert page.locator('[data-act="hours-open"]').count() == 0
    assert page.locator('.dt-hours a[href*="tabelog.com"]').count() == 1
    page.screenshot(path=str(args.output / 'missing-id.png'))

    page.evaluate('App.set({account:{signedIn:false,email:""},signedIn:false})')
    open_row(page, page.evaluate('hoursRefs[2]'))
    assert page.locator('[data-act="hours-login"]').count() == 1
    assert page.evaluate('hoursPermitCalls.length') == 0
    page.screenshot(path=str(args.output / 'unauthenticated.png'))
    page.evaluate('App.set({account:{signedIn:true,email:"hours-fixture@example.test"},signedIn:true})')

    page.evaluate("""() => {
      localStorage.setItem('tabelog.auth',JSON.stringify({sub:'fixture',exp:Date.now()+86400000,email:'hours-fixture@example.test'}));
      localStorage.setItem('omakase_state_cache_v2','{"fav":["fixture-fav"],"black":["fixture-black"]}');
      localStorage.setItem('tabelog.bookmarks','[{"id":"fixture-bookmark"}]');
      window.hoursStorageBefore = {
        saved:localStorage.getItem('omakase_state_cache_v2'),
        bookmarks:localStorage.getItem('tabelog.bookmarks')
      };
      hoursPermitMode='unauthorized';
    }""")
    open_row(page, page.evaluate('hoursRefs[8]'))
    page.locator('[data-act="hours-open"]').click()
    page.wait_for_function('App.state.account.signedIn === false && App.state.overlay.kind === "account"')
    assert page.evaluate("""() => localStorage.getItem('tabelog.auth') === null &&
      localStorage.getItem('omakase_state_cache_v2') === hoursStorageBefore.saved &&
      localStorage.getItem('tabelog.bookmarks') === hoursStorageBefore.bookmarks""")
    assert page.locator('[data-act="hours-login"]').count() == 1
    page.evaluate('hoursPermitMode="ok";App.act.closeOverlay("done");App.set({account:{signedIn:true,email:"hours-fixture@example.test"},signedIn:true})')

    for index, mode, fragment in [
        (3,'daily_limit','今天的查看次数已用完'),
        (4,'monthly_limit','本月的营业时间查看额度已用完'),
        (5,'unavailable','营业时间服务暂不可用'),
    ]:
        page.evaluate('(mode) => {hoursPermitMode=mode}', mode)
        open_row(page, page.evaluate(f'hoursRefs[{index}]'))
        before = page.evaluate('hoursWidgetCount')
        page.locator('[data-act="hours-open"]').click()
        page.wait_for_function('(text) => document.querySelector(".dt-hours-state").textContent.includes(text)', arg=fragment)
        assert page.evaluate('hoursWidgetCount') == before
        assert page.locator('.dt-hours a[href*="tabelog.com"]').count() == 1
        assert page.locator('.dt-hours a[href*="google.com/maps"]').count() == 1
        page.screenshot(path=str(args.output / f'{mode}.png'))

    page.evaluate('hoursPermitMode="ok";hoursWidgetOutcome="error"')
    open_row(page, page.evaluate('hoursRefs[6]'))
    page.locator('[data-act="hours-open"]').click()
    page.wait_for_function('!!document.querySelector("[data-act=hours-retry]")')
    assert page.locator('.dt-hours a[href*="google.com/maps"]').count() == 1
    page.screenshot(path=str(args.output / 'google-error.png'))
    before = page.evaluate('hoursWidgetCount')
    page.evaluate('hoursWidgetOutcome="ok"')
    page.locator('[data-act="hours-retry"]').click()
    page.wait_for_function('!!document.querySelector(".dt-hours-source")')
    assert page.evaluate('hoursWidgetCount') == before+1

    page.evaluate('PLACES_UI_CONFIG.apiKey=""')
    open_row(page, page.evaluate('hoursRefs[7]'))
    assert page.locator('[data-act="hours-open"]').count() == 0
    assert page.locator('.dt-hours a[href*="tabelog.com"]').count() == 1
    assert page.locator('.dt-hours a[href*="google.com/maps"]').count() == 1
    page.screenshot(path=str(args.output / 'unconfigured.png'))
    context.close()
    browser.close()
print('hours integration: real detail layout, four widths and fallback states passed')
