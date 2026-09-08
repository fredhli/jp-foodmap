import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';

// Real page functions, shared browser storage and a fictional process-local API.
export async function runPendingCases({browser,html,docs,rows}) {
  const [A,B,C,D]=rows.map(r=>r.detail_url), origin='https://jpfoodmap.com';
  const base={v:5,w:'base-5',sub:'alice',favorites:[A],blacklist:[],bookmarks:[]};
  const results=[];
  async function setup(options={}) {
    const initial=options.base===undefined?base:options.base;
    const ctx=await browser.newContext({serviceWorkers:'block',viewport:{width:416,height:657}});
    const server={mode:'ok',remote:initial||{v:0,favorites:[],blacklist:[],bookmarks:[]},requests:[],auth:'alice',...options.net};
    const errors=[];
    await ctx.route('**/*',route=>{
      const u=new URL(route.request().url());
      if(u.origin!==origin)return route.abort();
      if(u.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
      const p=path.resolve(docs,'.'+decodeURIComponent(u.pathname));
      if(!p.startsWith(docs+'/')||!fs.existsSync(p))return route.fulfill({status:404,body:''});
      return route.fulfill({body:fs.readFileSync(p),contentType:({'.js':'text/javascript','.css':'text/css','.json':'application/json','.png':'image/png'})[path.extname(p)]||'application/octet-stream'});
    });
    await ctx.exposeFunction('__pendingFetch',async({url,method='GET',body,tab,at})=>{
      server.requests.push({url,method,body,tab,at,auth:server.auth});
      if(url.endsWith('/me')||url.endsWith('/session'))return {body:JSON.stringify({sub:server.auth,email:server.auth+'@example.invalid',exp:Date.now()+86400000})};
      if(method==='GET')return server.getHang?{hang:true}:{body:JSON.stringify(server.remote)};
      if(server.mode==='hang')return {hang:true};
      // R5: the PUT dies the way an offline / DNS / blocked request does —
      // fetch rejects. The GET above still answers, which is exactly the
      // combination that wedged 3.1.1 (P0-1).
      if(server.mode==='neterr')return {neterr:true};
      if(server.mode==='conflictOnce'){
        server.mode='ok';server.remote=server.conflictRemote;
        return {status:409,body:JSON.stringify(server.remote)};
      }
      const b=JSON.parse(body);server.remote={...b,v:(server.remote.v||0)+1};delete server.remote.baseV;
      return {body:JSON.stringify({v:server.remote.v})};
    });
    await ctx.addInitScript(({initial,local})=>{
      if(!localStorage.getItem('__pendingSeeded')){
        localStorage.setItem('__pendingSeeded','1');
        localStorage.setItem('tabelog.auth',JSON.stringify({sub:'alice',exp:Date.now()+86400000}));
        localStorage.setItem('tabelog.seenIntro','1');localStorage.setItem('tabelog.lang','zh-CN');
        if(initial)localStorage.setItem('tabelog.syncBase',JSON.stringify(initial));
        localStorage.setItem('omakase_state_cache_v2',JSON.stringify(local||{fav:initial?.favorites||[],black:initial?.blacklist||[],dirty:false}));
        localStorage.setItem('tabelog.bookmarks',JSON.stringify(initial?.bookmarks||[]));
      }
      const f=fetch.bind(window);window.fetch=(url,opts={})=>{
        if(typeof url!=='string'||!url.startsWith('https://api.jpfoodmap.com/api'))return f(url,opts);
        return __pendingFetch({url,method:opts.method,body:opts.body,tab:window.name,at:Date.now()})
          .then(r=>r.hang?new Promise(()=>{})
                :r.neterr?Promise.reject(new TypeError('Failed to fetch'))
                :new Response(r.body,{status:r.status||200}));
      };
    },{initial,local:options.local});
    async function open(name) {
      const p=await ctx.newPage();p.on('pageerror',e=>errors.push(String(e)));
      await p.evaluate(n=>window.name=n,name);await p.goto(origin,{waitUntil:'domcontentloaded'});
      await p.waitForFunction(()=>window.__reliability);await p.waitForTimeout(100);
      await p.evaluate(()=>__reliability.stop());await p.clock.install();return p;
    }
    return {ctx,server,open,errors};
  }
  const settle=p=>p.waitForTimeout(100);
  const tick=async(p,ms)=>{await p.clock.fastForward(ms);await settle(p);};
  const snap=p=>p.evaluate(()=>({...__reliability.snapshot(),diskBase:localStorage.getItem('tabelog.syncBase'),diskPending:localStorage.getItem('tabelog.pendingWrite')}));
  async function synced(p) {
    for(let i=0;i<20;i++){
      const s=await snap(p);if(!s.dirty&&!s.pushInFlight&&!s.pending&&s.diskPending===null)return s;
      await settle(p);
    }
    assert.fail('Recovery upload did not settle within 2 seconds');
  }
  const puts=c=>c.server.requests.filter(r=>r.method==='PUT');
  async function test(name,fn,options) {
    if(process.env.CASE_FILTER&&!name.includes(process.env.CASE_FILTER))return;
    const c=await setup(options);
    try {await fn(c);assert.deepEqual(c.errors,[]);results.push({name,pass:true,puts:puts(c).map(r=>({tab:r.tab,auth:r.auth,w:JSON.parse(r.body).w}))});console.error('PASS '+name);}
    catch(error) {results.push({name,pass:false,error:String(error.stack),
      snapshots:await Promise.all(c.ctx.pages().map(p=>snap(p).catch(()=>null))),requests:c.server.requests});process.exitCode=1;}
    finally {await c.ctx.close();}
  }
  for(const legacy of [false,true]) await test('R1 first keepalive account switch'+(legacy?' repairs pending-only legacy state':''),async c=>{
    const p=await c.open('alice');
    await p.evaluate(A=>{__reliability.toggleFav(A);__reliability.flushOnHide();},A);await settle(p);
    const before=await snap(p);assert.equal(JSON.parse(before.diskBase).sub,'alice');assert.equal(before.pending.sub,'alice');
    if(legacy)await p.evaluate(()=>localStorage.removeItem('tabelog.syncBase'));
    c.server.auth='bob';c.server.mode='ok';c.server.getHang=false;
    c.server.remote={v:1,w:'bob-base',favorites:[B],blacklist:[],bookmarks:[]};
    await p.evaluate(()=>localStorage.setItem('tabelog.auth',JSON.stringify({sub:'bob',exp:Date.now()+86400000})));
    await p.reload({waitUntil:'domcontentloaded'});await p.waitForFunction(()=>window.__reliability);await settle(p);
    const s=await snap(p);assert.deepEqual(s.fav,[B]);assert.equal(s.base.sub,'bob');assert.equal(s.diskPending,null);
    assert.ok(puts(c).filter(r=>r.auth==='bob').every(r=>!JSON.parse(r.body).favorites.includes(A)));
  },{base:null,net:{getHang:true,mode:'hang'}});
  await test('R1 genuine anonymous favorites still seed first sign-in',async c=>{
    const p=await c.open('anonymous-seed');await settle(p);
    assert.deepEqual(new Set((await snap(p)).fav),new Set([A,B]));assert.equal((await snap(p)).dirty,false);
  },{base:null,local:{fav:[A],black:[],dirty:true},net:{remote:{v:1,w:'cloud',favorites:[B],blacklist:[],bookmarks:[]}}});
  await test('R1 same account reload confirms pending without a duplicate upload',async c=>{
    const p=await c.open('same-account');c.server.mode='hang';
    await p.evaluate(B=>{__reliability.toggleFav(B);__reliability.push();},B);await settle(p);
    const s=await snap(p);c.server.remote={...JSON.parse(s.pending.body),v:6};delete c.server.remote.baseV;c.server.mode='ok';
    await p.reload({waitUntil:'domcontentloaded'});await p.waitForFunction(()=>window.__reliability);await settle(p);
    assert.equal((await snap(p)).dirty,false);assert.equal((await snap(p)).diskPending,null);assert.equal(puts(c).length,1);
  });
  await test('R2 unknown PUT receives stable same-version other writer and later deletion',async c=>{
    const p=await c.open('pending-same-v');c.server.mode='hang';
    await p.evaluate(B=>{__reliability.toggleFav(B);__reliability.push();},B);await settle(p);await tick(p,15100);
    c.server.remote={...base,w:'other-5',favorites:[A,C]};c.server.mode='ok';
    await tick(p,5600);await tick(p,16100);await tick(p,5600);
    let s=await snap(p);assert.deepEqual(new Set(s.fav),new Set([A,B,C]));assert.equal(s.dirty,false);assert.equal(puts(c).length,2);
    assert.ok(JSON.parse(puts(c)[1].body).favorites.includes(C));
    c.server.remote={v:7,w:'true-delete',favorites:[C],blacklist:[],bookmarks:[]};
    await p.evaluate(()=>__reliability.pull(true));await settle(p);s=await snap(p);assert.deepEqual(s.fav,[C]);
  });
  await test('R2 unknown PUT retains compatibility with versionless Worker',async c=>{
    const p=await c.open('pending-legacy');c.server.mode='hang';
    await p.evaluate(B=>{__reliability.toggleFav(B);__reliability.push();},B);await settle(p);await tick(p,15100);
    c.server.remote={favorites:[C],blacklist:[],bookmarks:[]};c.server.mode='ok';
    await tick(p,5600);await tick(p,16100);await tick(p,5600);
    assert.deepEqual(new Set((await snap(p)).fav),new Set([A,B,C]));assert.equal((await snap(p)).dirty,false);
  });
  // BE-F: this case used to end with __reliability.resetWait() — the harness
  // calling clearSyncWait() by hand. The page never calls it on this path, so
  // what the case actually proved was "the test can unstick the engine", not
  // "the engine unsticks itself". No probe now: the tabs must converge on
  // their own, and the recovery upload must carry a NEW write id (3.1.2
  // discards a write proven lost instead of replaying its body).
  await test('R3 stale tabs recover without help and cannot revive cleared pending',async c=>{
    const a=await c.open('a');c.server.mode='hang';
    await a.evaluate(B=>{__reliability.toggleFav(B);__reliability.push();},B);await settle(a);
    const b=await c.open('b'),d=await c.open('c');await tick(a,15100);
    for(const ms of [5600,16100,61100])await tick(a,ms);
    assert.ok(puts(c).length>=2,'the lost write was never replaced');
    assert.notEqual(JSON.parse(puts(c)[1].body).w,JSON.parse(puts(c)[0].body).w);
    assert.equal(JSON.parse((await snap(a)).diskPending).attempts,1);
    c.server.remote={...JSON.parse(puts(c).at(-1).body),v:6};delete c.server.remote.baseV;c.server.mode='ok';
    for(const p of [a,b,d]){await tick(p,61100);await settle(p);}
    for(const p of [a,b,d])assert.equal((await snap(p)).diskPending,null);
    const settled=puts(c).length;
    await d.evaluate(D=>{__reliability.toggleFav(D);__reliability.push();},D);await settle(d);
    assert.equal(puts(c).length,settled+1);
    assert.notEqual(JSON.parse(puts(c).at(-1).body).w,JSON.parse(puts(c)[0].body).w);
    assert.deepEqual(new Set((await snap(d)).fav),new Set([A,B,D]));assert.equal((await snap(d)).dirty,false);
  });
  await test('R3 closed initiating tab hands pending recovery to another tab',async c=>{
    const a=await c.open('closed-owner');c.server.mode='hang';
    await a.evaluate(B=>{__reliability.toggleFav(B);__reliability.push();},B);await settle(a);
    const b=await c.open('successor');await a.close();c.server.mode='ok';
    for(const ms of [5600,16100,61100])await tick(b,ms);
    assert.equal(puts(c).length,2);
    assert.notEqual(JSON.parse(puts(c)[1].body).w,JSON.parse(puts(c)[0].body).w);
    assert.deepEqual(new Set(JSON.parse(puts(c)[1].body).favorites),new Set([A,B]));
    assert.equal((await snap(b)).dirty,false);assert.equal((await snap(b)).diskPending,null);
  });
  // BE-C: a non-URL entry is dropped and counted, never imported and never a
  // reason to refuse the rest of the file. The security property is unchanged:
  // nothing that is not an http(s) URL ever reaches state.fav.
  await test('R4 non-URL entries are skipped and counted; HTTP URLs and reference metadata remain compatible',async c=>{
    const p=await c.open('import-url');const before=await snap(p);
    for(const url of ['javascript:alert(1)','not-a-url','data:text/plain,hi','file:///tmp/x','/relative','https://','https://example.invalid/a b']){
      const data={favorites:[url],blacklist:[],bookmarks:[]};
      const n=await p.evaluate(d=>__reliability.normalizeImport(d),data);
      assert.deepEqual(n.favorites,[]);assert.equal(n.skipped,1);
      await p.evaluate(d=>{__reliability.openImportModal(__reliability.normalizeImport(d));__reliability.doImport();},data);
      assert.deepEqual((await snap(p)).fav,before.fav);assert.equal((await snap(p)).cache,before.cache);assert.equal((await snap(p)).bms,before.bms);
    }
    // The good half of a mixed file still imports.
    const mixed={favorites:[B,'javascript:alert(1)'],blacklist:[],bookmarks:[{id:'bm-sibling',lat:35,lon:139,name:'Sibling'}]};
    const mixedNorm=await p.evaluate(d=>__reliability.normalizeImport(d),mixed);
    assert.deepEqual(mixedNorm.favorites,[B]);assert.equal(mixedNorm.skipped,1);
    assert.equal(mixedNorm.bookmarks.length,1);
    const unknown='https://example.invalid/retired-place?x=1';
    const data={fav:['  '+B+'  ',{url:'http://example.invalid/old'},{detail_url:unknown}],black:[{url:' HTTPS://example.invalid/hidden '}],
      bookmarks:[{id:'bm-ref',lat:35,lon:139,name:'Pin',future:{keep:true}},
        {id:'list:trip',category:'meta',kind:'list',name:'Trip'},
        {id:'lm:trip:pin',category:'meta',kind:'member',list:'list:trip',ref:'bm-ref'},
        {id:'lm:trip:builtin',category:'meta',kind:'member',list:'list:trip',ref:'fb-tokyo'}]};
    const normalized=await p.evaluate(d=>__reliability.normalizeImport(d),data);
    assert.deepEqual(normalized.favorites,[B,'http://example.invalid/old',unknown]);assert.equal(normalized.blacklist[0],'https://example.invalid/hidden');
    assert.equal(normalized.bookmarks[2].ref,'bm-ref');assert.equal(normalized.bookmarks[3].ref,'fb-tokyo');
    await p.evaluate(n=>{__reliability.openImportModal(n);__reliability.doImport();},normalized);
    const s=await snap(p);assert.ok(s.fav.includes(unknown)&&s.fav.includes(B));assert.equal(s.bookmarks.find(b=>b.id==='bm-ref').future.keep,true);
    assert.ok((await p.evaluate(()=>__reliability.favoriteRefs())).includes(unknown));
  });
  // R5 (BE-F / P0-1) — the one combination the suite never had: the server
  // never moves AND every PUT dies with a network error. In 3.1.1 the retry
  // budget (attempts < 2, and navigator.locks required at all) ran out and
  // this device never sent another PUT for the rest of its life — across
  // reloads, across sign-outs. Transcribed from
  // audit_outputs/3.2.0-plan/backend/repro/wedge.mjs, verdict inverted.
  await test('R5 a frozen server plus failing PUTs never wedges the device',async c=>{
    const p=await c.open('wedge');c.server.mode='neterr';
    await p.evaluate(B=>{__reliability.toggleFav(B);__reliability.push();},B);await settle(p);
    assert.equal(puts(c).length,1);
    // Twenty minutes of polling with the network still broken.
    for(let i=0;i<20;i++){await tick(p,61100);await p.evaluate(()=>__reliability.pull(true));await settle(p);}
    const whileBroken=puts(c).length;
    assert.ok(whileBroken>1,'the device stopped sending entirely: '+whileBroken+' PUT(s)');
    assert.ok(whileBroken<12,'unbounded retries: '+whileBroken+' PUT(s) in 20 minutes');
    // Network returns and the user makes a fresh edit.
    c.server.mode='ok';
    await p.evaluate(C=>{__reliability.toggleFav(C);__reliability.push();},C);await settle(p);
    for(const ms of [5600,16100,61100,61100]){await tick(p,ms);await p.evaluate(()=>__reliability.pull(true));await settle(p);}
    const s=await snap(p);
    assert.ok(puts(c).length>whileBroken,'no PUT left the device after recovery');
    assert.deepEqual(new Set(s.fav),new Set([A,B,C]));
    assert.equal(s.dirty,false);assert.equal(s.diskPending,null);
    assert.deepEqual(new Set(c.server.remote.favorites),new Set([A,B,C]));
  });
  for(const newerDisk of [false,true])await test('R2 final post-pending added favorite deletion preserves remote addition'+(newerDisk?' with newer disk base':''),async c=>{
    const p=await c.open('cancel-new-favorite');c.server.mode='hang';
    await p.evaluate(B=>{__reliability.toggleFav(B);__reliability.push();},B);await settle(p);
    await p.evaluate(B=>__reliability.toggleFav(B),B);await tick(p,15100);
    assert.deepEqual((await snap(p)).fav,[A]);assert.equal((await snap(p)).dirty,true);
    if(newerDisk)await p.evaluate(({A,B})=>localStorage.setItem('tabelog.syncBase',
      JSON.stringify({sub:'alice',v:6,w:'confirmed-on-another-tab',favorites:[A,B],blacklist:[],bookmarks:[]})),{A,B});
    c.server.remote={...base,v:7,w:'other-after-v6',favorites:[A,B,C]};c.server.mode='ok';
    await tick(p,5600);
    const s=await synced(p);assert.deepEqual(new Set(s.fav),new Set([A,C]));assert.equal(s.dirty,false);
    assert.equal(puts(c).length,2);assert.deepEqual(new Set(JSON.parse(puts(c)[1].body).favorites),new Set([A,C]));
    assert.notEqual(JSON.parse(puts(c)[1].body).w,JSON.parse(puts(c)[0].body).w);
  });
  const R=rows[4].detail_url,E=rows[5].detail_url;
  const pin=(id,name=id)=>({id,lat:35,lon:139,name,future:{preserved:true}});
  const list=(id,name=id)=>({id:'list:'+id,category:'meta',kind:'list',name});
  const member=(id,l,ref=A)=>({id:'lm:'+id,category:'meta',kind:'member',list:'list:'+l,ref});
  const legacy={lat:35,lon:139,name:'Legacy without ID',future:{preserved:true}};
  const original=[pin('bm-anchor'),pin('bm-restore'),pin('bm-revert','Original'),
    list('stay'),member('stay','stay'),list('restore'),member('restore','restore'),legacy];
  const sent=[pin('bm-anchor'),pin('bm-cancel'),pin('bm-uncommitted'),pin('bm-revert','Sent edit'),
    list('stay','Sent name'),member('stay','stay'),list('cancel'),member('cancel','cancel'),legacy];
  const local=[pin('bm-anchor'),pin('bm-restore'),pin('bm-revert','Original'),pin('bm-uncommitted'),pin('bm-new'),
    list('stay','Local final name'),member('stay','stay','bm-new'),list('restore'),member('restore','restore'),
    list('new'),member('new','new','bm-new'),legacy];
  const remoteAdded=[pin('bm-remote'),list('remote'),member('remote','remote')];
  for(const version of ['higher','same','legacy','retry-conflict','matched'])for(const landed of [true,false]){
    if(version==='matched'&&!landed)continue;
    await test('R2 final '+version+' pending '+(landed?'landed':'uncommitted')+' preserves set and full bookmark deltas',async c=>{
      const p=await c.open('post-send-deltas');c.server.mode='hang';
      await p.evaluate(s=>{__reliability.editSnapshot(s);__reliability.push();},
        {favorites:[A,B,E],blacklist:[A,B,E],bookmarks:sent});await settle(p);
      const first=JSON.parse(puts(c)[0].body);assert.deepEqual(first.bookmarks,sent);
      await p.evaluate(s=>__reliability.editSnapshot(s),
        {favorites:[A,R,D,E],blacklist:[A,R,D,E],bookmarks:local});await tick(p,15100);
      const before=await snap(p);assert.equal(before.dirty,true);assert.deepEqual(before.bookmarks,local);
      // The other writer may include the uncertain body, or may never have seen it.
      const remoteBookmarks=(landed?sent:original.filter(b=>!['bm-restore','list:restore','lm:restore'].includes(b.id)))
        .map(b=>b.id==='bm-revert'?{...b,name:'Remote edit'}:b).concat(remoteAdded);
      const remote={favorites:landed?[A,B,E,C]:[A,C],blacklist:landed?[A,B,E,C]:[A,C],bookmarks:remoteBookmarks};
      if(version!=='legacy')Object.assign(remote,{v:version==='same'?5:7,w:'other-writer'});
      c.server.remote=remote;
      c.server.mode='ok';
      if(version==='retry-conflict'){
        c.server.conflictRemote=remote;c.server.remote={...base,favorites:[A,R],blacklist:[A,R],bookmarks:original};
        c.server.mode='conflictOnce';
      }
      if(version==='matched')c.server.remote={...first,v:6};
      await tick(p,5600);
      if(['same','legacy'].includes(version)){await tick(p,16100);await tick(p,5600);}
      if(version==='retry-conflict'){await tick(p,16100);await tick(p,61100);}
      const s=await synced(p),expectedSet=new Set(version==='matched'?[A,R,D,E]:[A,R,D,E,C]);
      assert.deepEqual(new Set(s.fav),expectedSet);assert.deepEqual(new Set(s.black),expectedSet);
      const keyed=bs=>Object.fromEntries(bs.map(b=>[b.id||'legacy',b]));
      const expectedBookmarks=version==='matched'?local:local.concat(remoteAdded);
      assert.deepEqual(keyed(s.bookmarks),keyed(expectedBookmarks));
      assert.equal(s.dirty,false);assert.equal(s.diskPending,null);assert.equal(puts(c).length,version==='retry-conflict'?3:2);
      // BE-A: the replacement for a write proven lost is the CURRENT state
      // under a fresh id, never a replay of the old body — but the old body
      // is still handed to the 409 merge (lostSentBody), which is why the
      // post-send deltas asserted above survive either way.
      if(version==='retry-conflict')assert.notEqual(JSON.parse(puts(c)[1].body).w,JSON.parse(puts(c)[0].body).w);
      const recovered=JSON.parse(puts(c).at(-1).body);assert.notEqual(recovered.w,first.w);
      assert.deepEqual(new Set(recovered.favorites),expectedSet);assert.deepEqual(new Set(recovered.blacklist),expectedSet);
      assert.deepEqual(keyed(recovered.bookmarks),keyed(expectedBookmarks));
      assert.deepEqual(keyed(JSON.parse(s.bms)),keyed(s.bookmarks));
      assert.deepEqual(new Set(JSON.parse(s.cache).fav),expectedSet);
    },{base:{...base,favorites:[A,R],blacklist:[A,R],bookmarks:original}});
  }
  return results;
}
