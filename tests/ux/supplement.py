"""Focused UX checks for keyboard, resize and first-visit map interactions."""
import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tests'))
import lib_browser
from playwright.sync_api import sync_playwright

parser=argparse.ArgumentParser()
parser.add_argument('--docs',type=Path,default=ROOT/'docs')
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--browser',choices=['webkit','chromium'],default='webkit')
args=parser.parse_args()
args.output.mkdir(parents=True,exist_ok=True)
lib_browser.DOCS=args.docs.resolve()
records=[]
with lib_browser.serve_docs(8977) as base,sync_playwright() as p:
    browser=getattr(p,args.browser).launch()
    context=browser.new_context(viewport={'width':475,'height':751},is_mobile=True,has_touch=True,service_workers='block')
    page=context.new_page()
    page.set_default_timeout(8000)
    page.route('https://**/*',lambda r:r.abort())
    page.add_init_script("localStorage.setItem('tabelog.lang','zh-CN');localStorage.setItem('tabelog.seenIntro','1')")
    lib_browser.boot(page,base)
    lib_browser.phone_tab(page,'results')
    page.locator('#wb-list .wb-row').first.click()
    page.wait_for_selector('#bs-sheet.bs-open')
    page.locator('#ux-detail-back').focus()
    page.keyboard.press('f')
    assert page.locator('#ux-detail-actions .ff-fav-btn').get_attribute('aria-pressed')=='true'
    page.keyboard.press('f')
    assert page.locator('#ux-detail-actions .ff-fav-btn').get_attribute('aria-pressed')=='false'
    page.keyboard.press('f')
    page.wait_for_timeout(300)
    assert page.locator('.sync-toast-msg').filter(has_text='登录可在其他设备恢复').count()==0, 'anonymous hint overlaps the explicit action'
    page.evaluate("window.uxContentNode=document.getElementById('bs-content')")
    for w,h in [(932,704),(591,689),(475,751)]:
        page.set_viewport_size({'width':w,'height':h})
        page.wait_for_timeout(250)
        assert page.evaluate("window.uxContentNode===document.getElementById('bs-content')"), 'detail was replaced'
        page.locator('#ux-detail-actions .ff-fav-btn').click(trial=True)
        page.locator('#ux-detail-actions .rst-gmaps').click(trial=True)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(args.output/f'{args.browser}-resize-{w}.png'))
    page.keyboard.press('Escape')
    page.wait_for_timeout(200)
    assert page.locator('#wb-list').is_visible(), 'Escape failed to return to the source'
    lib_browser.phone_tab(page,'fav')
    page.locator('#fv-new').click()
    page.locator('#fl-name').fill('Offset viewport test')
    page.evaluate("""()=>{Object.defineProperty(visualViewport,'height',{configurable:true,value:220});
      Object.defineProperty(visualViewport,'offsetTop',{configurable:true,value:40});visualViewport.dispatchEvent(new Event('resize'));
      let e=document.createElement('div');e.id='test-keyboard';e.style.cssText='position:fixed;z-index:999999;left:0;right:0;top:260px;bottom:0;background:#c9ccd1';document.body.append(e)}""")
    page.locator('#fl-modal .fl-save').click(trial=True)
    box=page.locator('#fl-modal').bounding_box()
    assert box['y']>=40 and box['y']+box['height']<=260
    page.screenshot(path=str(args.output/f'{args.browser}-keyboard-offset.png'))
    page.locator('#fl-modal .fl-save').click()
    records.append({'case':'keyboard-shortcuts-resize-offset','pass':True,'syntheticViewport':True})
    context.close()

    context=browser.new_context(viewport={'width':667,'height':375},is_mobile=True,has_touch=True,service_workers='block')
    page=context.new_page()
    page.set_default_timeout(8000)
    page.route('https://**/*',lambda r:r.abort())
    page.add_init_script("localStorage.setItem('tabelog.lang','en')")
    lib_browser.boot(page,base)
    page.wait_for_selector('#intro-bar:not([hidden])')
    page.mouse.click(333,260,button='right')
    page.wait_for_selector('#ff-add-bm')
    assert page.locator('#intro-bar').is_hidden()
    page.locator('#ff-add-bm').click()
    page.locator('#bm-modal .bm-cancel').click()
    page.screenshot(path=str(args.output/f'{args.browser}-intro-contextmenu.png'))
    records.append({'case':'first-visit-contextmenu-real-pointer','pass':True,'nativeLongPress':False})
    context.close()
    browser.close()
(args.output/'results.json').write_text(json.dumps(records,indent=2))
print(json.dumps(records,indent=2))
