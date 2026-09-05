"""Scenario A — M-003: NOT signed in, two tabs of the same browser.

Both tabs share one localStorage and each holds its own in-memory copy of
favourites / blacklist / bookmarks. Pre-2.0 every edit wrote the WHOLE
thing back, so the later writer erased the earlier tab's edit. 2.0 writes
are three-way merges (base = what the tab last wrote) and a `storage`
listener reconciles the other tab within ~300 ms.

Full loss matrix: tab B does one action, then tab A does one action, and
we diff what was on disk after B against what is on disk after A.

Extra check on the "unfav" column (ruling C-3): after A un-favourites #0,
B must NOT resurrect it — not on B's next write, and not on a fresh tab.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import mc_lib as L
import fake_worker as W
from playwright.sync_api import sync_playwright

BI = json.loads((L.REPO / 'data/favorites_builtin.json').read_text(encoding='utf-8'))
U = L.urls(14)


def import_file():
    L.SCRATCH.mkdir(parents=True, exist_ok=True)
    p = L.SCRATCH / 'import_favs.json'
    p.write_text(json.dumps({'schema': 1, 'app': 'jpfoodmap',
                             'favorites': [U[10], U[11]], 'blacklist': [],
                             'bookmarks': []}))
    return p


def do_action(page, kind, tag):
    """One user action, driven through the page's own handlers."""
    if kind in ('fav', 'unfav'):
        page.evaluate('u => window.__mcTapFav(u)', U[tag])
    elif kind == 'black':
        page.evaluate('u => window.__mcTapBlack(u)', U[tag])
    elif kind == 'bookmark':
        page.evaluate('([n, la, lo]) => window.__mcAddBookmark(n, la, lo)',
                      ['pin-' + str(tag), 35.0 + tag * 0.01, 139.0 + tag * 0.01])
    elif kind == 'hidebuiltin':
        page.evaluate('([la, lo]) => window.__mcHideBuiltin(la, lo)',
                      [BI[tag]['lat'], BI[tag]['lon']])
    elif kind == 'import':
        page.set_input_files('#ssm-import-file', str(import_file()))
        page.wait_for_timeout(400)
        page.click('#imp-modal .imp-confirm')
    page.wait_for_timeout(900)   # > the 300 ms storage-event debounce


def ids(bm):
    if not isinstance(bm, list):
        return []
    return [b.get('id') for b in bm if isinstance(b, dict)]


def raw_ls(page):
    return page.evaluate("""() => {
      const g = k => { try { return JSON.parse(localStorage.getItem(k)||'null'); }
                       catch(e){ return null; } };
      return {cache: g('omakase_state_cache_v2'), bookmarks: g('tabelog.bookmarks')};
    }""")


