"""Mobile detail density, complete budgets, navigation, tabs and real inset surface."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))
import lib_browser
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('--docs', type=Path, default=ROOT / 'docs')
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
lib_browser.DOCS = args.docs.resolve()
records, failures = [], []
cases = [('phone375',375,812,375,812,3), ('phone402',402,874,402,874,3),
         ('cover475',475,650,475,751,2.625), ('cover416',416,600,416,657,3),
         ('inner932',932,704,932,704,2.625), ('desktop',1440,900,1920,1080,1)]
probe = """() => {
 const box=e=>{if(!e)return null;const r=e.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height,b:r.bottom}};
 const q=s=>document.querySelector(s), back=q('#detail-root [data-ct="back"]');
 const title=q('.dt-title-row'), titleName=q('.dt-title'), titleTools=q('.dt-tools'), sum=q('.dt-summary'), row=q('.dt-actions');
 const sc=Containers.scroller('detail');
 const visible=e=>{const r=e.getBoundingClientRect(),c=getComputedStyle(e);return r.width>0&&r.height>0&&c.visibility==='visible'&&c.display!=='none'};
 const glyph=e=>[...e.querySelectorAll('.dt-act-label,svg,img')].some(n=>visible(n)&&(n.tagName!=='IMG'||n.complete&&n.naturalWidth>0));
 return {mode:App.state.layout.mode,minReading:App.layout.CFG.sheet.minText,rootFont:getComputedStyle(document.documentElement).fontSize,
  title:box(title),titleName:box(titleName),titleTools:box(titleTools),compact:title.classList.contains('is-compact-tools'),body:box(sc),
  header:box(q(App.state.layout.mode==='narrow'?'#sheet-head':'#col-detail-head')),
  back:box(back),backLabel:back?.getAttribute('aria-label'),summary:box(sum),
  values:[...sum.querySelectorAll('.dt-sum-label,.dt-sum-val')].map(e=>({text:e.textContent,w:e.clientWidth,sw:e.scrollWidth,overflow:getComputedStyle(e).overflow})),
  budget:box(q('.dt-sum-budget')),booking:box(q('.dt-sum-book')),
  cells:[...sum.querySelectorAll('.dt-sum-cell')].map(e=>{const r=e.getBoundingClientRect(),kids=[...e.children].map(n=>n.getBoundingClientRect());const l=Math.min(...kids.map(x=>x.left)),rr=Math.max(...kids.map(x=>x.right));return {box:box(e),centerDelta:Math.abs((l+rr)/2-(r.left+r.right)/2)}}),
  stack:row.classList.contains('is-stack'),
  actions:[...row.children].map(e=>({box:box(e),label:e.getAttribute('aria-label'),pressed:e.getAttribute('aria-pressed'),icon:e.classList.contains('is-icon-only'),glyph:glyph(e),overflow:e.scrollWidth>e.clientWidth+1})),
  viewportOverflow:document.documentElement.scrollWidth>innerWidth};
}"""

def check(ok, message):
    if not ok:
        failures.append(message)

with lib_browser.serve_docs(8993) as base, sync_playwright() as p:
    browser = p.chromium.launch()
    for name,w,h,sw,sh,dpr in cases:
        context = browser.new_context(viewport={'width':w,'height':h}, screen={'width':sw,'height':sh},
            device_scale_factor=dpr,has_touch=dpr>1,is_mobile=dpr>1,service_workers='block')
        page = context.new_page()
        errors=[]
        page.on('pageerror',lambda e: errors.append(str(e)))
        page.route('https://**/*',lambda r:r.abort())
        lib_browser.seed_local_storage(page, {'tabelog.lang':'zh-CN','tabelog.seenIntro':'1'})
        lib_browser.boot(page,base)
        for scale in (100,130,200):
            page.evaluate('''scale=>{
              document.documentElement.style.fontSize=(16*scale/100)+'px';
              App.set({fontScale:scale}); App.act.closeDetail();
              App.act.setTab('results'); App.layout.schedule();
            }''',scale)
            page.wait_for_timeout(200)
            tabs=page.locator('[data-ct="tab"]:visible').evaluate_all('(els)=>els.map(e=>e.dataset.tab)')
            check(tabs==['results','filters','saved'],f'{name}/{scale}: tabs {tabs}')
            page.evaluate('''() => {
              const r=Data.restaurants.find(r=>r.name.includes('らいもん')) || Data.restaurants[0];
              App.act.openDetail(r.id,'results');
            }''')
            page.wait_for_function('()=>!!Detail.detailFor(App.state.selected.id)')
            page.wait_for_timeout(550)
            for lang in ('zh','en','ja'):
                page.evaluate('lang=>App.i18n.setLang(lang)',lang)
                page.wait_for_timeout(180)
                data=page.evaluate(probe)
                data.update(case=name,scale=scale,lang=lang)
                records.append(data)
                if name=='inner932' and scale==130 and lang=='zh':
                    check(abs(data['budget']['y']-data['booking']['y'])<1,'inner932/130: compact booking forces budget wrap')
                if w>=750:
                    cells=data['cells']
                    check(len(cells)==3,f'{name}/{scale}/{lang}: summary cell count')
                    check(max(c['box']['y'] for c in cells)-min(c['box']['y'] for c in cells)<=1.1,
                          f'{name}/{scale}/{lang}: summary cells left the row')
                    check(max(c['box']['h'] for c in cells)-min(c['box']['h'] for c in cells)<=2.1,
                          f'{name}/{scale}/{lang}: summary cells lost equal height')
                    check(max(c['centerDelta'] for c in cells)<=1,
                          f'{name}/{scale}/{lang}: summary content not centered')
                    check(data['booking']['w']>=104*lib_browser.ui_z(page)-1,
                          f'{name}/{scale}/{lang}: booking column squeezed')
                check(not data['viewportOverflow'],f'{name}/{scale}/{lang}: page overflow')
                check(all(v['sw']<=v['w']+1 for v in data['values']),f'{name}/{scale}/{lang}: budget/status clipped')
                if w<1100:
                    check(data['titleTools'] is not None and abs((data['titleName']['y']+data['titleName']['h']/2)-(data['titleTools']['y']+data['titleTools']['h']/2))<=1,
                          f'{name}/{scale}/{lang}: title and tools not center-aligned')
                    check(not data['stack'],f'{name}/{scale}/{lang}: actions stacked')
                    check(data['header']['h']==0,f'{name}/{scale}/{lang}: separate back header')
                    check(data['back'] is not None,f'{name}/{scale}/{lang}: missing back')
                    touch=44*lib_browser.ui_z(page)-0.1  # 4.2.4 UI density
                    check(all(a['label'] and a['box']['w']>=touch and a['box']['h']>=touch and not a['overflow'] and a['glyph'] for a in data['actions']),f'{name}/{scale}/{lang}: action target/name/overflow')
                    check(max(a['box']['y'] for a in data['actions'])-min(a['box']['y'] for a in data['actions'])<1,f'{name}/{scale}/{lang}: action rows')
            if w>=750:
                for combo in (['¥10k–20k','¥10k–20k','Link available'],
                              ['Not provided','Not provided','No link']):
                    synthetic=page.evaluate('''values=>{
                      const nodes=[...document.querySelectorAll('.dt-summary .dt-sum-val')];
                      nodes.forEach((n,i)=>n.textContent=values[i]);
                      const sum=document.querySelector('.dt-summary'),cells=[...sum.querySelectorAll('.dt-sum-cell')];
                      const box=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height,b:r.bottom}};
                      return {values:nodes.map(e=>({w:e.clientWidth,sw:e.scrollWidth})),cells:cells.map(box),overflow:sum.scrollWidth>sum.clientWidth+1};
                    }''',combo)
                    records.append({'case':name,'scale':scale,'synthetic':combo,**synthetic})
                    check(not synthetic['overflow'] and all(v['sw']<=v['w']+1 for v in synthetic['values']),
                          f'{name}/{scale}/{combo}: possible summary value overflow')
                    check(max(c['y'] for c in synthetic['cells'])-min(c['y'] for c in synthetic['cells'])<=1.1,
                          f'{name}/{scale}/{combo}: possible summary value left the row')
            if scale in (100,200) or (name=='inner932' and scale==130):
                page.evaluate("()=>App.i18n.setLang('zh')")
                page.wait_for_timeout(150)
                page.screenshot(path=str(args.output/f'{name}-{scale}-top.png'))
                page.evaluate("()=>{const sc=Containers.scroller('detail');const e=document.querySelector('.dt-summary');sc.scrollTop+=e.getBoundingClientRect().top-sc.getBoundingClientRect().top-document.querySelector('.dt-title-row').offsetHeight-8;}")
                page.wait_for_timeout(120)
                page.screenshot(path=str(args.output/f'{name}-{scale}-budget.png'))
                if w<1100:
                    sticky=page.evaluate('''()=>{const e=document.querySelector('[data-ct="back"]'),r=e.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return e.contains(hit)||e===hit}''')
                    check(sticky,f'{name}/{scale}: back not reachable while scrolling')
            if w<1100:
                page.locator('#detail-root [data-ct="back"]').click()
                page.wait_for_timeout(180)
                check(page.evaluate('()=>!App.state.selected.id'),f'{name}/{scale}: back failed')
        if w<1100:
            page.evaluate('''()=>{
              document.documentElement.style.fontSize='32px';App.set({fontScale:200});
              const r=Data.restaurants.find(r=>r.name.includes('AZABUDAIHILLS')) || Data.restaurants.reduce((a,b)=>a.name.length>b.name.length?a:b);
              App.act.openDetail(r.id,'results');
            }''')
            page.wait_for_timeout(300)
            page.screenshot(path=str(args.output/f'{name}-200-long-name.png'))
            data=page.evaluate(probe)
            records.append({'case':name,'longName':True,**data})
            check(data['body']['h']-data['title']['h']>=data['minReading']-1,f'{name}: long title leaves no reading room')
        if name=='phone402':
            for inset,offset in ((0,0),(40,0),(42,0),(0,30),(40,30)):
                page.evaluate('''([inset,offset])=>{
                  document.documentElement.style.setProperty('--app-inset-top',inset+'px');
                  Object.defineProperty(window.visualViewport,'offsetTop',{value:offset,configurable:true});
                  App.layout.schedule();
                }''',[inset,offset])
                page.wait_for_timeout(120)
                safe=page.evaluate('''()=>{const s=getComputedStyle(document.querySelector('#app'),'::before');return {top:parseFloat(s.top),height:parseFloat(s.height),background:s.backgroundColor,pointer:s.pointerEvents,U:App.state.layout.U.y}}''')
                records.append({'case':'safe','inset':inset,'offset':offset,**safe})
                check(safe['height']==inset and safe['top']==offset and safe['U']==inset+offset and safe['pointer']=='none' and safe['background']=='rgb(255, 255, 255)',f'inset {inset}/{offset}: {safe}')
            page.evaluate('''()=>document.querySelectorAll('.dt-mapmark img,.dt-tabelog img').forEach(e=>e.src='/missing-brand-image.png')''')
            page.wait_for_timeout(80)
            data=page.evaluate(probe)
            check(all(a['glyph'] for a in data['actions']),'failed brand images leave blank actions')
            page.screenshot(path=str(args.output/'phone402-brand-failure.png'))
            # Same row survives selection and font-pressure changes, with no stale compact state.
            page.evaluate("()=>{document.documentElement.style.fontSize='16px';App.set({fontScale:100});App.act.openDetail(Data.restaurants[1].id,'marker');}")
            page.wait_for_timeout(200)
            check(page.locator('#detail-root [data-ct="back"]').count()==0,'marker source invents back')
        check(not errors,f'{name}: page errors {errors}')
        context.close()
    browser.close()
(args.output/'results.json').write_text(json.dumps({'records':records,'failures':failures},ensure_ascii=False,indent=2))
print(json.dumps({'records':len(records),'failures':failures},ensure_ascii=False))
raise SystemExit(bool(failures))
