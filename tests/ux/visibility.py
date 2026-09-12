"""The 3.2.0 acceptance numbers, as an executable gate (M-3.2-11).

    .venv-wsl/bin/python tests/ux/visibility.py --output /tmp/jpfoodmap-vis
    .venv-wsl/bin/python tests/ux/visibility.py --output DIR --only iphone402

This is the audit's `audit_outputs/3.2.0-plan/ux-phone/run_flows.py`, kept
only for the parts the plan turned into thresholds and rewritten against
3.2.0's selectors. The audit script drove two *running servers* (2.3.0 on
8231, 3.1.1 on 8311) through 23 narrative steps and wrote a comparison
table; it also still clicks `#phone-nav`, which 3.2.0 deleted. What is worth
keeping is its pixel sampler, so that is what came across verbatim.

The thresholds, from the engineering plan's acceptance section:

| state         | 402x874 | 475x751 | 591x689 |
|---------------|---------|---------|---------|
| home          | >= 78%  | >= 78%  | (recorded) |
| drawer open   | >= 10%  | (recorded) | >= 30% |
| detail open   | >= 18%  | >= 18%  | >= 18%  |

plus, on every viewport and every state: no page-level horizontal overflow,
and none of the strings on the audit's truncation list clipped. And, once
per run and independent of the browser: no full-width comma or semicolon
anywhere in the shipped EN / JA translation tables.

Two measurement notes, both inherited from the audit so the numbers stay
comparable with its table:

  * "visible" for a drawer state counts the scrim. The 2.3.0 overlay drawer
    leaves the map painted behind a `.18` scrim — dimmed, still legible,
    still the thing that tells you where you are — and that is exactly the
    36% the plan quotes for 591. An opaque panel scores zero either way.
  * the home state is measured with the intro bar up, which is what the
    plan's 78% baseline was measured with; hiding it would inflate the
    number by four points and stop protecting the first screen a new
    visitor sees.

402x874 is measured on WebKit and nothing else: it is an iPhone, and a
Chromium pass on those dimensions is not evidence about iOS Safari.

M-3.2-R1: every viewport runs once per UI language (zh-CN, en, ja). The
intro bar is the one piece of home-screen chrome whose height depends on
the language — a 53-character English sentence folded it to three rows and
took 475x751 home to 77.0% while the Chinese run read 83.4% — so a single
language gate was blind to exactly the row most likely to break. The
language is part of the screenshot name and of every results.json record.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))
import lib_browser  # noqa: E402


# The Fold rows are Chromium, the iPhone row is WebKit. `home`/`drawer`/
# `detail` are the floors from the plan; None means "record it, don't gate
# it" (the plan only names two devices for home and one for the drawer).
VIEWPORTS = {
    'iphone402': dict(w=402, h=874, browser='webkit', dpr=3,
                      home=0.78, drawer=0.10, detail=0.18),
    'fold475':   dict(w=475, h=751, browser='chromium', dpr=2,
                      home=0.78, drawer=None, detail=0.18),
    'fold591':   dict(w=591, h=689, browser='chromium', dpr=2,
                      home=None, drawer=0.30, detail=0.18),
}

# The three UI languages the gate runs in. zh-TW is derived from zh-CN by
# to_trad() at build time and is the same length, so it is not a fourth run.
LANGS = ('zh-CN', 'en', 'ja')

# Strings the audit caught clipped at a phone width (ux-phone/table_E.md).
# Every one of them is a whole fact the user needs — a price ceiling, a
# booking verdict, a distance, how many landmarks a layer holds — so a
# clip is a lost fact, not a cosmetic nit. The station-name rows in that
# table are all fold816 / fold932, which are column layouts and out of
# scope here.
# 4.0.0: the same six things, under the class names that now carry them.
#   #lp-n-builtin / .lp-sub  -> the layers popover's counts and sub-titles
#   .wb-row-pr / .ux-distance -> the result row's price and distance
#   .wb-row-net               -> the detail card's booking-policy line
#   .rst-list-new             -> the collection chip on the detail card
TRUNCATION_WATCH = (
    '.ov-layer-sub',            # "219 built-in landmarks + 0 of your own"
    '.ov-layer-title',          # the layer title that ran off the popover
    '.price-text',              # "¥10,000 - 20,000 max"
    '.dt-policy-text',          # "No online booking link detected"
    '.ls-dist',                 # "152 m"
    '.dt-list-chip',            # the collection chip on the detail card
)

# D6 / SPEC F: an EN or JA string must never carry a full-width comma or
# semicolon. Those appear when a long Chinese sentence is split on its own
# punctuation, each fragment translated, and the fragments joined back with
# the original separators — which is why 3.2.0 moved that copy to
# whole-sentence keys.
FULLWIDTH = '，；'

METRICS_JS = r"""
() => {
  const W = innerWidth, H = innerHeight;
  const mapEl = document.querySelector('.leaflet-container');
  const step = 10; let total = 0, vis = 0;
  const alphaOf = (bg) => { const m = bg && bg.match(/rgba?\(([^)]+)\)/); if (!m) return 0;
    const p = m[1].split(',').map(s => parseFloat(s)); return p.length < 4 ? 1 : p[3]; };
  // 0 = see-through, 1 = scrim (dims but the map is still readable), 2 = opaque
  function opaque(el) {
    if (el === document.documentElement || el === document.body) return 0;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return 0;
    const a = alphaOf(cs.backgroundColor) * parseFloat(cs.opacity || 1);
    if (a >= 0.5) return 2;
    if (a > 0.05) return 1;
    if (cs.backgroundImage !== 'none') return 2;
    if (/^(IMG|svg|SVG|CANVAS|BUTTON|INPUT|SELECT|TEXTAREA|VIDEO)$/.test(el.tagName)) return 2;
    for (const n of el.childNodes) { if (n.nodeType === 3 && n.textContent.trim()) return 2; }
    return 0;
  }
  let dim = 0;
  for (let y = step / 2; y < H; y += step) for (let x = step / 2; x < W; x += step) {
    total++;
    const els = document.elementsFromPoint(x, y);
    let occ = false, hit = false, scrim = false;
    for (const el of els) {
      if (mapEl && (el === mapEl || mapEl.contains(el))) { hit = true; break; }
      const o = opaque(el);
      if (o === 2) { occ = true; break; }
      if (o === 1) scrim = true;
    }
    // An inert map (setBackgroundInert while a drawer is open) is skipped by
    // hit-testing even though it is painted: fall back to geometry when
    // nothing opaque was found.
    if (!hit && !occ && mapEl) {
      const mr = mapEl.getBoundingClientRect(); const mcs = getComputedStyle(mapEl);
      if (mcs.visibility !== 'hidden' && mcs.display !== 'none' && x >= mr.left && x <= mr.right && y >= mr.top && y <= mr.bottom) hit = true;
    }
    if (hit && !occ) { if (scrim) dim++; else vis++; }
  }
  const descr = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    else if (el.className && typeof el.className === 'string') s += '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.');
    const t = (el.textContent || '').trim().replace(/\s+/g, ' ');
    return s + (t ? ' "' + t.slice(0, 40) + '"' : '');
  };
  const visibleRect = (el) => { const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < H && r.right > 0 && r.left < W; };
  const shown = (el) => { for (let e = el; e && e !== document.body; e = e.parentElement) {
      const cs = getComputedStyle(e); if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return false; }
    return true; };
  const overflowing = [];
  for (const el of document.querySelectorAll('body *')) {
    if (mapEl && mapEl.contains(el) && el !== mapEl) continue;
    if (!shown(el)) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || r.bottom < 0 || r.top > H) continue;
    if (r.right > W + 1 || r.left < -1) {
      let clipped = false;
      for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
        const o = getComputedStyle(p).overflowX; if (o === 'hidden' || o === 'clip' || o === 'auto' || o === 'scroll') { clipped = true; break; } }
      overflowing.push({el: descr(el), left: Math.round(r.left), right: Math.round(r.right), clippedByAncestor: clipped});
    }
    if (overflowing.length >= 12) break;
  }
  const truncated = [];
  for (const el of document.querySelectorAll('body *')) {
    if (mapEl && mapEl.contains(el)) continue;
    if (!shown(el) || !visibleRect(el)) continue;
    const cs = getComputedStyle(el);
    const clipsX = cs.overflowX === 'hidden' || cs.overflowX === 'clip';
    if (!clipsX) continue;
    if (el.scrollWidth > el.clientWidth + 1 && el.textContent.trim()) {
      truncated.push({el: descr(el), sel: (el.id ? '#' + el.id : '') ,
                      cls: (typeof el.className === 'string' ? el.className : ''),
                      ellipsis: cs.textOverflow === 'ellipsis',
                      scrollW: el.scrollWidth, clientW: el.clientWidth});
    }
    if (truncated.length >= 25) break;
  }
  return {
    innerWidth: W, innerHeight: H, scrollWidth: document.documentElement.scrollWidth,
    pageOverflow: document.documentElement.scrollWidth > W,
    mapVisible: total ? +(vis / total).toFixed(3) : null,
    mapDimmed: total ? +(dim / total).toFixed(3) : null,
    overflowing, truncated,
    body: document.body.className,
    sheetOpen: !!(window.App && App.state.selected.id),
    drawerOpen: !!(window.App && App.state.sheet.state !== 'collapsed'),
  };
}
"""


def watched(entry: dict) -> list[str]:
    """Which TRUNCATION_WATCH selectors this clipped element answers to."""
    classes = set((entry.get('cls') or '').split())
    ident = entry.get('sel') or ''
    return [s for s in TRUNCATION_WATCH
            if (s.startswith('#') and s == ident)
            or (s.startswith('.') and s[1:] in classes)]


def i18n_tables(docs: Path) -> dict[str, dict]:
    """The EN and JA maps as they are actually shipped, read back out of the
    built page rather than out of data/i18n/ — the build is what a visitor
    gets, and `to_trad` / the CJK-run scan sit between the two."""
    html = (docs / 'index.html').read_text(encoding='utf-8')
    out = {}
    for lang, var in (('en', 'TEXT_EN_MAP'), ('ja', 'TEXT_JA_MAP')):
        i = html.index('var ' + var)
        i = html.index('{', i)
        depth, j, instr, esc = 0, i, False, False
        while j < len(html):
            c = html[j]
            if instr:
                if esc:
                    esc = False
                elif c == '\\':
                    esc = True
                elif c == '"':
                    instr = False
            elif c == '"':
                instr = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out[lang] = json.loads(html[i:j + 1])
    return out


def check_punctuation(docs: Path) -> list[str]:
    bad = []
    for lang, table in i18n_tables(docs).items():
        for src, dst in table.items():
            if isinstance(dst, str) and any(ch in dst for ch in FULLWIDTH):
                bad.append(f'{lang}: {src!r} -> {dst!r}')
    return bad


def measure(page, out_dir: Path, vp: str, lang: str, state: str,
            records: list) -> dict:
    page.wait_for_timeout(450)
    m = page.evaluate(METRICS_JS)
    m.update(vp=vp, lang=lang, state=state)
    m['mapReachable'] = round((m['mapVisible'] or 0) + (m['mapDimmed'] or 0), 3)
    m['watched'] = [e for e in m['truncated'] if watched(e)]
    page.screenshot(path=str(out_dir / f'{vp}-{lang}-{state}.png'))
    records.append(m)
    print(f"  {vp:9s} {lang:5s} {state:8s} map={m['mapReachable']:.0%} "
          f"(solid {m['mapVisible']:.0%}) overflow={m['pageOverflow']} "
          f"clipped={len(m['watched'])}", flush=True)
    return m


def run_viewport(browser, base, vp: str, cfg: dict, lang: str, out_dir: Path,
                 records: list, failures: list) -> None:
    ctx = browser.new_context(
        viewport={'width': cfg['w'], 'height': cfg['h']},
        device_scale_factor=cfg['dpr'], is_mobile=True, has_touch=True,
        service_workers='block', locale=lang)
    ctx.set_default_timeout(15000)
    # A language, so the first-visit chooser does not own the screen — but
    # deliberately NOT tabelog.seenIntro: the plan's 78% home baseline was
    # measured with the intro bar up.
    ctx.add_init_script("localStorage.setItem('tabelog.lang',%r)" % lang)
    page = ctx.new_page()
    errors: list[str] = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.route('https://**/*', lambda r: r.abort())
    lib_browser.boot(page, base)
    page.wait_for_timeout(700)

    tag = f'{vp} {lang}'

    def gate(state: str, floor):
        m = measure(page, out_dir, vp, lang, state, records)
        if floor is not None and m['mapReachable'] < floor:
            failures.append(
                f'{tag} {state}: map is {m["mapReachable"]:.1%} of the screen, '
                f'floor is {floor:.0%}')
        if m['pageOverflow']:
            failures.append(
                f'{tag} {state}: the page scrolls sideways '
                f'({m["scrollWidth"]}px in a {m["innerWidth"]}px window); '
                f'{m["overflowing"][:3]}')
        for e in m['watched']:
            failures.append(f'{tag} {state}: {e["el"]} is clipped '
                            f'({e["scrollW"]} into {e["clientW"]}px)')
        return m

    gate('home', cfg['home'])

    # The drawer, opened the way a user opens it: the results segment of the
    # floating pill (M-3.2-03).
    # 3.2.x: the results segment of the floating #wb-seg pill. 4.0: the same
    # segment, now in the bottom sheet's own entry bar.
    page.locator('#sheet-head [data-ct="tab"][data-tab="results"]').click()
    page.wait_for_selector('#list-root .ls-row[data-id]')
    page.wait_for_timeout(500)
    gate('drawer', cfg['drawer'])

    # A detail card, opened from a result row so it carries the source-return
    # link and the full #bs-foot dock.
    page.evaluate("""()=>{document.querySelector('#list-root .ls-row[data-id] .ls-open')
      .dispatchEvent(new MouseEvent('click',{bubbles:true}))}""")
    page.wait_for_function('() => !!App.state.selected.id')
    page.wait_for_timeout(900)
    gate('detail', cfg['detail'])

    # Back to the map, then the layers popover — the only place
    # #lp-n-builtin and .lp-sub are on screen, and both were on the audit's
    # clipped list at every phone width.
    page.evaluate("""() => { App.act.closeDetail && App.act.closeDetail();
      App.act.setSheet('collapsed'); }""")
    page.wait_for_timeout(400)
    page.locator('[data-fab="layers"]').click()
    page.wait_for_timeout(500)
    gate('layers', None)

    if errors:
        failures.append(f'{tag}: page errors {errors[:3]}')
    ctx.close()


def main(argv=None) -> int:
    from playwright.sync_api import sync_playwright

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--docs', type=Path, default=ROOT / 'docs')
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--only', help='comma-separated viewport names')
    ap.add_argument('--langs', default=','.join(LANGS),
                    help='comma-separated UI languages (default: all three)')
    args = ap.parse_args(argv)
    langs = [l for l in args.langs.split(',') if l]
    args.output.mkdir(parents=True, exist_ok=True)
    lib_browser.DOCS = args.docs.resolve()

    picked = {k: v for k, v in VIEWPORTS.items()
              if not args.only or k in args.only.split(',')}
    records: list[dict] = []
    failures: list[str] = []

    bad = check_punctuation(args.docs)
    if bad:
        failures.append(f'{len(bad)} EN/JA string(s) carry a full-width '
                        f'comma or semicolon: {bad[:5]}')
    print(f'i18n punctuation: {len(bad)} offending string(s)')

    with lib_browser.serve_docs(8992) as base, sync_playwright() as p:
        for engine in ('chromium', 'webkit'):
            rows = {k: v for k, v in picked.items() if v['browser'] == engine}
            if not rows:
                continue
            browser = getattr(p, engine).launch()
            for vp, cfg in rows.items():
                for lang in langs:
                    print(f'[{vp} {lang}] {cfg["w"]}x{cfg["h"]} on {engine}')
                    try:
                        run_viewport(browser, base, vp, cfg, lang, args.output,
                                     records, failures)
                    except Exception as exc:  # keep measuring the others
                        failures.append(f'{vp} {lang}: {exc!r}')
                        print(f'  !! {vp} {lang} failed: {exc!r}'[:400],
                              flush=True)
            browser.close()

    (args.output / 'results.json').write_text(
        json.dumps({'records': records, 'failures': failures},
                   ensure_ascii=False, indent=2))
    if failures:
        print('\nvisibility: FAIL')
        for f in failures:
            print('  -', f)
        return 1
    print(f'\nvisibility: {len(records)} measurements, all thresholds met')
    return 0


if __name__ == '__main__':
    sys.exit(main())
