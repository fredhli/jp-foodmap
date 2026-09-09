"""Scenario H — M-041 / M-124: accounts and sign-out.

H1  tab A signs out; tab B is told, goes local, uploads nothing
H2  tab A signs in; tab B (opened signed-out) edits -> B's edit lands on the
    right base (no empty-base union upload of stale state)
H3  shared browser, account switch: disk holds Alice's favourites + pin
    (syncBase.sub = 'alice'), Bob signs in, Bob's cloud has its own data ->
    the page shows Bob's cloud data and never uploads Alice's
H4  sign-out with "also clear this device's data": the three state keys are
    gone after the reload; declining keeps them
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import mc_lib as L
import fake_worker as W
from playwright.sync_api import sync_playwright

U = L.urls(14)
S = L.short
PW = L.PUSH_WAIT


def n_puts(tab=None):
    return len([x for x in W.STATE.log if x['m'] == 'PUT' and (tab is None or x.get('tab') == tab)])


def h1(br):
    tl = []
    W.STATE.reset({'favorites': [U[0]], 'blacklist': [], 'bookmarks': [], 'v': 1})
    ctx = L.make_context(br, seed={
        'tabelog.auth': L.AUTH,
        'omakase_state_cache_v2': L.cache_blob([U[0]], [], False),
        'tabelog.syncBase': L.sync_base(1, [U[0]], [], []),
        'tabelog.bookmarks': []})
    a = L.open_tab(ctx, 'A')
    b = L.open_tab(ctx, 'B')
    a.wait_for_timeout(800)
    # The real sign-out path (account menu → confirm → signOut(): DELETE
    # /api/session + wipe + reload), keeping local data.
    a.on('dialog', lambda d: d.dismiss() if '同时清除' in d.message else d.accept())
    a.evaluate("() => document.getElementById('ssm-signout').click()")
    a.wait_for_function("() => localStorage.getItem('tabelog.auth') === null", timeout=8000)
    b.wait_for_timeout(1500)
    ui_b0 = b.evaluate('window.__mcUI()')
    n_before = n_puts()
    b.evaluate('u => window.__mcTapFav(u)', U[5])
    b.wait_for_timeout(PW)
    ui = b.evaluate('window.__mcUI()')
    tl.append({'step': 'A signed out; B favourites #5', 'ui_B_before': ui_b0, 'ui_B': ui,
               'puts_added': n_puts() - n_before, 'kv': W.STATE.blob()})
    r = {'id': 'H1', 'name': 'sign out in one tab, keep using the other',
         'B_told': '另一个窗口' in ui_b0['status'],
         'B_edit_uploaded': n_puts() > n_before,
         'B_status_text': ui['status'], 'B_badge_local_only': ui['localOnly'],
         'page_errors': list(b.mc_errors), 'timeline': tl}
    r['pass'] = (r['B_told'] and not r['B_edit_uploaded'] and r['B_badge_local_only']
                 and not r['page_errors'])
    ctx.close()
    return r


def h2(br):
    tl = []
    W.STATE.reset({'favorites': [U[9]], 'blacklist': [], 'bookmarks': [], 'v': 4})
    ctx = L.make_context(br, seed={
        'omakase_state_cache_v2': L.cache_blob([U[0], U[1]], [], True),
        'tabelog.bookmarks': []})
    a = L.open_tab(ctx, 'A')
    b = L.open_tab(ctx, 'B')
    a.evaluate("(p) => localStorage.setItem('tabelog.auth', JSON.stringify(p))", L.AUTH)
    a.reload(wait_until='domcontentloaded')
    a.wait_for_timeout(PW + 1000)
    tl.append({'step': 'A signs in and syncs', 'kv': W.STATE.blob(),
               'ui_A': a.evaluate('window.__mcUI()'), 'ui_B': b.evaluate('window.__mcUI()')})
    b.evaluate('u => window.__mcTapFav(u)', U[5])
    b.wait_for_timeout(PW)
    kv = W.STATE.blob()
    put_log = [{'tab': x.get('tab'), 'baseV': x.get('baseV'), 'status': x.get('status')}
               for x in W.STATE.log if x['m'] == 'PUT']
    tl.append({'step': 'B (opened before the sign-in) favourites #5', 'kv': kv,
               'ui_B': b.evaluate('window.__mcUI()'), 'puts': put_log})
    fav = set((kv or {}).get('favorites') or [])
    r = {'id': 'H2', 'name': 'sign in in one tab while another tab is open',
         'server_favorites': sorted(S(x) for x in fav), 'puts': put_log,
         'page_errors': list(a.mc_errors) + list(b.mc_errors), 'timeline': tl}
    # B's push must ride on the base A established (baseV >= 5, no 0/409 dance)
    b_puts = [p for p in put_log if p['tab'] == 'B']
    r['pass'] = (fav == {U[0], U[1], U[9], U[5]} and b_puts
                 and all(p['baseV'] >= 5 for p in b_puts) and not r['page_errors'])
    ctx.close()
    return r


def h3(br):
    tl = []
    alice_pin = {'id': 'bm-alice1', 'name_src': 'Alice hotel', 'name_sc': '', 'name_tc': '',
                 'name_jp': '', 'name_en': '', 'emoji': '🏨', 'lat': 35.68, 'lon': 139.76,
                 'category': 'bookmark'}
    # Bob's cloud (the fake Worker's single user u1 == the auth we seed).
    W.STATE.reset({'favorites': [U[9]], 'blacklist': [U[8]], 'bookmarks': [], 'v': 4, 'w': 'bobw1'})
    ctx = L.make_context(br, seed={
        'tabelog.auth': L.AUTH,
        'omakase_state_cache_v2': L.cache_blob([U[0], U[1]], [], False),
        'tabelog.syncBase': L.sync_base(3, [U[0], U[1]], [], [alice_pin], sub='alice'),
        'tabelog.bookmarks': [alice_pin]})
    a = L.open_tab(ctx, 'A')
    a.wait_for_timeout(PW + 500)
    ui = a.evaluate('window.__mcUI()')
    disk = a.evaluate("""() => {
      const g = k => { try { return JSON.parse(localStorage.getItem(k)||'null'); } catch(e){ return null; } };
      return {cache: g('omakase_state_cache_v2'), bm: (g('tabelog.bookmarks')||[]).map(b=>b&&b.id),
              base: (b => b && {v:b.v, sub:b.sub})(g('tabelog.syncBase'))};
    }""")
    kv1 = W.STATE.blob()
    tl.append({'step': 'Bob opens the page on Alice\'s browser', 'ui': ui, 'disk': disk, 'kv': kv1})
    a.evaluate('u => window.__mcTapFav(u)', U[5])
    a.wait_for_timeout(PW)
    kv = W.STATE.blob()
    tl.append({'step': 'Bob favourites #5', 'kv': kv, 'ui': a.evaluate('window.__mcUI()')})
    fav = set((kv or {}).get('favorites') or [])
    bm_ids = [b.get('id') for b in ((kv or {}).get('bookmarks') or []) if isinstance(b, dict)]
    r = {'id': 'H3', 'name': 'account switch on a shared browser: cloud wins',
         'ui_after_boot': ui, 'disk_after_boot': disk,
         'server_favorites': sorted(S(x) for x in fav), 'server_bookmark_ids': bm_ids,
         'page_errors': list(a.mc_errors), 'timeline': tl}
    r['pass'] = (ui['fav'] == 1 and ui['black'] == 1 and '已切换账号' in ui['status']
                 and set(disk['cache']['fav']) == {U[9]} and 'bm-alice1' not in disk['bm']
                 and disk['base']['sub'] == 'u1'
                 and fav == {U[9], U[5]} and 'bm-alice1' not in bm_ids
                 and not r['page_errors'])
    try:
        (L.OUT / 'shots').mkdir(parents=True, exist_ok=True)
        a.screenshot(path=str(L.OUT / 'shots/H3-account-switch.jpg'), quality=70, type='jpeg')
    except Exception:
        pass
    ctx.close()
    return r


def h4(br, accept_clear):
    tl = []
    pin = {'id': 'bm-mine1', 'name_src': 'My pin', 'name_sc': '', 'name_tc': '', 'name_jp': '',
           'name_en': '', 'emoji': '📍', 'lat': 35.1, 'lon': 139.1, 'category': 'bookmark'}
    W.STATE.reset({'favorites': [U[0]], 'blacklist': [], 'bookmarks': [pin], 'v': 1})
    ctx = L.make_context(br, seed={
        'tabelog.auth': L.AUTH,
        'omakase_state_cache_v2': L.cache_blob([U[0]], [], False),
        'tabelog.syncBase': L.sync_base(1, [U[0]], [], [pin]),
        'tabelog.bookmarks': [pin]})
    a = L.open_tab(ctx, 'A')
    seen = []

    def on_dialog(d):
        seen.append(d.message)
        # 1st confirm = the existing "退出登录？" prompt; 2nd = "同时清除本设备数据？"
        if '同时清除' in d.message or 'clear this device' in d.message:
            (d.accept() if accept_clear else d.dismiss())
        else:
            d.accept()
    a.on('dialog', on_dialog)
    a.evaluate("() => document.getElementById('ssm-signout').click()")
    a.wait_for_function("() => localStorage.getItem('tabelog.auth') === null", timeout=8000)
    a.wait_for_timeout(2500)   # reload + boot
    ls = a.evaluate("""() => ({auth: localStorage.getItem('tabelog.auth'),
      cache: localStorage.getItem('omakase_state_cache_v2'),
      base: localStorage.getItem('tabelog.syncBase'),
      bm: (JSON.parse(localStorage.getItem('tabelog.bookmarks')||'[]')).map(b=>b&&b.id)})""")
    tl.append({'step': 'after sign-out + reload', 'dialogs': seen, 'ls': ls})
    r = {'id': 'H4-' + ('clear' if accept_clear else 'keep'),
         'name': 'sign out ' + ('and clear this device' if accept_clear else 'keeping local data'),
         'dialogs_seen': len(seen), 'ls_after': ls,
         'page_errors': list(a.mc_errors), 'timeline': tl}
    if accept_clear:
        r['pass'] = (len(seen) == 2 and ls['auth'] is None and ls['cache'] is None
                     and ls['base'] is None and 'bm-mine1' not in ls['bm']
                     and not r['page_errors'])
    else:
        r['pass'] = (len(seen) == 2 and ls['auth'] is None and ls['cache'] is not None
                     and ls['base'] is not None and 'bm-mine1' in ls['bm']
                     and not r['page_errors'])
    ctx.close()
    return r


def run(br=None):
    L.start_site()
    out = []

    def go(br):
        for fn in (h1, h2, h3, lambda b: h4(b, True), lambda b: h4(b, False)):
            r = fn(br)
            out.append(r)
            print(r['id'], 'pass=', r['pass'], '|',
                  json.dumps({k: v for k, v in r.items() if k not in ('timeline', 'name', 'pass')},
                             ensure_ascii=False)[:400])

    if br is None:
        with sync_playwright() as p:
            b = p.chromium.launch()
            go(b)
            b.close()
    else:
        go(br)
    L.write_json('evidence/scenario-h-signout.json', {'cases': out})
    return out


if __name__ == '__main__':
    sys.exit(0 if all(r['pass'] for r in run()) else 1)
