"""Real-app regression for saved heading spacing and virtual height calibration.

Uses isolated, old-format local favourites and blocks all HTTPS traffic. Run
against a built docs directory; --baseline records failures without stopping.
"""
import argparse
from collections import defaultdict
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
parser.add_argument('--browser', choices=['chromium', 'webkit'], default='chromium')
parser.add_argument('--cases', default='375:100,475:130,932:100,932:200,1440:100,375:200')
parser.add_argument('--baseline', action='store_true')
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
lib_browser.DOCS = args.docs.resolve()
records = []
failures = []

# Two restaurants in each of 70 real cities make equal-size alternate groups.
by_city = defaultdict(list)
for row in json.loads((args.docs / 'data/restaurants.json').read_text()):
    if row.get('city'):
        by_city[row['city']].append(row['detail_url'])
cities = [refs[:2] for refs in by_city.values() if len(refs) >= 2][:70]
favourites = [ref for refs in cities for ref in refs]
bookmarks = [{'id': 'bm-collapse-pin', 'name': 'Other place', 'emoji': '📍',
              'lat': 35.68, 'lon': 139.76, 'category': 'bookmark'}]
favourites.append('bm-collapse-pin')
for i, refs in enumerate(cities):
    lid = f'list:collapse-{i:02}'
    name = f'Collection {i:02}' if i != 35 else '收藏夹长名称用于检查大字模式下自然换行不会截断标题'
    bookmarks.append({'id': lid, 'category': 'meta', 'kind': 'list', 'name': name, 'emoji': '📁'})
    for j, ref in enumerate(refs):
        bookmarks.append({'id': f'member:collapse-{i}-{j}', 'category': 'meta',
                          'kind': 'member', 'list': lid, 'ref': ref})
    if i % 5 == 0:
        bookmarks.append({'id': f'list:empty-{i:02}', 'category': 'meta', 'kind': 'list',
                          'name': f'Empty {i:02}', 'emoji': '📁'})
seed = {'tabelog.lang': 'zh-CN', 'tabelog.seenIntro': '1',
        'omakase_state_cache_v2': json.dumps({'fav': favourites, 'black': [], 'dirty': False}),
        'tabelog.bookmarks': json.dumps(bookmarks)}


def check(ok, message):
    if not ok:
        failures.append(message)
        if not args.baseline:
            raise AssertionError(message)


def geometry(page, name):
    """Compare each mounted heading against the full business group sequence."""
    data = page.evaluate("""() => {
      const s=App.state, groups=ListMod.groupsFor(s), open=s.saved.openGroups;
      let seq=[], expected={}, afterRows=false;
      for(const g of groups){
        let hasRows=(!open||open.has(g.key)) && g.items.length>0;
        expected[g.key]={top:seq.length===0?8:afterRows?24:0,bottom:hasRows?12:0};
        seq.push({key:g.key,t:'g'});
        if(hasRows)for(const it of g.items)seq.push({key:it.ref,t:'r'});
        afterRows=hasRows;
      }
      const vp=document.querySelector('.ls-vp'), nodes=[...vp.children].filter(e=>!e.classList.contains('ls-pad'));
      const m=ListMod.metrics(), problems=[], heights={};
      nodes.forEach((el,j)=>{
        const idx=m.window[0]+j, r=el.getBoundingClientRect(); heights[idx]=el.offsetHeight;
        if(j && Math.abs(r.top-nodes[j-1].getBoundingClientRect().bottom)>1)
          problems.push('row gap/overlap at '+idx);
        const b=el.querySelector('[data-group]'); if(!b)return;
        const key=b.dataset.group, exp=expected[key], st=getComputedStyle(el), br=b.getBoundingClientRect();
        if(parseFloat(st.paddingTop)!==exp.top||parseFloat(st.paddingBottom)!==exp.bottom)
          problems.push(key+': padding '+st.paddingTop+'/'+st.paddingBottom+' expected '+exp.top+'/'+exp.bottom);
        if(br.height<43.9)problems.push(key+': touch target '+br.height);
        for(const emoji of b.querySelectorAll(':scope > .emj'))
          if(Math.abs(emoji.getBoundingClientRect().width-parseFloat(emoji.style.width))>.5)
            problems.push(key+': emoji width changed before image decode');
        if(j && seq[idx-1].t==='g') {
          const prev=nodes[j-1].querySelector('[data-group]').getBoundingClientRect();
          if(Math.abs(br.top-prev.bottom)>1)problems.push(key+': heading gap '+(br.top-prev.bottom));
        }
      });
      const sc=Containers.scroller('list');
      return {problems,heights,count:seq.length,metrics:m,scroll:sc.scrollTop,
        viewport:sc.clientHeight,scrollHeight:sc.scrollHeight,domHeight:vp.getBoundingClientRect().height,
        fractionalCorrection:nodes.reduce((n,e)=>n+e.getBoundingClientRect().height-e.offsetHeight,0)};
    }""")
    check(not data['problems'], name + ': ' + '; '.join(data['problems']))
    # The virtualiser stores integer offsetHeight; scaled text has fractional CSS pixels.
    check(abs(data['domHeight'] - data['metrics']['total'] - data['fractionalCorrection']) <= 2,
          name + ': virtual padding differs from total: ' + str(data))
    return data


