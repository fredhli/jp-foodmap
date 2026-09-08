"""Scenario I — the remaining M1 sync acceptance points.

I1  M-045: favourite, then the tab goes to the background 300 ms later ->
    one keepalive PUT goes out before the 2.5 s debounce would have fired;
    coming back does not spend a second KV write
I2  M-047/M-138: another tab's favourite shows up here within 1 s (storage
    event) and the receiving tab sends ZERO PUTs
I3  M-046: localStorage writes throw -> status shows the warning, the
    favourite still counts and (signed in) still uploads; no page errors.
    Anonymous variant: the warning shows and the page keeps working.
I4  M-053: a device whose push keeps failing still receives remote changes
    and re-pushes the merge once the network is back
I5a UTF-8 data over 200,000 bytes stays complete and dirty with zero PUTs.
I5b A small body rejected by the server with 413 is not repeatedly uploaded;
    GET stays live and editing the content resumes sync.
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


def seed(fav=(), v=1, dirty=False, auth=True):
    W.STATE.reset({'favorites': list(fav), 'blacklist': [], 'bookmarks': [], 'v': v})
    d = {'omakase_state_cache_v2': L.cache_blob(fav, [], dirty),
         'tabelog.syncBase': L.sync_base(v, fav, [], []),
         'tabelog.bookmarks': []}
    if auth:
        d['tabelog.auth'] = L.AUTH
    return d


def puts(tab=None):
    return [x for x in W.STATE.log if x['m'] == 'PUT' and (tab is None or x.get('tab') == tab)]


def i1_keepalive(br):
    ctx = L.make_context(br, seed=seed([U[0]]))
    a = L.open_tab(ctx, 'A')
    a.wait_for_timeout(800)
    t0 = W.STATE.rel()
    a.evaluate('u => window.__mcTapFav(u)', U[3])
    a.wait_for_timeout(300)
    a.evaluate('window.__mcGoHidden()')
    a.wait_for_timeout(1200)
    ka = a.evaluate('window.__mcKeepalive')
    early = [x for x in puts('A') if x['t'] - t0 < 2000]
    a.evaluate('window.__mcVisChange()')       # back to the foreground
    a.wait_for_timeout(PW + 800)
    ui = a.evaluate('window.__mcUI()')
    kv = W.STATE.blob()
    r = {'id': 'I1', 'name': 'keepalive flush on hide',
         'keepalive_calls': ka, 'early_puts': [{'t': round(x['t'] - t0), 'keepalive': x.get('keepalive'),
                                                 'status': x.get('status')} for x in early],
         'kv_writes_total': len([x for x in puts() if x.get('status') == 200]),
         'server_favorites': sorted(S(x) for x in (kv or {}).get('favorites') or []),
         'ui_after': ui, 'page_errors': list(a.mc_errors)}
    r['pass'] = (len(ka) >= 1 and ka[0]['method'] == 'PUT'
                 and any(x.get('keepalive') for x in early)
                 and set((kv or {}).get('favorites') or []) == {U[0], U[3]}
                 and r['kv_writes_total'] == 1
                 and not ui['fabPending'] and not r['page_errors'])
    ctx.close()
    return r


def i2_storage_propagation(br):
    ctx = L.make_context(br, seed=seed([U[0]]))
    a = L.open_tab(ctx, 'A'); b = L.open_tab(ctx, 'B')
    a.wait_for_timeout(500)
    a.evaluate('u => window.__mcTapFav(u)', U[4])
    b.wait_for_timeout(1000)
    ui_b_1s = b.evaluate('window.__mcUI()')
    b.wait_for_timeout(PW + 1500)
    ui_b = b.evaluate('window.__mcUI()')
    r = {'id': 'I2', 'name': 'storage event propagation, receiver sends nothing',
         'B_fav_after_1s': ui_b_1s['fav'], 'B_fav_final': ui_b['fav'],
         'puts_from_B': len(puts('B')), 'puts_from_A': len(puts('A')),
         'page_errors': list(a.mc_errors) + list(b.mc_errors)}
    r['pass'] = (ui_b_1s['fav'] == 2 and r['puts_from_B'] == 0 and r['puts_from_A'] == 1
                 and not r['page_errors'])
    ctx.close()
    return r


def i3_storage_blocked(br, signed_in):
    ctx = L.make_context(br, seed=seed([U[0]], auth=signed_in))
    a = L.open_tab(ctx, 'A')
    a.wait_for_timeout(800)
    a.evaluate('window.__mcBlockStorage()')
    a.evaluate('u => window.__mcTapFav(u)', U[2])
    a.wait_for_timeout(PW + 500)
    ui = a.evaluate('window.__mcUI()')
    # The rest of the page must keep working: another toggle, a search box
    # keystroke, a bookmark modal open/close.
    a.evaluate('u => window.__mcTapBlack(u)', U[3])
    a.wait_for_timeout(300)
    ui2 = a.evaluate('window.__mcUI()')
    r = {'id': 'I3-' + ('signedin' if signed_in else 'anon'),
         'name': 'localStorage writes throw', 'status': ui['status'], 'ui': ui, 'ui2': ui2,
         'puts': len(puts('A')), 'page_errors': list(a.mc_errors)}
    r['pass'] = ('浏览器不允许' in ui['status'] and ui['fav'] == 2 and ui2['black'] == 1
                 and (not signed_in or r['puts'] >= 1) and not r['page_errors'])
    try:
        (L.OUT / 'shots').mkdir(parents=True, exist_ok=True)
        a.screenshot(path=str(L.OUT / f"shots/I3-{r['id']}.jpg"), quality=70, type='jpeg')
    except Exception:
        pass
    ctx.close()
    return r


def i4_dirty_keeps_pulling(br):
    ctx = L.make_context(br, seed=seed([U[0]]))
    a = L.open_tab(ctx, 'A')
    W.STATE.put_fail = True
    a.evaluate('u => window.__mcTapFav(u)', U[3]); a.wait_for_timeout(PW)
    ui1 = a.evaluate('window.__mcUI()')
    W.STATE.kv['state:u1'] = json.dumps(
        {'favorites': [U[0], U[9]], 'blacklist': [], 'bookmarks': [], 'v': 2, 'w': 'phone1'})
    a.evaluate('window.__mcVisChange()')
    a.wait_for_function('window.__mcUI().fav === 3', timeout=7500)
    ui2 = a.evaluate('window.__mcUI()')
    failing_puts = list(puts('A'))
    dirty_while_failing = a.evaluate("JSON.parse(localStorage.getItem('omakase_state_cache_v2')).dirty")
    W.STATE.put_fail = False
    a.evaluate('window.__mcVisChange()')
    a.wait_for_function('!window.__mcUI().fabPending', timeout=7500)
    ui3 = a.evaluate('window.__mcUI()')
    kv = W.STATE.blob()
    r = {'id': 'I4', 'name': 'dirty device with a failing push still receives',
         'fav_after_failed_push': ui1['fav'], 'fav_after_remote_change': ui2['fav'],
         'status_while_failing': ui2['status'], 'ui_final': ui3,
         'dirty_while_failing': dirty_while_failing,
         'failing_put_times': [x['t'] for x in failing_puts],
         'server_favorites': sorted(S(x) for x in (kv or {}).get('favorites') or []),
         'page_errors': list(a.mc_errors)}
    r['pass'] = (ui1['fav'] == 2 and ui2['fav'] == 3 and ui3['fav'] == 3
                 and set((kv or {}).get('favorites') or []) == {U[0], U[3], U[9]}
                 and dirty_while_failing is True and ui2['fabPending']
                 and all(y['t'] - x['t'] >= 4900 for x, y in zip(failing_puts, failing_puts[1:]))
                 and not ui3['fabPending'] and not r['page_errors'])
    ctx.close()
    return r


def i5_too_big(br):
    big = ['https://tabelog.com/tokyo/A1301/A130101/%08d/' % i for i in range(4300)]
    W.STATE.reset({'favorites': [U[0]], 'blacklist': [], 'bookmarks': [], 'v': 1})
    ctx = L.make_context(br, seed={
        'tabelog.auth': L.AUTH,
        'omakase_state_cache_v2': L.cache_blob([U[0]] + big, [], True),
        'tabelog.syncBase': L.sync_base(1, [U[0]], [], []),
        'tabelog.bookmarks': []})
    a = L.open_tab(ctx, 'A')
    a.wait_for_timeout(PW + 1500)
    ui = a.evaluate('window.__mcUI()')
    cache = a.evaluate("() => JSON.parse(localStorage.getItem('omakase_state_cache_v2'))")
    body_bytes = a.evaluate("""() => new TextEncoder().encode(JSON.stringify({
      favorites: JSON.parse(localStorage.getItem('omakase_state_cache_v2')).fav,
      blacklist: [], bookmarks: [], baseV: 1, w: 'byte-check'})).length""")
    r = {'id': 'I5a', 'name': 'UTF-8 body over 200k bytes is blocked before PUT',
         'status': ui['status'], 'ui': ui, 'puts': [x.get('status') for x in puts('A')],
         'disk_dirty': cache.get('dirty'), 'disk_fav_count': len(cache.get('fav') or []),
         'body_bytes': body_bytes, 'all_favorites_retained': set(cache.get('fav') or []) == set([U[0]] + big),
         'page_errors': list(a.mc_errors)}
    r['pass'] = ('同步数据超过上限' in ui['status'] and r['puts'] == [] and body_bytes > 200000
                 and cache.get('dirty') is True and r['disk_fav_count'] == 4301
                 and r['all_favorites_retained']
                 and not r['page_errors'])
    ctx.close()
    return r


def i5_server_413(br):
    ctx = L.make_context(br, seed=seed([U[0]]))
    a = L.open_tab(ctx, 'A')
    W.STATE.put_status = 413
    a.evaluate('u => window.__mcTapFav(u)', U[3]); a.wait_for_timeout(PW)
    first_puts = list(puts('A'))
    get0 = len([x for x in W.STATE.log if x['m'] == 'GET' and x.get('p') == '/api/state'])
    for _ in range(3):
        a.wait_for_timeout(1100)
        a.evaluate('window.__mcVisChange()')
    a.wait_for_timeout(300)
    rejected_ui = a.evaluate('window.__mcUI()')
    rejected_puts = list(puts('A'))
    gets = len([x for x in W.STATE.log if x['m'] == 'GET' and x.get('p') == '/api/state']) - get0
    cache = a.evaluate("JSON.parse(localStorage.getItem('omakase_state_cache_v2'))")
    W.STATE.put_status = None
    a.evaluate('u => window.__mcTapFav(u)', U[0])
    a.wait_for_function('!window.__mcUI().fabPending', timeout=7500)
    kv = W.STATE.blob()
    r = {'id': 'I5b', 'name': 'server 413 blocks same content while GET and later edit work',
         'first_put_statuses': [x.get('status') for x in first_puts],
         'first_put_bytes': [x.get('bytes') for x in first_puts],
         'puts_before_edit': len(rejected_puts), 'additional_gets': gets,
         'rejected_ui': rejected_ui, 'disk_dirty': cache.get('dirty'),
         'retained_favorites': sorted(S(u) for u in cache.get('fav') or []),
         'final_favorites': sorted(S(u) for u in (kv or {}).get('favorites') or []),
         'page_errors': list(a.mc_errors)}
    r['pass'] = (r['first_put_statuses'] == [413] and all(n < 200000 for n in r['first_put_bytes'])
                 and r['puts_before_edit'] == 1 and gets >= 3
                 and cache.get('dirty') is True and set(cache.get('fav') or []) == {U[0], U[3]}
                 and set((kv or {}).get('favorites') or []) == {U[3]} and not r['page_errors'])
    ctx.close()
    return r


def run(br=None):
    L.start_site()
    out = []

    def go(br):
        for fn in (i1_keepalive, i2_storage_propagation,
                   lambda b: i3_storage_blocked(b, True), lambda b: i3_storage_blocked(b, False),
                   i4_dirty_keeps_pulling, i5_too_big, i5_server_413):
            r = fn(br)
            out.append(r)
            print(r['id'], 'pass=', r['pass'], '|',
                  json.dumps({k: v for k, v in r.items() if k not in ('name', 'pass')},
                             ensure_ascii=False)[:400])

    if br is None:
        with sync_playwright() as p:
            b = p.chromium.launch()
            go(b)
            b.close()
    else:
        go(br)
    L.write_json('evidence/scenario-i-misc.json', {'cases': out})
    return out


if __name__ == '__main__':
    sys.exit(0 if all(r['pass'] for r in run()) else 1)
