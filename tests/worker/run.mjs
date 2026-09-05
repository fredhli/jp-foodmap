// Offline test bench for worker/src/index.js.
//
//   node tests/worker/run.mjs        (or: node --test tests/worker/)
//
// Imports the real Worker and drives it with Request objects against a
// Map-backed KV mock and a stubbed tokeninfo endpoint. Nothing here touches
// the network, the production Worker, KV, Google, or git.
//
// Each assertion is tagged with the M-ID it guards.
import worker from '../../worker/src/index.js';
import legacyWorker from './fixtures/worker-legacy.mjs';
import {KVMock, makeEnv, ORIGIN, tok, tokeninfoState, signJWT, decodeJWT} from './worker-env.mjs';

const results = [];
function rec(id, name, pass, detail) {
  results.push({id, name, pass, detail});
  if (!process.env.QUIET) {
    console.log((pass ? 'PASS ' : 'FAIL ') + id.padEnd(14) + name + (pass ? '' : (detail ? '\n      ' + detail : '')));
  }
}

const API = 'https://api.jpfoodmap.com/api';
function req(path, init = {}, origin = ORIGIN) {
  const h = new Headers(init.headers || {});
  if (origin) h.set('Origin', origin);
  return new Request(API + path, Object.assign({}, init, {headers: h}));
}
async function callOn(w, env, path, init, origin) {
  try {
    const r = await w.fetch(req(path, init, origin), env);
    const text = await r.text();
    return {
      status: r.status,
      text,
      headers: Object.fromEntries(r.headers.entries()),
      cookies: r.headers.getSetCookie ? r.headers.getSetCookie() : [],
      threw: null,
    };
  } catch (e) {
    return {status: 500, text: '', headers: {}, cookies: [], threw: String((e && e.message) || e)};
  }
}
const call = (env, path, init, origin) => callOn(worker, env, path, init, origin);

function sessionCookie(res) {
  for (const c of res.cookies) {
    if (c.startsWith('tabelog_session=') && !/Max-Age=0/.test(c)) return c.split(';')[0];
  }
  return '';
}
async function signIn(env, name = 'alice') {
  const r = await call(env, '/session', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id_token: tok(name)}),
  });
  return {res: r, cookie: sessionCookie(r)};
}
const hasCors = (r) => r.headers['access-control-allow-origin'] === ORIGIN;
const noStore = (r) => r.headers['cache-control'] === 'private, no-store';
const json = (t) => { try { return JSON.parse(t); } catch (_) { return null; } };
const stored = (kv, key = 'state:1001') => json(kv.primary(key));