def sweep(page, name):
    heights = {}
    page.evaluate("Containers.scroller('list').scrollTop=0")
    page.wait_for_timeout(80)
    for step in range(160):
        data = geometry(page, name)
        heights.update(data['heights'])
        if data['metrics']['window'][1] == data['count']:
            break
        page.evaluate("() => {const sc=Containers.scroller('list');sc.scrollTop+=Math.max(120,sc.clientHeight*.8)}")
        page.wait_for_timeout(35)
    check(len(heights) == data['count'], name + ': sweep missed items')
    expected = sum(heights.values())
    # Revisit both ends with every item measured, exercising cached offsets.
    for dest in ['0', '1e9', '0', '1e9']:
        page.evaluate(f"Containers.scroller('list').scrollTop={dest}")
        page.wait_for_timeout(70)
        data = geometry(page, name)
        check(abs(data['metrics']['total'] - expected) <= 2,
              name + f': total {data["metrics"]["total"]} != measured {expected}')
    records.append({'name': name, 'count': data['count'], 'total': expected, 'metrics': data['metrics']})


def open_pattern(page, pattern):
    page.evaluate("""pattern => {
      let gs=ListMod.groupsFor(App.state), keys=gs.filter((g,i)=>pattern==='all'||
        pattern==='odd'&&i%2===1&&g.items.length===2||pattern==='even'&&i%2===0&&g.items.length===2).map(g=>g.key);
      App.set({saved:{openGroups:new Set(keys)}});
      Containers.scroller('list').scrollTop=0;
    }""", pattern)
    page.wait_for_timeout(100)


