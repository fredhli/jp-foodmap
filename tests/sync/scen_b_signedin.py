"""Scenario B — M-004 (and M-002 phase 0): SIGNED IN, two tabs / devices.

  B1  serialised edits, different items
  B3  one tab deletes, the other adds (delete must win, add must survive)
  B4  one tab's push fails while the other succeeds — the failed edit must
      not be erased from disk and must reach the server
  B5  two DEVICES (separate contexts) PUT inside the Worker's KV get->put
      window: the Worker accepts both (lost update). The loser's next pull
      sees its own version number with a foreign write id (`w`), rebases on
      the pre-push base and re-pushes: nothing may end up missing.
  B5b both tabs dirty offline, network back, both retry at the same instant
  B6  a tab holding a dirty edit while the other tab pulls a remote change
  B7  import in one tab interleaved with a push from the other
  B8  a pin added in each tab
  B9  hide-a-builtin in one tab vs. a pin in the other
"""
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import mc_lib as L
import fake_worker as W
from playwright.sync_api import sync_playwright

BI = json.loads((L.REPO / 'data/favorites_builtin.json').read_text(encoding='utf-8'))
U = L.urls(14)
S = L.short
PW = L.PUSH_WAIT


def seed(fav=(), black=(), bookmarks=(), v=1, dirty=False):
    W.STATE.reset({'favorites': list(fav), 'blacklist': list(black),
                   'bookmarks': list(bookmarks), 'v': v})
    return {
        'tabelog.auth': L.AUTH,
        'omakase_state_cache_v2': L.cache_blob(fav, black, dirty),
        'tabelog.syncBase': L.sync_base(v, fav, black, list(bookmarks)),
        'tabelog.bookmarks': list(bookmarks),
    }


def ls(page):
    return page.evaluate("""() => {
      const g = k => { try { return JSON.parse(localStorage.getItem(k)||'null'); }
                       catch(e){ return null; } };
      return {cache: g('omakase_state_cache_v2'),
              bookmarks: (g('tabelog.bookmarks')||[]).map(b => b && b.id),
              syncBase: (b => b && {v: b.v, w: b.w, favorites: b.favorites,
                                    blacklist: b.blacklist,
                                    nbm: (b.bookmarks||[]).length})(g('tabelog.syncBase'))};
    }""")


def stamp(tl, step, pages, extra=None):
    e = {'step': step, 'kv': W.STATE.blob(), 'disk': ls(list(pages.values())[0])}
    for name, pg in pages.items():
        e['ui_' + name] = pg.evaluate('window.__mcUI()')
    if extra:
        e.update(extra)
    tl.append(e)
    return e


def fav_set(kv):
    return set((kv or {}).get('favorites') or [])


def errs(pages):
    out = []
    for name, pg in pages.items():
        out += [f'{name}: {x}' for x in pg.mc_errors]
    return out


def puts(status=None):
    return [x for x in W.STATE.log if x['m'] == 'PUT' and (status is None or x.get('status') == status)]


def aligned(pages, js_by_tab, lead_ms=700, settle_ms=None):
    """Fire one call in every tab at the same wall-clock instant."""
    t = list(pages.values())[0].evaluate('Date.now()') + lead_ms
    for name, pg in pages.items():
        js = js_by_tab[name]
        pg.evaluate(f'() => {{ setTimeout(() => {{ {js} }}, {t} - Date.now()); }}')
    time.sleep((lead_ms + (settle_ms or PW + 800)) / 1000.0)


def b1_serialised(br):
    tl = []
    ctx = L.make_context(br, seed=seed(fav=[U[0]], v=1))
    a = L.open_tab(ctx, 'A'); b = L.open_tab(ctx, 'B')
    pages = {'A': a, 'B': b}
    a.evaluate('u => window.__mcTapFav(u)', U[1]); a.wait_for_timeout(PW)
    stamp(tl, 'A favourites #1', pages)
    b.evaluate('u => window.__mcTapFav(u)', U[2]); b.wait_for_timeout(PW)
    e = stamp(tl, 'B favourites #2', pages)
    kv = fav_set(e['kv'])
    r = {'id': 'B1', 'name': 'serialised edits, different items',
         'server_favorites': sorted(S(x) for x in kv),
         'page_errors': errs(pages), 'timeline': tl}
    r['pass'] = kv == {U[0], U[1], U[2]} and not r['page_errors']
    ctx.close()
    return r