export async function runAll() {
  // ---------------- 1. CORS (unchanged contract) ----------------
  {
    const kv = new KVMock(); const env = makeEnv(kv);
    const ok = await call(env, '/state', {method: 'OPTIONS', headers: {'Access-Control-Request-Method': 'PUT'}});
    rec('W-CORS-1', 'OPTIONS echoes origin + credentials', ok.status === 204 && hasCors(ok) && ok.headers['access-control-allow-credentials'] === 'true', JSON.stringify(ok.headers));
    const evil = await call(env, '/state', {method: 'OPTIONS'}, 'https://evil.example');
    rec('W-CORS-2', 'unknown origin gets no Allow-Origin', evil.status === 204 && !evil.headers['access-control-allow-origin']);
    const lh = await call(env, '/state', {method: 'OPTIONS'}, 'http://localhost:8000');
    rec('W-CORS-3', 'localhost:8000 still allowed (dev)', lh.headers['access-control-allow-origin'] === 'http://localhost:8000');
    const noorigin = await call(env, '/state', {}, null);
    rec('W-CORS-4', 'no Origin (curl) → 401, no ACAO', noorigin.status === 401 && !noorigin.headers['access-control-allow-origin']);
  }

  // ---------------- 2. Session / cookies (M-037, M-013, M-126) ----------------
  {
    const kv = new KVMock(); const env = makeEnv(kv);
    const {res, cookie} = await signIn(env);
    rec('W-SESS-1', 'POST /session → 200 + Set-Cookie', res.status === 200 && !!cookie);
    const body = json(res.text);
    rec('W-SESS-2', 'response shape unchanged {sub,email,name,picture,exp(ms)}',
      body && body.sub === '1001' && body.email === 'alice@example.com' && body.name === 'Alice' &&
      body.picture === 'https://p/alice.png' && Math.abs(body.exp - (Date.now() + 90 * 86400e3)) < 10e3, res.text);

    const sess = res.cookies.find((c) => c.startsWith('tabelog_session=') && !/Max-Age=0/.test(c)) || '';
    rec('M-037-a', 'session cookie is host-only (no Domain=), Secure/HttpOnly/Lax/90d',
      !/Domain=/i.test(sess) && /Max-Age=7776000/.test(sess) && /SameSite=Lax/.test(sess) && /Secure/.test(sess) && /HttpOnly/.test(sess), sess.replace(/=[^;]{20,}/, '=<jwt>'));
    const legacyClear = res.cookies.find((c) => c.startsWith('tabelog_session=deleted')) || '';
    rec('M-037-b', 'same response clears the old Domain=jpfoodmap.com cookie',
      /Domain=jpfoodmap\.com/.test(legacyClear) && /Max-Age=0/.test(legacyClear), legacyClear);
    const hint = res.cookies.find((c) => c.startsWith('tabelog_has_session=')) || '';
    rec('M-013-a', 'hint cookie tabelog_has_session=1: domain-wide, readable by the page (not HttpOnly)',
      /^tabelog_has_session=1/.test(hint) && /Domain=jpfoodmap\.com/.test(hint) && !/HttpOnly/.test(hint) && /Max-Age=7776000/.test(hint), hint);

    const payload = decodeJWT(cookie.split('=').slice(1).join('='));
    rec('M-037-c', 'JWT carries no PII — exactly {sub,iat,exp,sv}',
      JSON.stringify(Object.keys(payload).sort()) === '["exp","iat","sub","sv"]' && payload.sub === '1001', JSON.stringify(payload));
    const prof = json(kv.primary('profile:1001'));
    rec('M-037-d', 'profile moved to KV profile:<sub>', !!prof && prof.email === 'alice@example.com' && prof.name === 'Alice');

    const me = await call(env, '/me', {headers: {Cookie: cookie}});
    const meBody = json(me.text);
    rec('M-037-e', '/api/me shape unchanged, profile read back from KV',
      me.status === 200 && meBody.sub === '1001' && meBody.email === 'alice@example.com' && meBody.picture === 'https://p/alice.png', me.text);
    rec('M-132-a', '/api/me has Cache-Control: private, no-store', noStore(me), JSON.stringify(me.headers));

    // Old cookie (pre-M-037) still carries PII inline and must keep working.
    const kv2 = new KVMock(); const env2 = makeEnv(kv2);
    const legacyJwt = await signJWT({sub: '1001', email: 'old@example.com', name: 'Old', picture: 'p', iat: 1, exp: Math.floor(Date.now() / 1000) + 999}, env2.SESSION_HMAC);
    const meOld = await call(env2, '/me', {headers: {Cookie: 'tabelog_session=' + legacyJwt}});
    rec('M-037-f', 'pre-M-037 cookie (PII in JWT, no profile key) still answers /api/me',
      meOld.status === 200 && json(meOld.text).email === 'old@example.com', meOld.text);

    // sv revocation
    const del = await call(env, '/session', {method: 'DELETE', headers: {Cookie: cookie}});
    rec('W-SESS-3', 'DELETE /session → 204 + clears session, legacy and hint cookies',
      del.status === 204 && del.cookies.length === 3 && del.cookies.every((c) => /Max-Age=0/.test(c)), JSON.stringify(del.cookies));
    rec('M-037-g', 'DELETE /session bumps sv:<sub>', kv.primary('sv:1001') === '1');
    const meAfter = await call(env, '/me', {headers: {Cookie: cookie}});
    rec('M-037-h', 'cookie issued before sign-out is now REVOKED (was valid for 90 days)',
      meAfter.status === 401 && meAfter.cookies.some((c) => /Max-Age=0/.test(c)), 'status=' + meAfter.status);
    const again = await signIn(env);
    rec('M-037-i', 'signing in again mints sv=1 and works', again.res.status === 200 &&
      decodeJWT(again.cookie.split('=').slice(1).join('=')).sv === 1 &&
      (await call(env, '/me', {headers: {Cookie: again.cookie}})).status === 200);
    const noSvCookie = await signJWT({sub: '1001', email: 'legacy@example.com', iat: 1, exp: Math.floor(Date.now() / 1000) + 999}, env.SESSION_HMAC);
    const meNoSv = await call(env, '/me', {headers: {Cookie: 'tabelog_session=' + noSvCookie}});
    rec('M-037-j', 'cookie without sv (pre-deploy) stays valid even after a sign-out', meNoSv.status === 200, 'status=' + meNoSv.status);

    // Both cookies present during the transition (host-only + domain-scoped).
    const both = await call(env, '/me', {headers: {Cookie: 'tabelog_session=deleted; tabelog_session=' + again.cookie.split('=').slice(1).join('=')}});
    rec('M-037-k', 'two tabelog_session values in one header: every one is tried', both.status === 200, 'status=' + both.status);
  }

  // ---------------- 3. JWT robustness (M-127, M-126) ----------------
  {
    const kv = new KVMock(); const env = makeEnv(kv);
    const {cookie} = await signIn(env);
    const tampered = cookie.slice(0, -2) + 'AA';
    const meT = await call(env, '/me', {headers: {Cookie: tampered}});
    rec('W-JWT-1', 'tampered signature → 401 + cookie cleared', meT.status === 401 && meT.cookies.some((c) => /Max-Age=0/.test(c)));
    const garb = await call(env, '/me', {headers: {Cookie: 'tabelog_session=a.b.%%%'}});
    rec('M-127-a', 'garbage cookie on /api/me → 401 (was 500: atob threw outside the try) + cleared + CORS',
      garb.status === 401 && garb.cookies.some((c) => /Max-Age=0/.test(c)) && hasCors(garb), 'status=' + garb.status + ' threw=' + garb.threw);
    const garbState = await call(env, '/state', {headers: {Cookie: 'tabelog_session=a.b.%%%'}});
    rec('M-127-b', 'garbage cookie on /api/state → 401 (was 500), with CORS',
      garbState.status === 401 && hasCors(garbState), 'status=' + garbState.status + ' threw=' + garbState.threw);
    const expired = await signJWT({sub: '1001', exp: Math.floor(Date.now() / 1000) - 5}, env.SESSION_HMAC);
    rec('W-JWT-2', 'expired JWT → 401', (await call(env, '/me', {headers: {Cookie: 'tabelog_session=' + expired}})).status === 401);
    const nosub = await signJWT({exp: Math.floor(Date.now() / 1000) + 100}, env.SESSION_HMAC);
    rec('W-JWT-3', 'JWT without sub → 401', (await call(env, '/me', {headers: {Cookie: 'tabelog_session=' + nosub}})).status === 401);
    const algNone = Buffer.from(JSON.stringify({alg: 'none'})).toString('base64url') + '.' +
      Buffer.from(JSON.stringify({sub: '1001', exp: Math.floor(Date.now() / 1000) + 100})).toString('base64url') + '.';
    rec('W-JWT-4', 'alg=none → 401', (await call(env, '/me', {headers: {Cookie: 'tabelog_session=' + algNone}})).status === 401);

    // M-126: two-key verification, one-key signing.
    const kvR = new KVMock();
    const envRot = makeEnv(kvR, 'FRA', {SESSION_HMAC: 'secret-new', SESSION_HMAC_PREV: 'secret-old'});
    const oldSigned = await signJWT({sub: '1001', iat: 1, exp: Math.floor(Date.now() / 1000) + 999}, 'secret-old');
    rec('M-126-a', 'cookie signed with SESSION_HMAC_PREV still verifies',
      (await call(envRot, '/me', {headers: {Cookie: 'tabelog_session=' + oldSigned}})).status === 200);
    const fresh = await signIn(envRot);
    const envOnlyOld = makeEnv(kvR, 'FRA', {SESSION_HMAC: 'secret-old'});
    const envOnlyNew = makeEnv(kvR, 'FRA', {SESSION_HMAC: 'secret-new'});
    rec('M-126-b', 'new cookies are always signed with the CURRENT secret',
      (await call(envOnlyNew, '/me', {headers: {Cookie: fresh.cookie}})).status === 200 &&
      (await call(envOnlyOld, '/me', {headers: {Cookie: fresh.cookie}})).status === 401);
    const envNoHmac = makeEnv(kvR, 'FRA', {SESSION_HMAC: '', SESSION_HMAC_PREV: ''});
    const s503 = await call(envNoHmac, '/session', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id_token: tok('alice')})});
    rec('M-126-c', 'no SESSION_HMAC → POST /session 503 (and a log line)', s503.status === 503 && hasCors(s503));
  }

  // ---------------- 4. Sign-in input handling (M-040) ----------------
  {
    const kv = new KVMock(); const env = makeEnv(kv);
    const bad = await call(env, '/session', {method: 'POST', body: 'not json'});
    rec('W-SI-1', 'invalid JSON → 400 + CORS', bad.status === 400 && hasCors(bad));
    rec('W-SI-2', 'wrong aud → 401', (await signIn(env, 'wrongaud')).res.status === 401);
    rec('W-SI-3', 'tokeninfo without sub → 401', (await signIn(env, 'nosub')).res.status === 401);
    rec('W-SI-4', 'unknown id_token → 401', (await signIn(env, 'unknown')).res.status === 401);
    const before = tokeninfoState.calls;
    const junk = await call(env, '/session', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id_token: 'x'})});
    rec('M-040-a', 'malformed id_token is rejected WITHOUT spending a tokeninfo subrequest',
      junk.status === 401 && tokeninfoState.calls === before, 'calls ' + before + '→' + tokeninfoState.calls);
    tokeninfoState.mode = 'down';
    rec('W-SI-5', 'tokeninfo unreachable → 401', (await signIn(env)).res.status === 401);
    tokeninfoState.mode = 'html';
    rec('W-SI-6', 'tokeninfo 503 → 401', (await signIn(env)).res.status === 401);
    tokeninfoState.mode = 'ok';
    rec('M-040-b', 'tokeninfo call carries AbortSignal.timeout(5000)',
      /AbortSignal\.timeout\(TOKENINFO_TIMEOUT_MS\)/.test(await workerSource()) , 'source check');
  }

  // ---------------- 5. /api/state basics (M-132) ----------------
  {
    const kv = new KVMock(); const env = makeEnv(kv);
    const {cookie} = await signIn(env);
    const C = {Cookie: cookie, 'Content-Type': 'application/json'};
    const noauth = await call(env, '/state');
    rec('W-ST-1', 'GET /state without auth → 401 JSON envelope + CORS',
      noauth.status === 401 && hasCors(noauth) && json(noauth.text) && json(noauth.text).error === 'no_auth', noauth.text);
    const bearer = await call(env, '/state', {headers: {Authorization: 'Bearer ' + tok('alice')}});
    rec('W-ST-2', 'legacy Bearer id_token still works', bearer.status === 200 && bearer.text === '{}');
    const empty = await call(env, '/state', {headers: C});
    rec('W-ST-3', 'new account GET → "{}"', empty.text === '{}' && empty.headers['content-type'] === 'application/json');
    rec('M-132-b', 'GET /state has Cache-Control: private, no-store', noStore(empty), JSON.stringify(empty.headers));
    const p1 = await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({favorites: ['a'], blacklist: [], bookmarks: [], baseV: 0})});
    rec('W-ST-4', 'versioned PUT baseV=0 → 200 {"v":1} (shape unchanged)', p1.status === 200 && p1.text === '{"v":1}', p1.text);
    rec('M-132-c', 'PUT /state has Cache-Control: private, no-store', noStore(p1));
    const s1 = stored(kv);
    rec('W-ST-5', 'stored blob = body minus baseV plus v', s1.v === 1 && !('baseV' in s1) && s1.favorites[0] === 'a');
    const p2 = await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({favorites: ['b'], blacklist: [], bookmarks: [], baseV: 0})});
    rec('W-ST-6', 'stale baseV → 409 with the current blob', p2.status === 409 && json(p2.text).v === 1);
    const p3 = await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({favorites: [], blacklist: [], bookmarks: [], baseV: 1})});
    rec('W-ST-7', 'deleting everything still propagates (known arrays replaced wholesale)',
      p3.status === 200 && stored(kv).favorites.length === 0);
    const m405 = await call(env, '/me', {method: 'POST', headers: C});
    rec('M-132-d', '405 on /api/me carries Allow', m405.status === 405 && m405.headers['allow'] === 'GET, OPTIONS' && hasCors(m405));
    const s405 = await call(env, '/session', {method: 'GET'});
    rec('M-132-e', '405 on /api/session carries Allow', s405.status === 405 && s405.headers['allow'] === 'POST, DELETE, OPTIONS');
    const st405 = await call(env, '/state', {method: 'PATCH', headers: C});
    rec('M-132-f', '405 on /api/state advertises GET, PUT, DELETE, OPTIONS', st405.status === 405 && st405.headers['allow'] === 'GET, PUT, DELETE, OPTIONS');
    const nf = await call(env, '/nope', {headers: C});
    rec('M-132-g', '404 is a JSON envelope with CORS', nf.status === 404 && hasCors(nf) && json(nf.text).error === 'not_found');
  }

  // ---------------- 6. M-044 unknown top-level fields ----------------
  {
    const kv = new KVMock(); const env = makeEnv(kv);
    const {cookie} = await signIn(env);
    const C = {Cookie: cookie, 'Content-Type': 'application/json'};
    kv.seed('state:1001', JSON.stringify({
      favorites: ['a'], blacklist: [], bookmarks: [{id: 'b1', emoji: '📍'}],
      prefs: {lang: 'ja'}, favMeta: {a: {list: 'x'}}, w: 'abc123', v: 3,
    }));
    const p = await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({
      favorites: ['a', 'b'], blacklist: [], bookmarks: [{id: 'b1', emoji: '📍'}], baseV: 3,
    })});
    const s = stored(kv);
    rec('M-044-a', 'unknown top-level fields (prefs/favMeta) survive a client that never heard of them',
      p.status === 200 && s.prefs && s.prefs.lang === 'ja' && s.favMeta && s.favMeta.a.list === 'x', JSON.stringify(s));
    rec('M-044-b', 'the write id w survives too (unblocks M-002 phase 0)', s.w === 'abc123');
    rec('M-044-c', 'the three known arrays are still replaced wholesale', s.favorites.join() === 'a,b' && s.v === 4);
    const p2 = await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({
      favorites: ['a'], blacklist: [], bookmarks: [], prefs: {lang: 'en'}, baseV: 4,
    })});
    rec('M-044-d', 'a client that DOES send the field overwrites it', p2.status === 200 && stored(kv).prefs.lang === 'en');
    const p3 = await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({
      favorites: ['a'], blacklist: [], bookmarks: [], prefs: null, baseV: 5,
    })});
    rec('M-044-e', 'explicit null deletes the field', p3.status === 200 && !('prefs' in stored(kv)) && stored(kv).favMeta);
    rec('M-044-f', 'client-sent v is still overridden by the server',
      (await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({favorites: [], blacklist: [], bookmarks: [], v: 999, baseV: 6})})).text === '{"v":7}');
  }

  // ---------------- 7. M-043 legacy PUT → server-side union ----------------
  {
    const kv = new KVMock(); const env = makeEnv(kv);
    const {cookie} = await signIn(env);
    const C = {Cookie: cookie, 'Content-Type': 'application/json'};
    kv.seed('state:1001', JSON.stringify({
      favorites: ['new1', 'new2'], blacklist: ['bl1'],
      bookmarks: [{id: 'b1', name: 'orig', emoji: '📍'}, {id: 'b2', emoji: '⭐'}],
      prefs: {lang: 'ja'}, v: 1,
    }));
    const leg = await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({
      favorites: ['stale'], blacklist: [], bookmarks: [{id: 'b1', name: 'edited', emoji: '📍'}],
    })});
    const s = stored(kv);
    rec('M-043-a', 'legacy PUT (no baseV) still answers the literal "ok"', leg.status === 200 && leg.text === 'ok', leg.text);
    rec('M-043-b', 'favorites are unioned, not replaced (was: new1/new2 wiped)',
      ['new1', 'new2', 'stale'].every((f) => s.favorites.includes(f)) && s.favorites.length === 3, JSON.stringify(s.favorites));
    rec('M-043-c', 'blacklist unioned', s.blacklist.join() === 'bl1');
    rec('M-043-d', 'bookmarks unioned by id, the uploaded copy wins',
      s.bookmarks.length === 2 && s.bookmarks.find((b) => b.id === 'b1').name === 'edited' && !!s.bookmarks.find((b) => b.id === 'b2'), JSON.stringify(s.bookmarks));
    rec('M-043-e', 'unknown fields survive a legacy PUT too', s.prefs.lang === 'ja' && s.v === 2);
    const leg2 = await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({favorites: ['x']})});
    rec('M-043-f', 'legacy PUT that omits bookmarks entirely does not delete them',
      leg2.text === 'ok' && stored(kv).bookmarks.length === 2, JSON.stringify(stored(kv).bookmarks));
    // A no-id entry is kept, and repeated legacy pushes must not duplicate it.
    kv.seed('state:1001', JSON.stringify({favorites: [], blacklist: [], bookmarks: [{name: 'anon', lat: 1, lon: 2}], v: 9}));
    await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({favorites: [], blacklist: [], bookmarks: [{name: 'anon', lat: 1, lon: 2}]})});
    rec('M-043-g', 'bookmarks without an id are kept and de-duplicated', stored(kv).bookmarks.length === 1, JSON.stringify(stored(kv).bookmarks));
  }

  // ---------------- 8. M-042 / M-127 bad shapes ----------------
  {
    const kv = new KVMock(); const env = makeEnv(kv);
    const {cookie} = await signIn(env);
    const C = {Cookie: cookie, 'Content-Type': 'application/json'};
    for (const [name, body] of [['array', '[1,2]'], ['number', '5'], ['null', 'null'], ['string', '"abc"'], ['bool', 'true']]) {
      kv.writes.delete('state:1001');
      const r = await call(env, '/state', {method: 'PUT', headers: C, body});
      rec('M-127-' + name, 'PUT body ' + name + ' → 400 (was uncaught 500) + CORS, nothing stored',
        r.status === 400 && hasCors(r) && json(r.text).error === 'invalid_shape' && kv.primary('state:1001') === null,
        'status=' + r.status + ' threw=' + r.threw);
    }
    kv.writes.delete('state:1001');
    const fav = await call(env, '/state', {method: 'PUT', headers: C, body: '{"favorites":"x","blacklist":[],"bookmarks":[],"baseV":0}'});
    rec('M-042-a', 'favorites of the wrong type is coerced to [] (not a 400, not stored as a string)',
      fav.status === 200 && Array.isArray(stored(kv).favorites) && stored(kv).favorites.length === 0, kv.primary('state:1001'));
    const bm = await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({
      favorites: [], blacklist: [], baseV: 1,
      bookmarks: [{id: 'b1', emoji: '📍'}, {id: 'b2', emoji: 5}, null, 'x', 7, {id: 'b3'}, {id: 'b4', emoji: null}],
    })});
    const kept = stored(kv).bookmarks.map((b) => b.id);
    rec('M-042-b', 'a bookmark with a non-string emoji is dropped, the rest survive (was: client-side wipe of all 219 built-ins)',
      bm.status === 200 && kept.join() === 'b1,b3,b4', JSON.stringify(stored(kv).bookmarks));
    const big = await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({favorites: [1, {a: 1}, null], blacklist: [], bookmarks: [], baseV: 2})});
    rec('M-042-c', 'odd favorites entries are stored as-is (known-bad shapes only, no whitelist)', big.status === 200);
  }

  // ---------------- 9. M-129 unreadable stored blob ----------------
  {
    const kv = new KVMock(); const env = makeEnv(kv);
    const {cookie} = await signIn(env);
    const C = {Cookie: cookie, 'Content-Type': 'application/json'};
    kv.seed('state:1001', '{not json');
    const g = await call(env, '/state', {headers: C});
    rec('M-129-a', 'corrupt KV value → GET returns {} (was: raw garbage → permanent 同步失败)', g.status === 200 && g.text === '{}', g.text);
    const c409 = await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({favorites: [], blacklist: [], bookmarks: [], baseV: 3})});
    rec('M-129-b', 'corrupt blob + stale baseV → 409 with {} so the client can self-heal', c409.status === 409 && c409.text === '{}', c409.text);
    const ok0 = await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({favorites: ['x'], blacklist: [], bookmarks: [], baseV: 0})});
    rec('M-129-c', 'baseV=0 over a corrupt blob heals it to v=1', ok0.status === 200 && stored(kv).v === 1);
    kv.seed('state:1001', '[1,2]');
    rec('M-129-d', 'an array stored by the old Worker also reads back as {}', (await call(env, '/state', {headers: C})).text === '{}');
  }

  // ---------------- 10. M-053 size limit in bytes ----------------
  {
    const kv = new KVMock(); const env = makeEnv(kv);
    const {cookie} = await signIn(env);
    const C = {Cookie: cookie, 'Content-Type': 'application/json'};
    const cjk = JSON.stringify({favorites: [], blacklist: [], bookmarks: [{id: 'bm-x', name_src: '東'.repeat(150000), lat: 1, lon: 2, emoji: '📍'}], baseV: 0});
    const r = await call(env, '/state', {method: 'PUT', headers: C, body: cjk});
    const b = json(r.text);
    rec('M-053-a', '450 KB of CJK (150k UTF-16 units) → 413; the limit is bytes now',
      r.status === 413 && b.limit === 200000 && b.used === Buffer.byteLength(cjk), r.text.slice(0, 120));
    rec('M-053-b', '413 body carries {limit, used} and CORS', hasCors(r) && typeof b.used === 'number');
    const urls = JSON.stringify({favorites: Array.from({length: 4000}, (_, i) => 'https://tabelog.com/tokyo/A1301/A130101/' + (13000000 + i) + '/'), blacklist: [], bookmarks: [], baseV: 0});
    rec('M-053-c', '4000 Tabelog URLs still → 413', (await call(env, '/state', {method: 'PUT', headers: C, body: urls})).status === 413);
    const okBody = JSON.stringify({favorites: ['x'.repeat(1000)], blacklist: [], bookmarks: [], baseV: 0});
    rec('M-053-d', 'a normal body is unaffected', (await call(env, '/state', {method: 'PUT', headers: C, body: okBody})).status === 200);
  }

  // ---------------- 11. M-008 DELETE /api/state ----------------
  {
    const kv = new KVMock(); const env = makeEnv(kv);
    const {cookie} = await signIn(env);
    const C = {Cookie: cookie, 'Content-Type': 'application/json'};
    await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({favorites: ['a'], blacklist: [], bookmarks: [], baseV: 0})});
    const noauth = await call(env, '/state', {method: 'DELETE'});
    rec('M-008-a', 'DELETE /state without auth → 401', noauth.status === 401 && hasCors(noauth));
    rec('M-008-b', 'state survives the unauthenticated attempt', !!kv.primary('state:1001'));
    const d = await call(env, '/state', {method: 'DELETE', headers: C});
    rec('M-008-c', 'DELETE /state → 204 and every key for the user is gone',
      d.status === 204 && kv.primary('state:1001') === null && kv.primary('profile:1001') === null && kv.primary('sv:1001') === null,
      'status=' + d.status);
    rec('M-008-d', 'DELETE /state signs the browser out (3 cleared cookies)',
      d.cookies.length === 3 && d.cookies.every((c) => /Max-Age=0/.test(c)), JSON.stringify(d.cookies));
    rec('M-008-e', 'a fresh GET after deletion is a clean {}', (await call(env, '/state', {headers: {Authorization: 'Bearer ' + tok('alice')}})).text === '{}');
  }

  // ---------------- 12. M-040 KV failures ----------------
  {
    const kv = new KVMock({enforceRate: true}); const env = makeEnv(kv);
    const {cookie} = await signIn(env);
    const C = {Cookie: cookie, 'Content-Type': 'application/json'};
    const q1 = await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({favorites: ['1'], blacklist: [], bookmarks: [], baseV: 0})});
    const q2 = await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({favorites: ['1', '2'], blacklist: [], bookmarks: [], baseV: 1})});
    rec('M-040-c', 'KV 1-write/s/key → 429 + Retry-After + CORS (was an uncaught 500)',
      q1.status === 200 && q2.status === 429 && q2.headers['retry-after'] === '2' && hasCors(q2), 'q2=' + q2.status + ' ' + q2.text);
    const kv2 = new KVMock(); kv2.putHook = async (k) => { if (k.startsWith('state:')) throw new Error('KV PUT failed: quota exceeded'); };
    const env2 = makeEnv(kv2);
    const s2 = await signIn(env2);
    const q3 = await call(env2, '/state', {method: 'PUT', headers: {Cookie: s2.cookie}, body: JSON.stringify({favorites: [], blacklist: [], bookmarks: [], baseV: 0})});
    rec('M-040-d', 'KV write quota exhausted → 503 + Retry-After + readable JSON body',
      q3.status === 503 && q3.headers['retry-after'] === '10' && hasCors(q3) && json(q3.text).error === 'storage_unavailable', q3.text);
    const kv3 = new KVMock(); const env3 = makeEnv(kv3);
    const s3 = await signIn(env3);
    kv3.getHook = async (k) => { if (k.startsWith('state:')) throw new Error('KV GET exploded'); };
    const g = await call(env3, '/state', {headers: {Cookie: s3.cookie}});
    rec('M-127-c', 'an unexpected throw anywhere → 500 WITH CORS headers (was: bare network error)',
      g.status === 500 && hasCors(g) && json(g.text).error === 'internal_error', 'status=' + g.status + ' ' + JSON.stringify(g.headers));
  }

  // ---------------- 13. Compatibility matrix ----------------
  {
    // new Worker × new client
    const kv = new KVMock(); const env = makeEnv(kv);
    const {cookie} = await signIn(env);
    const C = {Cookie: cookie, 'Content-Type': 'application/json'};
    const a = await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({favorites: ['f1'], blacklist: [], bookmarks: [{id: 'b1', emoji: '📍'}], baseV: 0, w: 'wid-1'})});
    rec('MATRIX-1', 'new Worker × new client (baseV + w) → {"v":1}, w stored', a.text === '{"v":1}' && stored(kv).w === 'wid-1');
    // new Worker × old client
    const b = await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({favorites: ['f2'], blacklist: [], bookmarks: []})});
    const sb = stored(kv);
    rec('MATRIX-2', 'new Worker × old client (no baseV) → "ok", union, w kept',
      b.text === 'ok' && sb.favorites.includes('f1') && sb.favorites.includes('f2') && sb.bookmarks.length === 1 && sb.w === 'wid-1', JSON.stringify(sb));

    // old Worker × new client
    const kvL = new KVMock(); const envL = makeEnv(kvL);
    const sL = await callOn(legacyWorker, envL, '/session', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id_token: tok('alice')})});
    const cookieL = (sL.cookies[0] || '').split(';')[0];
    const l1 = await callOn(legacyWorker, envL, '/state', {method: 'PUT', headers: {Cookie: cookieL, 'Content-Type': 'application/json'}, body: JSON.stringify({favorites: ['f1'], blacklist: [], bookmarks: [], baseV: 0, w: 'wid-1'})});
    rec('MATRIX-3', 'old Worker × new client body → 200 {"v":1} and w is stored verbatim',
      l1.status === 200 && l1.text === '{"v":1}' && json(kvL.primary('state:1001')).w === 'wid-1', l1.text + ' ' + kvL.primary('state:1001'));
    // …and the new Worker reads what the old one wrote, keeping w.
    const l2 = await call(env, '/state', {method: 'PUT', headers: C, body: JSON.stringify({favorites: ['f3'], blacklist: [], bookmarks: [], baseV: stored(kv).v})});
    rec('MATRIX-4', 'new Worker picks up an old-Worker blob without losing w', l2.status === 200 && stored(kv).w === 'wid-1');
    // old Worker × old client — unchanged behaviour, documented for contrast.
    const l3 = await callOn(legacyWorker, envL, '/state', {method: 'PUT', headers: {Cookie: cookieL, 'Content-Type': 'application/json'}, body: JSON.stringify({favorites: ['stale'], blacklist: [], bookmarks: []})});
    rec('MATRIX-5', 'old Worker × old client is last-write-wins (the bug M-043 fixes; asserted so the contrast stays honest)',
      l3.text === 'ok' && json(kvL.primary('state:1001')).favorites.join() === 'stale');
    // A cookie minted by the NEW worker (no PII, sv claim) must work on the OLD one.
    const crossed = await callOn(legacyWorker, envL, '/state', {headers: {Cookie: cookie}});
    rec('MATRIX-6', 'a new-Worker cookie (no PII, sv claim) is still accepted by the old Worker', crossed.status === 200, 'status=' + crossed.status);
  }

  const pass = results.filter((r) => r.pass).length;
  const fail = results.length - pass;
  return {pass, fail, results};
}

let SRC = null;
async function workerSource() {
  if (SRC === null) {
    const {readFileSync} = await import('node:fs');
    const {fileURLToPath} = await import('node:url');
    SRC = readFileSync(fileURLToPath(new URL('../../worker/src/index.js', import.meta.url)), 'utf8');
  }
  return SRC;
}

const invokedDirectly = process.argv[1] && process.argv[1].endsWith('run.mjs');
if (invokedDirectly) {
  const {pass, fail, results} = await runAll();
  console.log('\n' + pass + '/' + results.length + ' assertions passed' + (fail ? ' — ' + fail + ' FAILED' : ''));
  process.exit(fail ? 1 : 0);
}