try:
    with lib_browser.serve_docs(8991) as base, sync_playwright() as p:
        browser = getattr(p, args.browser).launch()
        for case in args.cases.split(','):
            width, scale = map(int, case.split(':'))
            context = browser.new_context(viewport={'width': width, 'height': 900},
                                          service_workers='block')
            page = context.new_page()
            page.set_default_timeout(20000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('https://**/*', lambda route: route.abort())
            lib_browser.seed_local_storage(page, seed)
            lib_browser.boot(page, base)
            page.evaluate("""scale=>{
              if(scale===200){document.documentElement.style.fontSize='32px';App.set({fontScale:200});App.layout.schedule()}
              else App.i18n.setFontScale(scale);
              App.act.setTab('saved');
              if(App.state.layout.mode==='narrow')App.act.setSheet('expanded');
            }""", scale)
            page.wait_for_timeout(600)
            for mode in ['city', 'list', 'city']:
                page.locator('[data-groupby]').select_option(mode)
                page.wait_for_timeout(120)
                for pattern in ['none', 'odd', 'even', 'none']:
                    open_pattern(page, pattern)
                    sweep(page, f'{case}-{mode}-{pattern}')
                if mode == 'city':
                    same_size = []
                    for pick in [20, 40]:
                        page.evaluate("""pick=>{
                          const g=ListMod.groupsFor(App.state).filter(g=>g.items.length===2)[pick];
                          App.set({saved:{openGroups:new Set([g.key])}});
                          Containers.scroller('list').scrollTop=0;
                        }""", pick)
                        page.wait_for_timeout(100)
                        same_size.append(page.evaluate('ListMod.metrics().items'))
                        sweep(page, f'{case}-{mode}-same-size-{pick}')
                    check(same_size[0] == same_size[1] and same_size[0] >= 60,
                          case + ': same-size calibration fixture changed size')
                    open_pattern(page, 'none')
                page.evaluate("Containers.scroller('list').scrollTop=0")
                page.wait_for_timeout(80)
                for _ in range(3):
                    page.locator('[data-group]').first.click()
                    page.wait_for_timeout(100)
                    geometry(page, f'{case}-{mode}-toggle')
                page.screenshot(path=str(args.output / f'{width}-{scale}-{mode}.png'))
            # Open a row far from the first group, then return to its source.
            open_pattern(page, 'all')
            page.evaluate("Containers.scroller('list').scrollTop=1600")
            page.wait_for_timeout(150)
            source = page.evaluate("""()=>{
              const ids=ListMod.visibleIds(), id=ids.find(id=>Data.byId(id));
              const row=[...document.querySelectorAll('.ls-row[data-id]')].find(e=>e.dataset.id===id);
              const offset=row.getBoundingClientRect().top-Containers.scroller('list').getBoundingClientRect().top;
              row.querySelector('[data-open]').click();return {id,offset};
            }""")
            page.wait_for_timeout(600)
            if page.evaluate("App.state.layout.mode==='mid'"):
                expanded = page.evaluate("""()=>{
                  const box=document.querySelector('.dt-cand'),title=box.querySelector('.dt-cand-title');
                  const tools=box.querySelector('.dt-cand-tools'),r=box.getBoundingClientRect(),tr=title.getBoundingClientRect(),ur=tools.getBoundingClientRect();
                  return {height:r.height,titleHeight:tr.height,lineHeight:parseFloat(getComputedStyle(title).lineHeight),
                          fits:tr.left>=r.left&&tr.right<=r.right&&ur.left>=r.left&&ur.right<=r.right};
                }""")
                check(expanded['fits'] and expanded['titleHeight'] <= 2 * expanded['lineHeight'] + 1,
                      case + ': candidate heading text squeezed into a column ' + str(expanded))
                records.append({'name': case + '-candidates-expanded', **expanded})
                page.screenshot(path=str(args.output / f'{width}-{scale}-candidates.png'))
                for _ in range(2):
                    page.locator('[data-act="cand-close"]').click()
                    page.wait_for_timeout(150)
                    cand = page.evaluate("""()=>{
                      const box=document.querySelector('.dt-cand--collapsed'),button=box.querySelector('button');
                      return {box:box.getBoundingClientRect().height,button:button.getBoundingClientRect().height};
                    }""")
                    check(cand['button'] >= 44 and abs(cand['box']-cand['button']) < 1,
                          case + ': collapsed candidate padding ' + str(cand))
                    page.locator('[data-act="cand-open"]').click()
                    page.wait_for_timeout(150)
            page.evaluate('App.nav.back()')
            page.wait_for_timeout(700)
            restored = page.evaluate("""id=>{
              const row=[...document.querySelectorAll('.ls-row[data-id]')].find(e=>e.dataset.id===id);
              return row?row.getBoundingClientRect().top-Containers.scroller('list').getBoundingClientRect().top:null;
            }""", source['id'])
            check(restored is not None and abs(restored-source['offset']) <= 3,
                  case + f': source return {source["offset"]} -> {restored}')
            check(not errors, case + ': page errors ' + str(errors))
            print(f'PASS {args.browser} {case}', flush=True)
            context.close()
        browser.close()
finally:
    (args.output / 'results.json').write_text(json.dumps({'records': records, 'failures': failures}, ensure_ascii=False, indent=2))