def b3_del_vs_add(br):
    tl = []
    ctx = L.make_context(br, seed=seed(fav=[U[0], U[1], U[2]], v=1))
    a = L.open_tab(ctx, 'A'); b = L.open_tab(ctx, 'B')
    pages = {'A': a, 'B': b}
    b.evaluate('u => window.__mcTapFav(u)', U[2]); b.wait_for_timeout(PW)   # delete #2
    stamp(tl, 'B deletes #2', pages)
    a.evaluate('u => window.__mcTapFav(u)', U[5]); a.wait_for_timeout(PW)   # add #5
    e = stamp(tl, 'A adds #5', pages)
    kv = fav_set(e['kv'])
    r = {'id': 'B3', 'name': 'one tab deletes, the other adds',
         'expected': '#0 #1 #5', 'server_favorites': sorted(S(x) for x in kv),
         'page_errors': errs(pages), 'timeline': tl}
    r['pass'] = kv == {U[0], U[1], U[5]} and not r['page_errors']
    ctx.close()
    return r


def b4_failed_push(br):
    tl = []
    ctx = L.make_context(br, seed=seed(fav=[U[0]], v=1))
    a = L.open_tab(ctx, 'A'); b = L.open_tab(ctx, 'B')
    pages = {'A': a, 'B': b}
    W.STATE.put_fail = True
    a.evaluate('u => window.__mcTapFav(u)', U[3]); a.wait_for_timeout(PW)
    stamp(tl, 'A favourites #3 while the network is down (PUT 503)', pages)
    W.STATE.put_fail = False
    b.evaluate('u => window.__mcTapFav(u)', U[4]); b.wait_for_timeout(PW)
    e = stamp(tl, 'B favourites #4, network is back, B pushes OK', pages)
    disk_fav = set((e['disk'].get('cache') or {}).get('fav') or [])
    orphaned = U[3] not in disk_fav
    c = L.open_tab(ctx, 'C'); c.wait_for_timeout(PW)
    pages['C'] = c
    e2 = stamp(tl, 'a third tab opens (== what a reload would show)', pages)
    a.evaluate('window.__mcVisChange()'); a.wait_for_timeout(PW)
    e3 = stamp(tl, 'A comes back to the foreground', pages)
    kv = fav_set(e3['kv'])
    r = {'id': 'B4', 'name': "one tab's push failed while the other succeeded",
         'A_edit_erased_from_disk_by_B': orphaned,
         'third_tab_favourite_count': e2['ui_C']['fav'],
         'server_favorites': sorted(S(x) for x in kv),
         'page_errors': errs(pages), 'timeline': tl}
    r['pass'] = (not orphaned and kv == {U[0], U[3], U[4]}
                 and e2['ui_C']['fav'] == 3 and not r['page_errors'])
    ctx.close()
    return r


def b5_race_two_devices(br, window_ms, rep):
    """Two DEVICES (separate storage) PUT inside the KV get->put window."""
    tl = []
    seeded = seed(fav=[U[0]], v=1)
    ca = L.make_context(br, seed=seeded)
    cb = L.make_context(br, seed=seeded)
    a = L.open_tab(ca, 'A'); b = L.open_tab(cb, 'B')
    pages = {'A': a, 'B': b}
    W.STATE.kv_window_ms = window_ms
    aligned(pages, {'A': f'window.__mcTapFav({json.dumps(U[6])});',
                    'B': f'window.__mcTapFav({json.dumps(U[7])});'})
    e = stamp(tl, 'A favourites #6 and B favourites #7 at the same instant', pages)
    W.STATE.kv_window_ms = 0
    kv = fav_set(e['kv'])
    raced = len([x for x in puts(200) if x.get('baseV') == 1]) > 1
    lost_at_race = [S(x) for x in (U[6], U[7]) if x not in kv]
    # Both devices poll (visibility). The one whose write was dropped sees
    # v == its v but w != its w, rebases on prevSyncBase and re-pushes.
    for pg in (a, b):
        pg.evaluate('window.__mcVisChange()'); pg.wait_for_timeout(PW)
    for pg in (a, b):
        pg.evaluate('window.__mcVisChange()'); pg.wait_for_timeout(PW)
    e2 = stamp(tl, 'both devices polled twice', pages)
    kv2 = fav_set(e2['kv'])
    r = {'id': f'B5-w{window_ms}-r{rep}', 'window_ms': window_ms,
         'both_puts_accepted_at_same_baseV': raced,
         'lost_at_race': lost_at_race,
         'server_after_polls': sorted(S(x) for x in kv2),
         'ui_A_after': e2['ui_A'], 'ui_B_after': e2['ui_B'],
         'page_errors': errs(pages), 'timeline': tl,
         'worker_log': [{k: v for k, v in x.items() if k not in ('resp', 'sent', 'returned')}
                        for x in W.STATE.log]}
    r['pass'] = (kv2 == {U[0], U[6], U[7]} and e2['ui_A']['fav'] == 3
                 and e2['ui_B']['fav'] == 3 and not r['page_errors'])
    ca.close(); cb.close()
    return r


