"""Offline opening-hours UI Kit states and four viewport snapshots."""
import argparse
import json
from pathlib import Path

from opencc import OpenCC
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / 'src/tabelog/ui'
parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
raw = {k:v for k,v in json.loads((UI / 'i18n/ui-strings.json').read_text()).items() if isinstance(v,dict)}
keys = [k for k in raw if '营业时间' in k or '查看' in k or 'Google 地点' in k or '{time}' in k]
assert all(raw[k].get('en') and raw[k].get('ja') for k in keys)
trad = OpenCC('s2twp')
translations = {
    'zh': {},
    'tw': {k: trad.convert(k) for k in raw},
    'en': {k: v['en'] for k, v in raw.items()},
    'ja': {k: v['ja'] for k, v in raw.items()},
}

BOOT = r"""
({translations}) => {
  window.translations = translations;
  if (!crypto.randomUUID) crypto.randomUUID = () => 'fixture-' + Math.random().toString(36).slice(2);
  window.mockCalls = [];
  window.mockPermitMode = 'ok';
  window.fakeOutcome = 'ok';
  window.fakeCreated = 0;
  window.google = { maps: { importLibrary: async name => {
    if (name !== 'places') throw Error('unexpected library');
    return {};
  } } };
  customElements.define('gmp-place-details', class extends HTMLElement {
    constructor() { super(); window.fakeCreated++; }
    connectedCallback() {
      if (!this.querySelector('.fake-hours-list')) {
        const detail = document.createElement('div');
        detail.className = 'fake-hours-list';
        detail.innerHTML = ['Mon 11:30–21:00','Tue 11:30–21:00','Wed 11:30–21:00',
          'Thu 11:30–21:00','Fri 11:30–21:00','Sat 12:00–20:00','Sun 12:00–20:00']
          .map(day => '<div>' + day + '</div>').join('');
        this.appendChild(detail);
      }
      const event = window.fakeOutcome === 'error' ? 'gmp-error' : 'gmp-load';
      setTimeout(() => this.dispatchEvent(new Event(event)), 10);
    }
  });
  window.PLACES_UI_CONFIG = {
    apiKey: 'fake-browser-key', permitUrl: 'https://api.jpfoodmap.com/api/places/permit'
  };
  window.fetch = async (url, options) => {
    if (url !== PLACES_UI_CONFIG.permitUrl) throw Error('unexpected network request');
    const body = JSON.parse(options.body);
    mockCalls.push({url, method:options.method, credentials:options.credentials, body});
    const mode = mockPermitMode;
    const errors = {daily_limit:429, monthly_limit:429, unauthorized:401, unavailable:503};
    const status = errors[mode] || 200;
    const payload = status === 200
      ? {allowed:true, requestId:body.requestId, remainingDaily:19, resetAt:'2026-09-25T00:00:00Z'}
      : {error:mode, resetAt:'2026-09-25T00:00:00Z'};
    return {ok:status === 200, status, json:async () => payload};
  };
  const state = {lang:'zh', account:{signedIn:true,email:'fixture@example.test'}};
  window.App = {state, set:patch => {
    if (patch.account) Object.assign(state.account,patch.account);
    if (patch.signedIn !== undefined) state.signedIn=patch.signedIn;
    fixture.show(fixture.row);
  }};
  window.fixture = {
    state, row:{id:'https://tabelog.com/tokyo/fixture/',gpid:'fixture-place-1',lat:35.68,lon:139.76},
    ctx:{App, t:(key, args) => {
      let value = (translations[state.lang] || {})[key] || key;
      return value.replace(/\{([^}]+)\}/g, (m,k) => args && args[k] !== undefined ? args[k] : m);
    }, util:{esc:value => String(value == null ? '' : value).replace(/[&<>"']/g,
      c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))},
      act:{openOverlay:kind => {window.lastOverlay=kind},
        invalidateSession:() => {
          state.account.signedIn=false;
          state.account.email='';
          fixture.show(fixture.row);
        }}
    },
    show(row) {
      Hours.park();
      this.row = row;
      const root = document.getElementById('detail-root');
      root.innerHTML = Hours.html(state,row);
      Hours.attach(root,row,state.lang,state);
    }
  };
  Hours.init(fixture.ctx);
  fixture.show(fixture.row);
  document.addEventListener('click', e => {
    const button = e.target.closest('[data-act]');
    if (!button) return;
    const act = button.dataset.act;
    if (act === 'hours-open' || act === 'hours-retry') Hours.activate(fixture.row,state.lang);
    if (act === 'hours-login') fixture.ctx.act.openOverlay('account');
  });
}
"""

