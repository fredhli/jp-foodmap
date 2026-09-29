import assert from 'node:assert/strict';
import {test} from 'node:test';
import worker from '../../worker/src/index.js';
import {PlacesQuota, quotaWindow} from '../../worker/src/places-quota.js';
import {KVMock, makeEnv, ORIGIN, tok, signJWT} from './worker-env.mjs';

class Storage {
  constructor() { this.data = new Map(); this.queue = Promise.resolve(); this.alarm = null; }
  transaction(fn) {
    const run = this.queue.then(async () => {
      const staged = new Map(this.data);
      const result = await fn({get: async k => staged.get(k), put: async (k,v) => { staged.set(k,v); }});
      this.data = staged;
      return result;
    });
    this.queue = run.catch(() => {});
    return run;
  }
  async getAlarm() { return this.alarm; }
  async setAlarm(value) { this.alarm = value; }
  async deleteAll() { this.data.clear(); }
}
function setup(overrides = {}) {
  const env = makeEnv(new KVMock(), 'FRA', {PLACES_UI_ENABLED:'true', ...overrides});
  const objects = new Map();
  env.PLACES_QUOTA = {
    idFromName: name => name,
    get: id => {
      if (!objects.has(id)) objects.set(id, new PlacesQuota({storage: new Storage()}, env));
      return objects.get(id);
    },
  };
  return {env, objects};
}
async function permit(env, opts = {}) {
  const headers = {Origin: opts.origin === undefined ? ORIGIN : opts.origin, 'Content-Type':'application/json'};
  if (!opts.anonymous) headers.Authorization = 'Bearer ' + tok(opts.user || 'alice');
  if (opts.cookie) { delete headers.Authorization; headers.Cookie = opts.cookie; }
  const body = opts.raw === undefined ? JSON.stringify({placeId:opts.placeId || 'ChIJ-test-place', requestId:opts.requestId || crypto.randomUUID(), user:opts.fakeUser}) : opts.raw;
  const r = await worker.fetch(new Request('https://api.jpfoodmap.com/api/places/permit', {method:'POST',headers,body}),env);
  return {status:r.status, body:await r.json(),headers:r.headers};
}

test('places: authentication, origin, configuration and request validation', async () => {
  const {env,objects} = setup();
  assert.equal((await permit(env,{anonymous:true})).status,401);
  assert.equal((await permit(env,{origin:'https://evil.example'})).status,403);
  assert.equal((await permit(env,{origin:''})).status,403);
  assert.equal((await permit(env,{raw:'{'})).status,400);
  assert.equal((await permit(env,{placeId:'https://invalid/'})).status,400);
  assert.equal((await permit(env,{raw:'x'.repeat(2049)})).status,413);
  assert.equal(objects.size,0);
  assert.equal((await permit({...env,PLACES_UI_ENABLED:'false'})).status,503);
  assert.equal((await permit({...env,PLACES_QUOTA:null})).status,503);
  assert.equal((await permit({...env,PLACES_MONTHLY_LIMIT:'20001'})).status,503);
  assert.equal((await permit({...env,PLACES_MONTHLY_LIMIT:'20000',PLACES_DAILY_LIMIT:'200'})).status,200);
});

test('places: account quota survives simultaneous tabs and ignores claimed identity', async () => {
  const {env} = setup();
  const rs = await Promise.all(Array.from({length:30},(_,i)=>permit(env,{fakeUser:'fake-'+i})));
  assert.equal(rs.filter(r=>r.status===200).length,20);
  assert.equal(rs.filter(r=>r.body.error==='daily_limit').length,10);
  const bob = await permit(env,{user:'bob'});
  assert.equal(bob.status,200);
  assert.equal(bob.body.remainingDaily,19);
  assert.equal(bob.headers.get('Cache-Control'),'private, no-store');
  assert.equal(bob.headers.get('Access-Control-Allow-Origin'),ORIGIN);
});

test('places: global monthly quota is shared and never raced', async () => {
  const {env} = setup({PLACES_MONTHLY_LIMIT:'3'});
  const rs = await Promise.all(Array.from({length:12},(_,i)=>permit(env,{user:i%2?'bob':'alice'})));
  assert.equal(rs.filter(r=>r.status===200).length,3);
  assert.equal(rs.filter(r=>r.body.error==='monthly_limit').length,9);
  assert.ok(rs.find(r=>r.status===429).headers.get('Retry-After'));
});

test('places: request retries are idempotent and bound to the restaurant', async () => {
  const {env,objects} = setup();
  const requestId = crypto.randomUUID();
  const rs = await Promise.all([permit(env,{requestId}),permit(env,{requestId})]);
  assert.deepEqual(rs[0].body,rs[1].body);
  assert.equal(rs[0].body.remainingDaily,19);
  assert.equal((await permit(env,{requestId,placeId:'ChIJ-other-place'})).status,409);
  assert.equal([...objects.values()][0].storage.data.get('monthly'),1);
});

test('places: uses signed session identity and fails closed on storage faults', async () => {
  const {env} = setup();
  const jwt = await signJWT({sub:'cookie-user',exp:Math.floor(Date.now()/1000)+3600},env.SESSION_HMAC);
  assert.equal((await permit(env,{cookie:'tabelog_session='+jwt})).status,200);
  env.PLACES_QUOTA.get = () => ({fetch:async()=>{throw new Error('offline');}});
  assert.equal((await permit(env)).status,503);
});

test('places: day and month rollover, Pacific DST, and alarm cleanup', async () => {
  assert.equal(quotaWindow(Date.parse('2026-03-08T09:00:00Z')).resetAt,'2026-03-09T07:00:00.000Z');
  assert.equal(quotaWindow(Date.parse('2026-11-01T08:00:00Z')).resetAt,'2026-11-02T08:00:00.000Z');
  assert.equal(quotaWindow(Date.parse('2026-10-01T06:59:59Z')).month,'2026-09');
  const oldNow=Date.now;
  try {
    let now=Date.parse('2026-09-29T12:00:00Z');Date.now=()=>now;
    const {env,objects}=setup({PLACES_DAILY_LIMIT:'1',PLACES_MONTHLY_LIMIT:'2'});
    assert.equal((await permit(env)).status,200);
    assert.equal((await permit(env)).body.error,'daily_limit');
    now=Date.parse('2026-09-30T12:00:00Z');
    assert.equal((await permit(env)).status,200);
    assert.equal((await permit(env,{user:'bob'})).body.error,'monthly_limit');
    now=Date.parse('2026-10-01T12:00:00Z');
    assert.equal((await permit(env)).status,200);
    assert.equal(objects.size,2);
    const old=objects.get('places:2026-09');await old.alarm();assert.equal(old.storage.data.size,0);
  } finally {Date.now=oldNow;}
});


test('places: session revocation storage failure cannot issue a permit', async () => {
  const {env,objects}=setup();
  const jwt=await signJWT({sub:'revoked-user',sv:0,exp:Math.floor(Date.now()/1000)+3600},env.SESSION_HMAC);
  env.KV.get=async()=>{throw new Error('session storage offline');};
  const result=await permit(env,{cookie:'tabelog_session='+jwt});
  assert.equal(result.status,503);
  assert.equal(result.body.error,'unavailable');
  assert.equal(objects.size,0);
});
