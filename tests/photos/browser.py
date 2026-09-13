"""Bounded image recovery across every photo surface; no external requests."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lib_browser
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
helper = (ROOT / 'src/tabelog/ui/js/core.js').read_text().split('/* Photo variants', 1)[1]
helper = '/* Photo variants' + helper
BASE = 'https://tblg.k-img.com/restaurant/images/Rvw/123/'
classes = ['dt-photo-img', 'dt-cand-img', 'dt-lb-img', 'ls-img', 'mp-bubble-photo']
requests = []
signed = 'https://tblg.k-img.com/resize/660x370c/restaurant/images/Rvw/123/signed.jpg?token=abc&x=1'
with lib_browser.serve_docs(8978) as base, sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    def respond(route):
        url = route.request.url
        requests.append(url)
        success = ('320x320_rect_working.jpg' in url or '640x640_rect_valid.jpg' in url or url == signed)
        route.fulfill(status=200 if success else 404,
                      content_type='image/png' if success else 'text/plain',
                      body=(ROOT / 'docs/img/google-maps-v2.png').read_bytes() if success else b'missing')
    page.route('https://**/*', lambda r: r.abort())
    page.route('https://tblg.k-img.com/**', respond)
    page.goto(base + '/manifest.json')
    page.set_content('<main></main>')
    page.add_script_tag(content=helper)
    def add(name, photo):
        return page.evaluate('''([name,url])=>{
          let im=document.createElement('img');im.className=name;im.alt='';
          im.style.cssText='width:80px;height:56px';im.src=PhotoUrls.url(url,640);
          document.querySelector('main').append(im);return document.images.length-1;
        }''', [name, photo])
    for name in classes:
        i = add(name, BASE + '640x640_rect_working.jpg')
        page.wait_for_function('(i)=>document.images[i].naturalWidth>0', arg=i)
        assert page.evaluate('(i)=>document.images[i].src', i) == BASE + '320x320_rect_working.jpg'
    assert requests.count(BASE + '640x640_rect_working.jpg') == 1, requests
    i = add('dt-photo-img', BASE + '640x640_rect_valid.jpg')
    page.wait_for_function('(i)=>document.images[i].naturalWidth>0', arg=i)
    assert BASE + '320x320_rect_valid.jpg' not in requests
    before = len(requests)
    i = add('dt-cand-img', BASE + '640x640_rect_failed.jpg')
    page.wait_for_function('(i)=>document.images[i].dataset.photoFailed', arg=i)
    assert len(requests) - before == 3, requests[before:]
    assert page.evaluate('(i)=>document.images[i].getBoundingClientRect().height', i) == 56
    i = add('dt-lb-img', signed)
    page.wait_for_function('(i)=>document.images[i].naturalWidth>0', arg=i)
    assert page.evaluate('(i)=>document.images[i].src', i) == signed
    # The built page must offer recovery after every size failed in its lightbox.
    ctx = browser.new_context(service_workers='block', viewport={'width': 932, 'height': 704})
    app = ctx.new_page()
    app.route('https://**/*', lambda r: r.abort())
    recovered = {'ok': False}
    def app_photo(route):
        route.fulfill(status=200 if recovered['ok'] else 404, content_type='image/png',
                      body=(ROOT / 'docs/img/google-maps-v2.png').read_bytes() if recovered['ok'] else b'missing')
    app.route('https://tblg.k-img.com/**', app_photo)
    lib_browser.seed_local_storage(app, {'tabelog.lang': 'zh', 'tabelog.seenIntro': '1'})
    lib_browser.boot(app, base)
    app.evaluate('App.act.openDetail(Data.restaurants.find(r=>r.id.includes("13275655")).id,"map")')
    app.wait_for_selector('.dt-photo-img')
    app.locator('.dt-photo').first.click()
    app.wait_for_selector('[data-lb="retry"]')
    assert app.locator('.dt-lb-img').evaluate('(e)=>e.getBoundingClientRect().height') > 100
    recovered['ok'] = True
    app.locator('[data-lb="retry"]').click()
    app.wait_for_function('document.querySelector(".dt-lb-img").naturalWidth>0')
    assert not app.locator('[data-lb="retry"]').count()
    app.locator('[data-lb="next"]').click()
    app.locator('[data-lb="next"]').click()
    app.wait_for_function('document.querySelector(".dt-lb-img").naturalWidth>0')
    browser.close()
print('All five photo surfaces, shared cache, valid 640, bounded failure, stable frame signed source recovery, built lightbox retry and rapid switch passed.')