def b5b_offline_return(br, window_ms=20):
    tl = []
    ctx = L.make_context(br, seed=seed(fav=[U[0]], v=1))
    a = L.open_tab(ctx, 'A'); b = L.open_tab(ctx, 'B')
    pages = {'A': a, 'B': b}
    W.STATE.put_fail = True
    a.evaluate('u => window.__mcTapFav(u)', U[6]); a.wait_for_timeout(PW)
    b.evaluate('u => window.__mcTapFav(u)', U[7]); b.wait_for_timeout(PW)
    stamp(tl, 'network down: both tabs favourite something, both PUTs fail', pages)
    W.STATE.put_fail = False
    W.STATE.kv_window_ms = window_ms
    aligned(pages, {'A': 'window.__mcVisChange();', 'B': 'window.__mcVisChange();'},
            lead_ms=600, settle_ms=PW + 1500)
    e = stamp(tl, 'network back, both tabs become visible at the same instant', pages)
    W.STATE.kv_window_ms = 0
    kv = fav_set(e['kv'])
    r = {'id': 'B5b', 'name': 'offline -> online, both tabs retry together',
         'server_favorites': sorted(S(x) for x in kv),
         'kv_writes': len(puts(200)),
         'page_errors': errs(pages), 'timeline': tl}
    r['pass'] = kv == {U[0], U[6], U[7]} and not r['page_errors']
    ctx.close()
    return r


def b6_dirty_vs_pull(br):
    tl = []
    ctx = L.make_context(br, seed=seed(fav=[U[0]], v=1))
    a = L.open_tab(ctx, 'A'); b = L.open_tab(ctx, 'B')
    pages = {'A': a, 'B': b}
    W.STATE.put_fail = True
    a.evaluate('u => window.__mcTapFav(u)', U[3]); a.wait_for_timeout(PW)
    stamp(tl, 'A has a dirty, unsent edit (#3)', pages)
    W.STATE.put_fail = False
    W.STATE.kv['state:u1'] = json.dumps(
        {'favorites': [U[0], U[9]], 'blacklist': [], 'bookmarks': [], 'v': 2, 'w': 'phone1'})
    b.evaluate('window.__mcVisChange()'); b.wait_for_timeout(PW)
    e = stamp(tl, 'phone adds #9 remotely; tab B pulls it', pages)
    disk = set((e['disk'].get('cache') or {}).get('fav') or [])
    a.evaluate('window.__mcVisChange()'); a.wait_for_timeout(PW)
    e2 = stamp(tl, 'A comes to the foreground', pages)
    kv = fav_set(e2['kv'])
    r = {'id': 'B6', 'name': 'a dirty tab while the other tab pulls',
         'A_unsent_edit_still_on_disk_after_B_pulled': U[3] in disk,
         'B_sees_remote_add': e['ui_B']['fav'],
         'server_favorites': sorted(S(x) for x in kv),
         'page_errors': errs(pages), 'timeline': tl}
    r['pass'] = (U[3] in disk and kv == {U[0], U[3], U[9]} and not r['page_errors'])
    ctx.close()
    return r


def b7_import(br):
    tl = []
    L.SCRATCH.mkdir(parents=True, exist_ok=True)
    imp = L.SCRATCH / 'imp_b.json'
    imp.write_text(json.dumps({'schema': 1, 'app': 'jpfoodmap',
                               'favorites': [U[10], U[11]],
                               'blacklist': [], 'bookmarks': []}))
    ctx = L.make_context(br, seed=seed(fav=[U[0]], v=1))
    a = L.open_tab(ctx, 'A'); b = L.open_tab(ctx, 'B')
    a.on('dialog', lambda d: d.accept())
    pages = {'A': a, 'B': b}
    b.evaluate('u => window.__mcTapFav(u)', U[4]); b.wait_for_timeout(PW)
    stamp(tl, 'B favourites #4 and pushes', pages)
    a.set_input_files('#ssm-import-file', str(imp)); a.wait_for_timeout(500)
    a.click('#imp-modal .imp-confirm'); a.wait_for_timeout(PW)
    e = stamp(tl, 'A imports a favorites.json with two more restaurants', pages)
    kv = fav_set(e['kv'])
    r = {'id': 'B7', 'name': 'import in one tab vs. a push from the other',
         'server_favorites': sorted(S(x) for x in kv),
         'page_errors': errs(pages), 'timeline': tl}
    r['pass'] = kv == {U[0], U[4], U[10], U[11]} and not r['page_errors']
    ctx.close()
    return r