with sync_playwright() as p:
    browser = p.chromium.launch()
    for name, width, height, column, mobile in [
        ('desktop-1440', 1440, 900, 384, False),
        ('fold8-outer-475', 475, 751, 475, True),
        ('fold8-inner-932', 932, 704, 340, False),
        ('iphone-402', 402, 874, 402, True),
    ]:
        context = browser.new_context(viewport={'width': width, 'height': height},
                                      is_mobile=mobile, has_touch=mobile,
                                      service_workers='block')
        page = context.new_page()
        page.set_content('<!doctype html><html><head></head><body>'
                         '<main class="fixture-card"><div class="fixture-summary">'
                         '<span>★ 3.72</span><span>¥5,000–¥9,999</span></div>'
                         '<div id="detail-root"></div><div class="fixture-next">位置与座位</div>'
                         '</main></body></html>')
        page.add_style_tag(path=str(UI / 'css/tokens.css'))
        page.add_style_tag(path=str(UI / 'css/base.css'))
        page.add_style_tag(path=str(UI / 'css/detail.css'))
        page.add_style_tag(content=f'''
          body {{ margin:0;background:#F7FAFF;color:#111A43;font-family:var(--font-family); }}
          .fixture-card {{ box-sizing:border-box;width:min({column}px,100%);margin:24px auto;
            padding:20px;background:#fff;border:1px solid var(--separator);border-radius:12px; }}
          .fixture-summary {{ display:flex;justify-content:space-between;gap:8px;padding:12px;
            border-radius:8px;background:var(--surface-subtle); }}
          .fixture-next {{ margin-top:24px;font-weight:600; }}
          .fake-hours-list {{ padding:16px;color:#111A43;line-height:1.6; }}
        ''')
        page.add_script_tag(path=str(UI / 'js/hours.js'))
        page.evaluate(BOOT, {'translations': translations})
        assert page.locator('[data-act="hours-open"]').count() == 1
        assert page.evaluate('mockCalls.length') == 0
        assert page.evaluate('fakeCreated') == 0
        page.screenshot(path=str(args.output / f'{name}-initial.png'), full_page=True)
        page.locator('[data-act="hours-open"]').click()
        page.wait_for_function("document.querySelector('.dt-hours-source') !== null")
        assert page.evaluate('mockCalls.length') == 1
        assert page.evaluate('fakeCreated') == 1
        assert page.locator('gmp-place-details-place-request').get_attribute('place') == 'fixture-place-1'
        assert page.locator('gmp-place-opening-hours').count() == 1
        assert page.locator('gmp-place-attribution').count() == 1
        assert page.evaluate("mockCalls[0].method==='POST' && mockCalls[0].credentials==='include' && !!mockCalls[0].body.requestId")
        page.evaluate('Hours.attach(document.getElementById("detail-root"),fixture.row,fixture.state.lang,fixture.state)')
        assert page.evaluate('mockCalls.length') == 1
        assert page.evaluate('fakeCreated') == 1
        dimensions = page.evaluate('''() => ({overflow:document.documentElement.scrollWidth > innerWidth,
          section:document.querySelector('.dt-hours').getBoundingClientRect().width,
          widget:document.querySelector('gmp-place-details').getBoundingClientRect().width})''')
        assert not dimensions['overflow'], (name, dimensions)
        assert 250 <= dimensions['widget'] <= 400, (name, dimensions)
        page.screenshot(path=str(args.output / f'{name}-loaded.png'), full_page=True)
        context.close()

    page = browser.new_page(viewport={'width': 402, 'height': 874}, service_workers='block')
    page.set_content('<!doctype html><html><body><div id="detail-root"></div></body></html>')
    page.add_script_tag(path=str(UI / 'js/hours.js'))
    page.evaluate(BOOT, {'translations': translations})
    page.evaluate("fixture.show({id:'https://tabelog.com/no-match/',gpid:'',lat:0,lon:0})")
    assert page.get_by_text('未匹配到 Google 地点', exact=False).count() == 1
    assert page.locator('[data-act="hours-open"]').count() == 0
    assert page.evaluate('mockCalls.length') == 0
    page.evaluate("fixture.state.account.signedIn=false; fixture.show({id:'https://tabelog.com/fixture/',gpid:'fixture-place-2'})")
    page.locator('[data-act="hours-login"]').click()
    assert page.evaluate('lastOverlay') == 'account'
    assert page.evaluate('mockCalls.length') == 0
    page.evaluate("fixture.state.account.signedIn=true; mockPermitMode='unauthorized'; fixture.show({id:'https://tabelog.com/auth-expired/',gpid:'fixture-expired'})")
    page.locator('[data-act="hours-open"]').click()
    page.wait_for_function('fixture.state.account.signedIn === false && lastOverlay === "account"')
    assert page.locator('[data-act="hours-login"]').count() == 1
    assert page.evaluate('fakeCreated') == 0
    page.evaluate("fixture.state.account.signedIn=true; mockPermitMode='daily_limit'; fixture.show({id:'https://tabelog.com/limit/',gpid:'fixture-place-3'})")
    page.locator('[data-act="hours-open"]').click()
    page.wait_for_function("document.querySelector('.dt-hours-state').textContent.includes('今天的查看次数已用完')")
    assert page.evaluate('fakeCreated') == 0
    assert page.evaluate('mockCalls.length') == 2
    page.evaluate("mockPermitMode='monthly_limit'; fixture.show({id:'https://tabelog.com/month/',gpid:'fixture-place-4'})")
    page.locator('[data-act="hours-open"]').click()
    page.wait_for_function("document.querySelector('.dt-hours-state').textContent.includes('本月的营业时间查看额度已用完')")
    assert page.evaluate('fakeCreated') == 0
    page.evaluate("mockPermitMode='ok'; fakeOutcome='error'; fixture.show({id:'https://tabelog.com/error/',gpid:'fixture-place-5'})")
    page.locator('[data-act="hours-open"]').click()
    page.wait_for_function("document.querySelector('[data-act=hours-retry]') !== null")
    assert page.evaluate('fakeCreated') == 1
    page.evaluate("fakeOutcome='ok'")
    page.locator('[data-act="hours-retry"]').click()
    page.wait_for_function("document.querySelector('.dt-hours-source') !== null")
    assert page.evaluate('fakeCreated') == 2
    assert page.evaluate('mockCalls.length') == 5
    for lang, expected in [('en','Opening hours'), ('ja','営業時間'), ('tw','營業時間'), ('zh','营业时间')]:
        page.evaluate('(lang) => {fixture.state.lang=lang; fixture.show(fixture.row)}', lang)
        assert page.locator('.dt-hours h2').inner_text() == expected
    edge = browser.new_page(viewport={'width': 402, 'height': 874}, service_workers='block')
    edge.set_content('<!doctype html><html><body><div id="detail-root"></div></body></html>')
    edge.add_script_tag(path=str(UI / 'js/hours.js'))
    edge.evaluate(BOOT, {'translations': translations})
    edge.evaluate("""() => {
      window.nativeTimeout = window.setTimeout;
      window.setTimeout = (fn, delay, ...rest) => nativeTimeout(fn, delay >= 10000 ? 30 : delay, ...rest);
      google.maps.importLibrary = () => new Promise(() => {});
    }""")
    edge.locator('[data-act="hours-open"]').click()
    edge.wait_for_function("document.querySelector('.dt-hours-state').textContent.includes('暂不可用')")
    assert edge.evaluate('mockCalls.length === 1 && fakeCreated === 0')
    edge.evaluate('google.maps.importLibrary = async () => ({})')
    edge.locator('[data-act="hours-retry"]').click()
    edge.wait_for_function("!!document.querySelector('.dt-hours-source')")
    assert edge.evaluate('mockCalls.length === 2 && fakeCreated === 1')

    edge.evaluate("""() => {
      window.originalPermitFetch = window.fetch;
      window.timedPermitCalls = [];
      window.fetch = (url, options) => {
        timedPermitCalls.push(JSON.parse(options.body));
        return new Promise(() => {});
      };
      fixture.show({id:'https://tabelog.com/permit-timeout/',gpid:'fixture-timeout'});
    }""")
    edge.locator('[data-act="hours-open"]').click()
    edge.wait_for_function("document.querySelector('.dt-hours-state').textContent.includes('暂不可用')")
    assert edge.evaluate('timedPermitCalls.length === 2 && timedPermitCalls[0].requestId === timedPermitCalls[1].requestId')
    assert edge.evaluate('fakeCreated === 1')
    edge.evaluate('fetch = originalPermitFetch; setTimeout = nativeTimeout')

    edge.evaluate("""() => {
      window.fetch = async (url, options) => {
        const body = JSON.parse(options.body);
        mockCalls.push({url,method:options.method,credentials:options.credentials,body});
        const status = mockPermitMode === 'daily_limit' ? 429 : 200;
        return {ok:status===200,status,json:async()=>status===200
          ? {allowed:true,requestId:body.requestId,remainingDaily:19}
          : {error:'daily_limit',resetAt:'2026-10-25T01:00:00Z'}};
      };
      mockPermitMode='daily_limit';
      fixture.show({id:'https://tabelog.com/dst-reset/',gpid:'fixture-dst'});
    }""")
    edge.locator('[data-act="hours-open"]').click()
    edge.wait_for_function("document.querySelector('.dt-hours-state').textContent.includes('今天的查看次数已用完')")
    edge.evaluate("""() => {
      Date.now = () => Date.parse('2026-10-25T01:00:01Z');
      mockPermitMode='ok';
      Hours.attach(document.getElementById('detail-root'),fixture.row,fixture.state.lang,fixture.state);
    }""")
    assert edge.locator('[data-act="hours-open"]').count() == 1
    edge.locator('[data-act="hours-open"]').click()
    edge.wait_for_function("!!document.querySelector('.dt-hours-source')")
    assert edge.evaluate('fakeCreated === 2')

    edge.evaluate("""() => {
      window.fetch = (url, options) => new Promise(resolve => {
        const body = JSON.parse(options.body);
        window.resolvePendingPermit = () => resolve({ok:true,status:200,json:async()=>({
          allowed:true,requestId:body.requestId,remainingDaily:18
        })});
      });
      fixture.show({id:'https://tabelog.com/auth-race/',gpid:'fixture-race'});
    }""")
    edge.locator('[data-act="hours-open"]').click()
    edge.wait_for_function('typeof resolvePendingPermit === "function"')
    edge.evaluate("""() => {
      fixture.state.account.signedIn=false;
      fixture.show(fixture.row);
      resolvePendingPermit();
    }""")
    edge.wait_for_timeout(50)
    assert edge.evaluate('fakeCreated === 2')
    assert edge.locator('[data-act="hours-login"]').count() == 1
    browser.close()
print('hours UI: four viewports, quota and timeout states, retry, DST reset, auth race, four languages passed')
