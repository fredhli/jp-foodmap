// Offline fault injection. Source overlay avoids competing with the release build.
// Run with --built after the release owner generates docs/index.html.
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import {runPendingCases} from './pending-tabs.mjs';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
const require = createRequire(import.meta.url);
let playwright;
try {
  playwright = require('playwright');
} catch (error) {
  if (error.code !== 'MODULE_NOT_FOUND') throw error;
  // Reuse the Python dependency already declared by this project.
  const python = process.env.PYTHON || ['.venv-wsl/bin/python', '.venv/bin/python', '.venv/Scripts/python.exe']
    .map(p => path.resolve(p)).find(p => fs.existsSync(p)) || (process.platform === 'win32' ? 'python' : 'python3');
  const driver = execFileSync(python, ['-c',
    'import pathlib,playwright;print(pathlib.Path(playwright.__file__).parent/"driver"/"package")'],
    {encoding: 'utf8'}).trim();
  playwright = require(driver);
}
const {chromium} = playwright;
const root = process.cwd(), docs = path.join(root,'docs');
const src = fs.readFileSync(path.join(root,'src/tabelog/scrape/map.py'),'utf8');
let html = fs.readFileSync(path.join(docs,'index.html'),'utf8');
if (!process.argv.includes('--built')) {
  for (const [start,end] of [
    ['  function loadPopups()', '  // ===== Apple-style'],
    ['  function fetchAuthed(', '  // Bounded dependency wait.'],
    ['    var pushInFlight = false,', '    // Flash the filter FAB'],
    ['    function adoptDiskSyncBase()', '    // True for the single retry'],
    ['    var pullRetriedAfterSilent = false;', '    // M-027 / B4:'],
    ['    function downloadBackup()', '    impModal.querySelector(\'.imp-confirm\')'],
    ['    function fallbackSilentGIS(cb)', '    // Boot-time restore'],
  ]) {
    const a=html.indexOf(start), b=html.indexOf(end,a), c=src.indexOf(start), d=src.indexOf(end,c);
    assert.ok(Math.min(a,b,c,d)>=0,start);
    html=html.slice(0,a)+src.slice(c,d)+html.slice(b);
  }
}
const anchor='    function schedulePush() {';
html=html.replace(anchor, `
window.__reliability = {
 pull, push, toggleFav, normalizeImport, openImportModal, doImport, flushOnHide, loadPopups, downloadBackup, tryRestoreSession, reconcile,
 favoriteRefs: function(){return favBuildGroups().flatMap(function(g){return g.items.map(function(i){return i.ref;});});},
 auth: function(kind){ window.__authResult='waiting';
   (kind==='me' ? tryMe : function(cb){exchangeForSession('fake-token',cb);})(function(ok){window.__authResult=ok;}); },
 stop: function(){clearInterval(pollTimer);clearTimeout(pushTimer);},
 bookmark: function(b){bookmarks.length=0;bookmarks.push(b);schedulePush();},
 editSnapshot: function(s){state.fav=new Set(s.favorites);state.black=new Set(s.blacklist);
   replaceBookmarksArray(s.bookmarks);schedulePush();},
 snapshot: function(){return {fav:Array.from(state.fav),black:Array.from(state.black),
   bookmarks:JSON.parse(JSON.stringify(bookmarks)),dirty,base:syncBase,pending:pendingWrite,
   pullInFlight,pushInFlight,retryAt,waitingForCloud,rejectedContent,
   status:(document.getElementById('ff-sync-status')||{}).textContent||'',
   cache:localStorage.getItem(CACHE_KEY),bms:localStorage.getItem(BM_KEY)};}
};
`+anchor);
const rows=JSON.parse(fs.readFileSync(path.join(docs,'data/restaurants.json'),'utf8'));
const A=rows[0].detail_url,B=rows[1].detail_url;
const origin='http://127.0.0.1:8974';
const results=[];
const browser=await chromium.launch({headless:true});
async function open(base={v:5,w:'confirmed',favorites:[A],blacklist:[],bookmarks:[]}) {
  const ctx=await browser.newContext({serviceWorkers:'block',viewport:{width:416,height:657}});
  await ctx.route('**/*',route=>{
    const u=new URL(route.request().url());
    if(u.origin!==origin) return route.abort();
    if(u.pathname==='/') return route.fulfill({contentType:'text/html',body:html});
    const p=path.resolve(docs,'.'+decodeURIComponent(u.pathname));
    if(!p.startsWith(docs+'/')||!fs.existsSync(p)) return route.fulfill({status:404,body:''});
    return route.fulfill({contentType:({'.js':'text/javascript','.css':'text/css','.json':'application/json','.png':'image/png'})[path.extname(p)]||'application/octet-stream',body:fs.readFileSync(p)});
  });
  await ctx.addInitScript(({base})=>{
    const auth={sub:'test-only',email:'nobody@example.invalid',exp:Date.now()+86400000};
    localStorage.setItem('tabelog.auth',JSON.stringify(auth));
    localStorage.setItem('tabelog.lang','zh-CN');
    localStorage.setItem('tabelog.syncBase',JSON.stringify({...base,sub:auth.sub}));
    localStorage.setItem('omakase_state_cache_v2',JSON.stringify({fav:base.favorites,black:[],dirty:false}));
    localStorage.setItem('tabelog.bookmarks',JSON.stringify(base.bookmarks));
    window.__net={mode:'ok',remote:base,requests:[],authHang:false,getHang:false,retryAfter:'2'};
    const f=window.fetch.bind(window);
    window.fetch=(url,opts={})=>{
      if(typeof url!=='string'||!url.startsWith('https://api.jpfoodmap.com/api')) return f(url,opts);
      const n=window.__net,method=opts.method||'GET';
      n.requests.push({url,method,body:opts.body,signal:!!opts.signal,keepalive:!!opts.keepalive,at:Date.now()});
      const bodyHang=()=>Promise.resolve(new Response(new ReadableStream({start(c){c.enqueue(new TextEncoder().encode('{'));}})));
      if(url.endsWith('/me')||url.endsWith('/session')) return n.authHang?bodyHang():Promise.resolve(new Response(JSON.stringify(auth)));
      if(method==='GET') return n.getHang?bodyHang():Promise.resolve(new Response(JSON.stringify(n.remote)));
      if(n.mode==='hang') return new Promise(()=>{});
      if(n.mode==='bodyhang') return bodyHang();
      if(n.mode==='409') return Promise.resolve(new Response(JSON.stringify(n.remote),{status:409}));
      if(['413','429','503'].includes(n.mode)) return Promise.resolve(new Response('{}',{status:Number(n.mode),headers:{'Retry-After':n.retryAfter}}));
      const b=JSON.parse(opts.body); n.remote={...b,v:(n.remote.v||0)+1};delete n.remote.baseV;
      return Promise.resolve(new Response(JSON.stringify({v:n.remote.v})));
    };
  },{base});
  const page=await ctx.newPage(); const errors=[];page.on('pageerror',e=>errors.push(String(e)));
  await page.goto(origin,{waitUntil:'domcontentloaded'});
  await page.waitForFunction(()=>window.__reliability&&!__reliability.snapshot().pullInFlight);
  await page.evaluate(()=>__reliability.stop());
  await page.clock.install();
  return {ctx,page,errors};
}
const pause=p=>p.waitForTimeout(40);
const snap=p=>p.evaluate(()=>__reliability.snapshot());
const net=p=>p.evaluate(()=>__net.requests);
async function test(name,fn){if(process.env.CASE_FILTER&&!name.includes(process.env.CASE_FILTER))return;const c=await open();try{await fn(c.page);assert.deepEqual(c.errors,[]);results.push({name,pass:true});}finally{await c.ctx.close();}}
try {
  await test('lower GET keeps confirmed data; later deletion propagates',async p=>{
    await p.evaluate(()=>{__net.remote={v:4,w:'old',favorites:[],blacklist:[],bookmarks:[]};__reliability.pull(true);});await pause(p);
    let s=await snap(p);assert.deepEqual(s.fav,[A]);assert.equal(s.base.v,5);assert.ok(s.waitingForCloud);
    await p.evaluate(()=>{__net.remote={v:6,w:'new',favorites:[],blacklist:[],bookmarks:[]};});
    await p.clock.runFor(5600);await pause(p);s=await snap(p);assert.deepEqual(s.fav,[]);assert.equal(s.base.v,6);
  });
  // BE-D (P1-2): a server that stays rolled back — a restored KV backup, not
  // a stale read — is eventually accepted. 3.1.x only had the brake: the
  // device stayed waitingForCloud forever and never pushed again. Acceptance
  // unions (never merges against the old base), so a rollback can resurrect a
  // deletion but can never drop anything this device holds.
  await test('a persistent server rollback is accepted, unions, and resumes pushing',async p=>{
    await p.evaluate(()=>{__net.remote={v:4,w:'old',favorites:[],blacklist:[],bookmarks:[]};__reliability.pull(true);});await pause(p);
    let s=await snap(p);assert.deepEqual(s.fav,[A]);assert.ok(s.waitingForCloud);
    assert.equal((await net(p)).filter(r=>r.method==='PUT').length,0);
    for(let i=0;i<4;i++){await p.clock.runFor(61100);await p.evaluate(()=>__reliability.pull(true));await pause(p);}
    s=await snap(p);
    assert.equal(s.waitingForCloud,false);
    assert.deepEqual(s.fav,[A]);assert.equal(s.dirty,false);
    assert.equal((await net(p)).filter(r=>r.method==='PUT').length,1);
    assert.deepEqual(await p.evaluate(()=>__net.remote.favorites),[A]);
  });
  await test('lower 409 never rebases or deletes',async p=>{
    await p.evaluate(B=>{__net.mode='409';__net.remote={v:4,w:'old',favorites:[],blacklist:[],bookmarks:[]};__reliability.toggleFav(B);__reliability.push();},B);
    await pause(p);const s=await snap(p);assert.equal(s.base.v,5);assert.deepEqual(new Set(s.fav),new Set([A,B]));assert.ok(s.dirty);
  });
  for(const mode of ['hang','bodyhang']) await test(mode+' PUT unlocks, preserves identity, confirms lost response',async p=>{
    await p.evaluate(({B,mode})=>{__net.mode=mode;__reliability.toggleFav(B);__reliability.push();},{B,mode});await pause(p);
    await p.clock.runFor(15100);await pause(p);let s=await snap(p);
    assert.equal(s.pushInFlight,false);assert.ok(s.pending&&s.dirty);assert.equal((await net(p)).filter(r=>r.method==='PUT').length,1);
    await p.evaluate(()=>{const b=JSON.parse(__reliability.snapshot().pending.body);__net.remote={...b,v:6};__net.mode='ok';});
    await p.clock.runFor(5600);await pause(p);s=await snap(p);assert.equal(s.pending,null);assert.equal(s.dirty,false);assert.equal(s.base.v,6);
    assert.equal((await net(p)).filter(r=>r.method==='PUT').length,1);
  });
  // BE-A: 3.1.x replayed the identical body under the identical write id.
  // 3.1.2 declares the write lost once the server has shown its own base back
  // three times over the dwell window, and sends the CURRENT state under a
  // fresh w — a replay can duplicate a write that did land, a fresh push
  // cannot lose anything.
  await test('uncommitted PUT proven lost is re-sent as a fresh write',async p=>{
    await p.evaluate(B=>{__net.mode='hang';__reliability.toggleFav(B);__reliability.push();},B);await pause(p);
    await p.clock.runFor(15100);await pause(p);
    await p.evaluate(()=>{__net.mode='ok';});
    for (const ms of [5600,16100,61100]) {await p.clock.runFor(ms);await pause(p);}
    const puts=(await net(p)).filter(r=>r.method==='PUT');
    assert.equal(puts.length,2);
    assert.notEqual(JSON.parse(puts[1].body).w,JSON.parse(puts[0].body).w);
    assert.deepEqual(new Set(JSON.parse(puts[1].body).favorites),new Set([A,B]));
    assert.equal((await snap(p)).dirty,false);assert.equal((await snap(p)).pending,null);
  });
  // BE-B: the user-facing escape valve. Hidden while sync is healthy, shown
  // once the engine is holding an uncertain write, and one click resolves it
  // without waiting out the dwell window.
  await test('manual retry appears only when stuck and unsticks the device',async p=>{
    const btn='#ssm-retry-sync';
    assert.equal(await p.evaluate(s=>document.querySelector(s).hidden,btn),true);
    await p.evaluate(B=>{__net.mode='hang';__reliability.toggleFav(B);__reliability.push();},B);await pause(p);
    await p.clock.runFor(15100);await pause(p);
    assert.equal(await p.evaluate(s=>document.querySelector(s).hidden,btn),false);
    await p.evaluate(()=>{__net.mode='ok';});
    await p.evaluate(s=>document.querySelector(s).click(),btn);await pause(p);await pause(p);
    const s=await snap(p);
    assert.equal(s.pending,null);assert.equal(s.dirty,false);assert.equal(s.waitingForCloud,false);
    assert.equal((await net(p)).filter(r=>r.method==='PUT').length,2);
    assert.deepEqual(new Set(await p.evaluate(()=>__net.remote.favorites)),new Set([A,B]));
    assert.equal(await p.evaluate(s=>document.querySelector(s).hidden,btn),true);
  });
  await test('GET body and auth body deadline release callbacks',async p=>{
    await p.evaluate(()=>{__net.getHang=true;__reliability.pull(true);__net.authHang=true;__reliability.auth('me');});await pause(p);
    await p.clock.runFor(15100);await pause(p);assert.equal((await snap(p)).pullInFlight,false);assert.equal(await p.evaluate(()=>__authResult),false);
    await p.evaluate(()=>__reliability.auth('session'));await pause(p);await p.clock.runFor(15100);await pause(p);assert.equal(await p.evaluate(()=>__authResult),false);
  });
  await test('expired cached session survives offline probe and restores online',async p=>{
    await p.evaluate(()=>{const a=JSON.parse(localStorage.getItem('tabelog.auth'));a.exp=Date.now()-1;localStorage.setItem('tabelog.auth',JSON.stringify(a));
      __net.authHang=true;__reliability.tryRestoreSession();});await pause(p);
    await p.clock.runFor(15100);await pause(p);
    assert.equal(await p.evaluate(()=>JSON.parse(localStorage.getItem('tabelog.auth')).sub),'test-only');
    await p.evaluate(()=>{__net.authHang=false;window.dispatchEvent(new Event('online'));});await pause(p);
    assert.ok(await p.evaluate(()=>JSON.parse(localStorage.getItem('tabelog.auth')).exp>Date.now()));
  });
  await test('unknown write readback keeps edits made during the request',async p=>{
    await p.evaluate(B=>{__net.mode='hang';__reliability.toggleFav(B);__reliability.push();},B);await pause(p);
    await p.evaluate(A=>{__reliability.toggleFav(A);},A);
    await p.clock.runFor(15100);await pause(p);
    await p.evaluate(()=>{const b=JSON.parse(__reliability.snapshot().pending.body);__net.remote={...b,v:6};__net.mode='ok';});
    await p.clock.runFor(5600);await pause(p);
    const s=await snap(p);assert.deepEqual(s.fav,[B]);assert.equal(s.dirty,false);assert.equal(s.base.v,7);
  });
  await test('413 same state blocks writes while GET remains live; edit resumes',async p=>{
    await p.evaluate(B=>{__net.mode='413';__reliability.toggleFav(B);__reliability.push();},B);await pause(p);
    for(let i=0;i<4;i++){await p.evaluate(()=>__reliability.pull(true));await pause(p);}
    let req=await net(p);assert.equal(req.filter(r=>r.method==='PUT').length,1);assert.ok(req.filter(r=>r.url.endsWith('/state')&&r.method==='GET').length>=5);
    await p.evaluate(A=>{__net.mode='ok';__reliability.toggleFav(A);__reliability.push();},A);await pause(p);assert.equal((await snap(p)).dirty,false);
  });
  for(const code of ['429','503']) await test(code+' honors Retry-After without stacked sends',async p=>{
    await p.evaluate(({B,code})=>{__net.mode=code;__net.retryAfter='10';__reliability.toggleFav(B);__reliability.push();},{B,code});await pause(p);
    await p.evaluate(()=>{for(let i=0;i<10;i++){__reliability.push();__reliability.pull(true);}});
    await p.clock.runFor(9900);assert.equal((await net(p)).filter(r=>r.method==='PUT').length,1);
    await p.evaluate(()=>{__net.mode='ok';});await p.clock.runFor(1200);await pause(p);assert.equal((await snap(p)).dirty,false);
  });
  await test('UTF8 oversized Chinese data is rejected before transfer',async p=>{
    await p.evaluate(()=>{__reliability.bookmark({id:'bm-large',lat:35,lon:139,name:'字'.repeat(70000)});__reliability.push();});await pause(p);
    assert.equal((await net(p)).filter(r=>r.method==='PUT').length,0);assert.ok((await snap(p)).dirty);
    await p.evaluate(()=>{__reliability.bookmark({id:'bm-large',lat:35,lon:139,name:'字'.repeat(61000)});__reliability.push();});await pause(p);
    assert.equal((await net(p)).filter(r=>r.method==='PUT').length,1);assert.equal((await snap(p)).dirty,false);
  });
  // BE-C: a structurally broken FILE is still refused whole; a single broken
  // ENTRY inside a usable file is skipped and counted instead of costing the
  // user everything else in the backup.
  await test('import refuses broken files, skips broken entries; old and future fields survive',async p=>{
    const before=await snap(p);
    for(const data of [{favorites:{}},{nothing:'known'},'not an object',42])
      assert.equal(await p.evaluate(d=>__reliability.normalizeImport(d),data),null);
    for(const [data,favN,skipped] of [[{favorites:[{url:{x:1}}]},0,1],
        [{bookmarks:[{id:'bad',lat:35,lon:139,name:{toString:5}}]},0,1],
        [{favorites:[B],bookmarks:[{id:'list:broken',category:'meta',kind:'member',list:{},ref:B}]},1,1]]) {
      const n=await p.evaluate(d=>__reliability.normalizeImport(d),data);
      assert.equal(n.favorites.length,favN);assert.equal(n.skipped,skipped);assert.deepEqual(n.bookmarks,[]);
    }
    assert.deepEqual(await snap(p),before);
    // The unreadable bookmark must not reach state even when the rest imports.
    await p.evaluate(B=>{__reliability.openImportModal(__reliability.normalizeImport(
      {favorites:[B],blacklist:[],bookmarks:[{id:'bad',lat:35,lon:139,name:{toString:5}}]}));__reliability.doImport();},B);
    const partial=await snap(p);
    assert.ok(partial.fav.includes(B));assert.deepEqual(partial.bookmarks,before.bookmarks);
    const payload={fav:[{detail_url:B}],black:[],bookmarks:[{id:'bm-old',lat:35,lon:139,name:'Old',future:{keep:true}},
      {id:'list:test',category:'meta',kind:'list',name:'Trip'},
      {id:'lm:test:ref',category:'meta',kind:'member',list:'list:test',ref:B},
      {id:'fb-tokyo',category:'hidden'}]};
    await p.evaluate(data=>{const n=__reliability.normalizeImport(data);__reliability.openImportModal(n);__reliability.doImport();},payload);
    const s=await snap(p);assert.ok(s.fav.includes(B));assert.equal(s.bookmarks.find(b=>b.id==='bm-old').future.keep,true);assert.equal(s.bookmarks.length,4);
  });
  await test('same version other writer and versionless server remain compatible',async p=>{
    await p.evaluate(B=>{__net.remote={v:5,w:'other',favorites:[B],blacklist:[],bookmarks:[]};__reliability.pull(true);},B);await pause(p);
    assert.deepEqual(new Set((await snap(p)).fav),new Set([A,B]));
    await p.evaluate(()=>{__net.remote={favorites:[],blacklist:[],bookmarks:[]};__reliability.pull(true);});await pause(p);
    assert.deepEqual(new Set((await snap(p)).fav),new Set([A,B]));
  });
  await test('keepalive has no foreground abort and records uncertain identity',async p=>{
    await p.evaluate(B=>{__reliability.toggleFav(B);__reliability.flushOnHide();},B);await pause(p);
    const r=(await net(p)).find(r=>r.keepalive);assert.ok(r);assert.equal(r.signal,false);
    assert.ok((await snap(p)).pending);assert.ok((await snap(p)).dirty);
  });
  results.push(...await runPendingCases({browser,html,docs,rows}));
} catch(error) {results.push({name:'failure',pass:false,error:String(error.stack)});process.exitCode=1;}
finally {await browser.close();console.log(JSON.stringify(results,null,2));}