def b8_bookmarks(br):
    tl = []
    ctx = L.make_context(br, seed=seed(v=1))
    a = L.open_tab(ctx, 'A'); b = L.open_tab(ctx, 'B')
    pages = {'A': a, 'B': b}
    b.evaluate('([n,la,lo]) => window.__mcAddBookmark(n,la,lo)', ['B-pin', 35.1, 139.1])
    b.wait_for_timeout(PW)
    stamp(tl, 'B adds a pin', pages)
    a.evaluate('([n,la,lo]) => window.__mcAddBookmark(n,la,lo)', ['A-pin', 35.2, 139.2])
    a.wait_for_timeout(PW)
    e = stamp(tl, "A adds another pin", pages)
    bms = (e['kv'] or {}).get('bookmarks') or []
    names = sorted(x.get('name_src') for x in bms if isinstance(x, dict) and x.get('name_src'))
    r = {'id': 'B8', 'name': 'a pin added in each tab',
         'server_bookmarks': names, 'page_errors': errs(pages), 'timeline': tl}
    r['pass'] = names == ['A-pin', 'B-pin'] and not r['page_errors']
    ctx.close()
    return r


def b9_hide_vs_pin(br):
    tl = []
    ctx = L.make_context(br, seed=seed(v=1))
    a = L.open_tab(ctx, 'A'); b = L.open_tab(ctx, 'B')
    pages = {'A': a, 'B': b}
    b.evaluate('([n,la,lo]) => window.__mcAddBookmark(n,la,lo)', ['B-pin', 35.1, 139.1])
    b.wait_for_timeout(PW)
    stamp(tl, 'B adds a pin', pages)
    a.evaluate('([la,lo]) => window.__mcHideBuiltin(la,lo)', [BI[0]['lat'], BI[0]['lon']])
    a.wait_for_timeout(PW)
    e = stamp(tl, f'A hides the built-in landmark {BI[0]["id"]}', pages)
    bms = (e['kv'] or {}).get('bookmarks') or []
    hidden = [x.get('id') for x in bms if isinstance(x, dict) and x.get('category') == 'hidden']
    pins = [x.get('name_src') for x in bms if isinstance(x, dict) and x.get('name_src')]
    r = {'id': 'B9', 'name': 'hide a built-in landmark vs. a pin in the other tab',
         'server_hidden': hidden, 'server_pins': pins,
         'page_errors': errs(pages), 'timeline': tl}
    r['pass'] = hidden == [BI[0]['id']] and pins == ['B-pin'] and not r['page_errors']
    ctx.close()
    return r


def run(br=None):
    L.start_site()
    out = []

    def go(br):
        for fn in (b1_serialised, b3_del_vs_add, b4_failed_push, b6_dirty_vs_pull,
                   b7_import, b8_bookmarks, b9_hide_vs_pin, b5b_offline_return):
            r = fn(br)
            out.append(r)
            print(r['id'], 'pass=', r['pass'], '|',
                  json.dumps({k: v for k, v in r.items()
                              if k not in ('timeline', 'worker_log', 'name', 'pass')},
                             ensure_ascii=False)[:300])
        for w in (20, 60):
            for rep in range(2):
                r = b5_race_two_devices(br, w, rep)
                out.append(r)
                print(r['id'], 'pass=', r['pass'], 'raced=', r['both_puts_accepted_at_same_baseV'],
                      'lost_at_race=', r['lost_at_race'], 'after=', r['server_after_polls'])

    if br is None:
        with sync_playwright() as p:
            b = p.chromium.launch()
            go(b)
            b.close()
    else:
        go(br)
    L.write_json('evidence/scenario-b-signedin.json', {'cases': out})
    return out


if __name__ == '__main__':
    sys.exit(0 if all(r['pass'] for r in run()) else 1)