def run(br=None):
    L.start_site()
    W.STATE.reset()
    results = []
    b_actions = ['fav', 'bookmark', 'hidebuiltin', 'black']
    a_actions = ['fav', 'unfav', 'black', 'bookmark', 'hidebuiltin', 'import']

    def one(br, b_act, a_act):
        case = f'A-{b_act}-then-{a_act}'
        ctx = L.make_context(br)          # no auth -> local mode
        ctx.on('page', lambda pg: pg.on('dialog', lambda d: d.accept()))
        tl = []
        a = L.open_tab(ctx, 'A')
        for k in range(3):
            a.evaluate('u => window.__mcTapFav(u)', U[k])
            a.wait_for_timeout(120)
        a.wait_for_timeout(700)
        tl.append({'step': 'A favourites 3 restaurants', 'disk': raw_ls(a)})
        b = L.open_tab(ctx, 'B')
        do_action(b, b_act, 5)
        after_b = raw_ls(b)
        tl.append({'step': f'B does {b_act}', 'disk': after_b,
                   'ui_B': b.evaluate('window.__mcUI()')})
        do_action(a, a_act, 0 if a_act == 'unfav' else 6)
        after_a = raw_ls(a)
        tl.append({'step': f'A does {a_act}', 'disk': after_a,
                   'ui_A': a.evaluate('window.__mcUI()'),
                   'ui_B': b.evaluate('window.__mcUI()')})

        fb = set((after_b.get('cache') or {}).get('fav') or [])
        fa = set((after_a.get('cache') or {}).get('fav') or [])
        kb = set((after_b.get('cache') or {}).get('black') or [])
        ka = set((after_a.get('cache') or {}).get('black') or [])
        bb = set(ids(after_b.get('bookmarks')))
        ba = set(ids(after_a.get('bookmarks')))
        intentional = {U[0]} if a_act == 'unfav' else set()
        lost_fav = sorted((fb - fa) - intentional)
        lost_black = sorted(kb - ka)
        lost_bm = sorted(bb - ba)
        # A's own action must have landed on disk too (not just B's kept).
        a_landed = True
        if a_act == 'fav':
            a_landed = U[6] in fa
        elif a_act == 'black':
            a_landed = U[6] in ka
        elif a_act == 'import':
            a_landed = {U[10], U[11]} <= fa
        elif a_act == 'bookmark':
            a_landed = len(ba - bb) >= 1
        elif a_act == 'hidebuiltin':
            a_landed = any(x == BI[6]['id'] for x in ba)
        resurrected = False
        if a_act == 'unfav':
            resurrected = U[0] in fa
            # B now writes again (reconciled memory + one more fav) and a
            # fresh tab reads the disk: #0 must stay gone on both.
            b.evaluate('u => window.__mcTapFav(u)', U[7])
            b.wait_for_timeout(900)
            after_b2 = raw_ls(b)
            resurrected = resurrected or (U[0] in set((after_b2.get('cache') or {}).get('fav') or []))
            c = L.open_tab(ctx, 'C')
            ui_c = c.evaluate('window.__mcUI()')
            resurrected = resurrected or c.evaluate(
                'u => (JSON.parse(localStorage.getItem("omakase_state_cache_v2")||"{}").fav||[]).includes(u)', U[0])
            tl.append({'step': 'B favourites #7 after A un-favourited #0; fresh tab C',
                       'disk': after_b2, 'ui_C': ui_c})
            c.close()
        errors = list(a.mc_errors) + list(b.mc_errors)
        res = {
            'case': case, 'b_action': b_act, 'a_action': a_act,
            'lost_favorites': [L.short(x) for x in lost_fav],
            'lost_blacklist': [L.short(x) for x in lost_black],
            'lost_bookmarks': lost_bm,
            'n_lost': len(lost_fav) + len(lost_black) + len(lost_bm),
            'a_action_landed': a_landed,
            'unfav_resurrected': resurrected,
            'page_errors': errors,
            'timeline': tl,
        }
        res['pass'] = res['n_lost'] == 0 and a_landed and not resurrected and not errors
        print(f"{case:34s} pass={res['pass']} lost={res['n_lost']} "
              f"a_landed={a_landed} resurrected={resurrected}")
        if case == 'A-fav-then-fav':
            try:
                (L.OUT / 'shots').mkdir(parents=True, exist_ok=True)
                a.screenshot(path=str(L.OUT / 'shots/A01-tabA-after.jpg'), quality=70, type='jpeg')
                b.screenshot(path=str(L.OUT / 'shots/A01-tabB-after.jpg'), quality=70, type='jpeg')
            except Exception:
                pass
        ctx.close()
        return res

    def go(br):
        for b_act in b_actions:
            for a_act in a_actions:
                results.append(one(br, b_act, a_act))

    if br is None:
        with sync_playwright() as p:
            b = p.chromium.launch()
            go(b)
            b.close()
    else:
        go(br)
    L.write_json('evidence/scenario-a-anon-matrix.json',
                 {'note': 'not signed in; two tabs of one browser; '
                          'loss = present on disk after B acted, gone after A acted',
                  'cases': results})
    failed = [r['case'] for r in results if not r['pass']]
    print(f'\n{len(failed)}/{len(results)} combinations fail: {failed}')
    return [{'id': r['case'], 'pass': r['pass'], **{k: v for k, v in r.items() if k != 'timeline'}}
            for r in results]


if __name__ == '__main__':
    sys.exit(0 if all(r['pass'] for r in run()) else 1)
