// Tabelog map sync API.
// Two auth paths:
//   1) Cookie session — POST /api/session exchanges a Google id_token for an
//      HMAC-signed JWT stored in a host-only HttpOnly cookie on
//      api.jpfoodmap.com. The cookie lasts SESSION_TTL_SECS (90 days). All
//      /api/state requests auto-ride it; clients never have to round-trip
//      Google again until the session expires or is revoked via
//      DELETE /api/session.
//   2) Bearer id_token (legacy) — kept alive for one release so pages loaded
//      against the previous Worker keep working. Will be removed.
//
// Storage layout (KV) — keys are only ever added, never renamed:
//   state:<sub>    the sync blob {favorites, blacklist, bookmarks, v, ...}
//   profile:<sub>  {email, name, picture, t} — moved out of the JWT (M-037)
//   sv:<sub>       session version counter, bumped by DELETE /api/session
//                  so signing out actually revokes the cookie (M-037)
//
// PUT /api/state replaces the three known arrays wholesale (so deletions
// propagate) but PRESERVES every other top-level field the stored blob has
// (M-044) — send an explicit null to drop one.

const COOKIE_NAME = 'tabelog_session';
const HINT_COOKIE_NAME = 'tabelog_has_session';   // M-013: readable by the page
const SESSION_TTL_SECS = 90 * 24 * 60 * 60;   // 90 days
const LEGACY_COOKIE_DOMAIN = 'jpfoodmap.com'; // M-037: only cleared now, never set
const HINT_COOKIE_DOMAIN = 'jpfoodmap.com';   // hint carries no secret — page reads it
const MAX_BODY_BYTES = 200_000;               // M-053: bytes, not UTF-16 units
const TOKENINFO_TIMEOUT_MS = 5000;            // M-040
// M-044: everything outside this set survives a PUT untouched.
const KNOWN_STATE_FIELDS = ['favorites', 'blacklist', 'bookmarks', 'v'];
const STATE_ARRAYS = ['favorites', 'blacklist', 'bookmarks'];
const TEXT = new TextEncoder();
// M-132: sync state is per-user and must never sit in a shared/heuristic cache.
const NO_STORE = {'Cache-Control': 'private, no-store'};

// ---------- CORS ----------

function corsHeaders(req, env) {
  const origin = req.headers.get('Origin') || '';
  const allowed = (env.ALLOWED_ORIGINS || '').split(',').map(s => s.trim()).filter(Boolean);
  const echo = allowed.includes(origin) ? origin : '';
  // Credentialed CORS requires a specific origin echo — never '*'. If the
  // request comes from an unknown origin we deliberately omit Allow-Origin
  // so the browser blocks it.
  const h = {
    'Access-Control-Allow-Headers': 'Authorization,Content-Type',
    'Access-Control-Allow-Methods': 'GET,PUT,POST,DELETE,OPTIONS',
    'Access-Control-Max-Age': '86400',
    'Vary': 'Origin',
  };
  if (echo) {
    h['Access-Control-Allow-Origin'] = echo;
    h['Access-Control-Allow-Credentials'] = 'true';
  }
  return h;
}

// ---------- Responses ----------

// M-132: one JSON envelope for every error. Success bodies are untouched —
// GET/409 still return the raw blob, versioned PUT still {v}, legacy PUT
// still the literal text 'ok' (deployed clients parse exactly that).
function jsonError(status, code, message, cors, extra, extraHeaders) {
  const body = Object.assign({error: code, message}, extra || {});
  return new Response(JSON.stringify(body), {
    status,
    headers: Object.assign({}, cors, NO_STORE, {'Content-Type': 'application/json'}, extraHeaders || {}),
  });
}
function jsonOk(bodyText, cors, extraHeaders) {
  return new Response(bodyText, {
    headers: Object.assign({}, cors, NO_STORE, {'Content-Type': 'application/json'}, extraHeaders || {}),
  });
}

// ---------- JWT (HS256) ----------

function b64urlFromBytes(bytes) {
  let bin = '';
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
}
function b64urlFromStr(s) { return b64urlFromBytes(TEXT.encode(s)); }
function b64urlDecodeBytes(s) {
  s = s.replace(/-/g, '+').replace(/_/g, '/');
  const pad = (4 - s.length % 4) % 4;
  const bin = atob(s + '='.repeat(pad));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
function b64urlDecodeStr(s) {
  const bytes = b64urlDecodeBytes(s);
  return new TextDecoder().decode(bytes);
}

async function hmacKey(secret) {
  return crypto.subtle.importKey(
    'raw', TEXT.encode(secret),
    {name: 'HMAC', hash: 'SHA-256'},
    false, ['sign', 'verify']
  );
}
async function signJWT(payload, secret) {
  const header = {alg: 'HS256', typ: 'JWT'};
  const h = b64urlFromStr(JSON.stringify(header));
  const p = b64urlFromStr(JSON.stringify(payload));
  const data = h + '.' + p;
  const key = await hmacKey(secret);
  const sig = await crypto.subtle.sign('HMAC', key, TEXT.encode(data));
  return data + '.' + b64urlFromBytes(new Uint8Array(sig));
}
// M-127: the whole body is guarded. b64urlDecodeBytes used to sit outside the
// try, so a cookie with a non-base64 signature threw InvalidCharacterError out
// of the handler → 500 (and, before the router's catch, without CORS headers).
async function verifyJWT(token, secret) {
  try {
    if (!token || typeof token !== 'string' || !secret) return null;
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    const data = parts[0] + '.' + parts[1];
    const sig = b64urlDecodeBytes(parts[2]);
    const key = await hmacKey(secret);
    const ok = await crypto.subtle.verify('HMAC', key, sig, TEXT.encode(data));
    if (!ok) return null;
    const payload = JSON.parse(b64urlDecodeStr(parts[1]));
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
    if (typeof payload.exp !== 'number' || payload.exp * 1000 <= Date.now()) return null;
    if (!payload.sub) return null;
    return payload;
  } catch (_) { return null; }
}

// M-126: rotation-friendly. Verify against the current secret first, then the
// previous one; always SIGN with the current. Rotation is: copy current into
// SESSION_HMAC_PREV → deploy → set a new SESSION_HMAC → deploy → drop PREV
// after 90 days (one cookie lifetime).
function sessionSecrets(env) {
  const out = [];
  if (env.SESSION_HMAC) out.push(env.SESSION_HMAC);
  if (env.SESSION_HMAC_PREV) out.push(env.SESSION_HMAC_PREV);
  return out;
}
async function verifySessionCookie(token, env) {
  for (const secret of sessionSecrets(env)) {
    const claims = await verifyJWT(token, secret);
    if (claims) {
      if (await sessionVersionOk(env, claims)) return claims;
      return null;
    }
  }
  return null;
}

// M-037: signing out bumps sv:<sub>, which invalidates every cookie minted
// before it. Cookies with no `sv` claim predate this deploy and stay valid —
// they cost no KV read either.
async function sessionVersionOk(env, claims) {
  if (typeof claims.sv !== 'number') return true;
  try {
    const raw = await env.KV.get('sv:' + claims.sub);
    const cur = raw ? (parseInt(raw, 10) || 0) : 0;
    return claims.sv === cur;
  } catch (_) {
    return true;   // KV hiccup must not sign everyone out
  }
}
async function readSessionVersion(env, sub) {
  try {
    const raw = await env.KV.get('sv:' + sub);
    return raw ? (parseInt(raw, 10) || 0) : 0;
  } catch (_) { return 0; }
}

// ---------- Cookie helpers ----------

// M-037: during the transition a browser can hold BOTH the old
// Domain=jpfoodmap.com cookie and the new host-only one under the same name,
// and their order in the Cookie header is not specified. Return every value
// so the caller can try them all.
function readCookies(req, name) {
  const raw = req.headers.get('Cookie') || '';
  const out = [];
  for (const part of raw.split(/;\s*/)) {
    const i = part.indexOf('=');
    if (i < 0) continue;
    if (part.slice(0, i) !== name) continue;
    const v = part.slice(i + 1);
    try { out.push(decodeURIComponent(v)); }
    catch (_) { out.push(v); }
  }
  return out;
}
function readCookie(req, name) {
  const all = readCookies(req, name);
  return all.length ? all[0] : '';
}
// Host-only: no Domain attribute, so the session JWT is sent to
// api.jpfoodmap.com only — not to Pages, assets.*, or any future subdomain.
function buildSetCookie(value, maxAgeSecs) {
  return [
    `${COOKIE_NAME}=${value}`,
    `Path=/`,
    `Max-Age=${maxAgeSecs}`,
    `SameSite=Lax`,
    `Secure`,
    `HttpOnly`,
  ].join('; ');
}
// M-037: same name, old scope — sent once alongside the new cookie so the
// stale domain-wide copy disappears on the first request after the deploy.
function buildLegacyClearCookie() {
  return [
    `${COOKIE_NAME}=deleted`,
    `Domain=${LEGACY_COOKIE_DOMAIN}`,
    `Path=/`,
    `Max-Age=0`,
    `SameSite=Lax`,
    `Secure`,
    `HttpOnly`,
  ].join('; ');
}
// M-013: non-HttpOnly, domain-wide, carries no secret. The page reads it to
// decide whether probing /api/me is worth a round-trip — it survives a
// localStorage wipe (ITP), unlike the tabelog.auth mirror.
function buildHintCookie(maxAgeSecs) {
  return [
    `${HINT_COOKIE_NAME}=${maxAgeSecs > 0 ? '1' : 'deleted'}`,
    `Domain=${HINT_COOKIE_DOMAIN}`,
    `Path=/`,
    `Max-Age=${maxAgeSecs}`,
    `SameSite=Lax`,
    `Secure`,
  ].join('; ');
}
// Multiple Set-Cookie headers need append(), not an object literal.
function headersWithCookies(base, cookies) {
  const h = new Headers(base);
  for (const c of cookies) h.append('Set-Cookie', c);
  return h;
}
function clearSessionCookies() {
  return [buildSetCookie('deleted', 0), buildLegacyClearCookie(), buildHintCookie(0)];
}

// ---------- Google id_token verification (existing path) ----------

async function verifyGoogleIdToken(token, expectedAud) {
  // tokeninfo endpoint validates signature, exp, aud, iss for us. One
  // outbound fetch per call — fine at our traffic shape, and avoids
  // having to maintain Google's JWKS rotation.
  if (!looksLikeIdToken(token)) return null;   // M-040: don't burn a subrequest
  let info;
  try {
    // M-040: a hung tokeninfo used to hold the Worker (and the client's
    // sign-in) open indefinitely.
    const r = await fetch('https://oauth2.googleapis.com/tokeninfo?id_token=' + encodeURIComponent(token), {
      signal: AbortSignal.timeout(TOKENINFO_TIMEOUT_MS),
    });
    if (!r.ok) return null;
    info = await r.json();
  } catch (_) { return null; }
  if (!info || typeof info !== 'object') return null;
  if (info.aud !== expectedAud) return null;
  if (!info.sub) return null;
  return info;
}

// M-040: cheap shape gate in front of the outbound call — a flood of junk
// tokens can no longer burn one Google subrequest each.
function looksLikeIdToken(token) {
  if (typeof token !== 'string') return false;
  if (token.length < 40 || token.length > 8192) return false;
  return token.split('.').length === 3;
}

// ---------- Auth resolution ----------

// Try cookie first; on miss, fall back to Bearer id_token. Returns
// {sub, email, name, picture} or null. Also returns `source` so callers
// can decide whether to refresh-as-cookie on the response.
async function resolveAuth(req, env) {
  const claims = await resolveCookieClaims(req, env);
  if (claims) {
    return {
      source: 'cookie',
      sub: claims.sub,
      // Empty on cookies minted after M-037 — /api/me reads profile:<sub>.
      email: claims.email || '',
      name: claims.name || '',
      picture: claims.picture || '',
      exp: claims.exp,
    };
  }
  const auth = req.headers.get('Authorization') || '';
  const token = auth.startsWith('Bearer ') ? auth.slice(7) : '';
  if (!token) return null;
  const info = await verifyGoogleIdToken(token, env.GOOGLE_CLIENT_ID);
  if (!info) return null;
  return {
    source: 'bearer',
    sub: info.sub,
    email: info.email || '',
    name: info.name || '',
    picture: info.picture || '',
    exp: null,
  };
}

// Every cookie value under COOKIE_NAME, current secret then PREV.
async function resolveCookieClaims(req, env) {
  if (!env.SESSION_HMAC && !env.SESSION_HMAC_PREV) {
    // M-126: silent fall-through to the Bearer path used to look like a
    // working deploy. Say it out loud in the tail.
    console.log('jpfoodmap-api: SESSION_HMAC is not configured — cookie sessions disabled');
    return null;
  }
  for (const c of readCookies(req, COOKIE_NAME)) {
    if (!c || c === 'deleted') continue;
    const claims = await verifySessionCookie(c, env);
    if (claims) return claims;
  }
  return null;
}

// ---------- Profile (M-037: PII lives in KV, not in the JWT) ----------

async function readProfile(env, sub) {
  try {
    const raw = await env.KV.get('profile:' + sub);
    if (!raw) return null;
    const p = JSON.parse(raw);
    if (!p || typeof p !== 'object' || Array.isArray(p)) return null;
    return p;
  } catch (_) { return null; }
}

// ---------- Handlers ----------

async function handleSessionPost(req, env, cors) {
  if (!env.SESSION_HMAC) {
    return jsonError(503, 'session_disabled', 'session disabled', cors);
  }
  let body;
  try { body = await req.json(); }
  catch (_) { return jsonError(400, 'invalid_json', 'invalid json', cors); }
  const idToken = body && body.id_token;
  if (!idToken) return jsonError(400, 'no_id_token', 'no id_token', cors);

  const info = await verifyGoogleIdToken(idToken, env.GOOGLE_CLIENT_ID);
  if (!info) return jsonError(401, 'bad_id_token', 'bad id_token', cors);

  const profile = {
    email: info.email || '',
    name: info.name || '',
    picture: info.picture || '',
    t: Date.now(),
  };
  // M-037: profile lives in KV so the cookie stops carrying PII. Best effort —
  // a KV blip must not block sign-in; /api/me falls back to the claims.
  try { await env.KV.put('profile:' + info.sub, JSON.stringify(profile)); }
  catch (_) { /* ignore */ }

  const nowSecs = Math.floor(Date.now() / 1000);
  const claims = {
    sub: info.sub,
    iat: nowSecs,
    exp: nowSecs + SESSION_TTL_SECS,
    sv: await readSessionVersion(env, info.sub),   // M-037
  };
  const jwt = await signJWT(claims, env.SESSION_HMAC);
  const headers = headersWithCookies(
    Object.assign({}, cors, NO_STORE, {'Content-Type': 'application/json'}),
    [buildSetCookie(jwt, SESSION_TTL_SECS), buildLegacyClearCookie(), buildHintCookie(SESSION_TTL_SECS)],
  );
  // Response shape unchanged — deployed pages read exactly these five fields.
  return new Response(JSON.stringify({
    sub: info.sub,
    email: profile.email,
    name: profile.name,
    picture: profile.picture,
    exp: claims.exp * 1000,
  }), {headers});
}

async function handleSessionDelete(req, env, cors) {
  // M-037: real revocation. Bump sv:<sub> so the cookie we just told the
  // browser to drop is also refused if it was copied elsewhere.
  const claims = await resolveCookieClaims(req, env);
  if (claims) {
    try {
      const cur = await readSessionVersion(env, claims.sub);
      await env.KV.put('sv:' + claims.sub, String(cur + 1));
    } catch (_) { /* best effort — the cookie still gets cleared */ }
  }
  const headers = headersWithCookies(Object.assign({}, cors, NO_STORE), clearSessionCookies());
  return new Response(null, {status: 204, headers});
}

async function handleMe(req, env, cors) {
  const claims = await resolveCookieClaims(req, env);
  if (!claims) {
    // Bad/expired cookie — proactively clear it so the browser doesn't keep
    // sending a value we'll just keep rejecting.
    const headers = headersWithCookies(Object.assign({}, cors, NO_STORE, {'Content-Type': 'application/json'}), clearSessionCookies());
    return new Response(JSON.stringify({error: 'no_session', message: 'no session'}), {status: 401, headers});
  }
  // Cookies minted before M-037 still carry the profile inline; new ones don't.
  const profile = await readProfile(env, claims.sub);
  return jsonOk(JSON.stringify({
    sub: claims.sub,
    email: (profile && profile.email) || claims.email || '',
    name: (profile && profile.name) || claims.name || '',
    picture: (profile && profile.picture) || claims.picture || '',
    exp: claims.exp * 1000,
  }), cors);
}

// M-042: known-bad shapes only. No field whitelist — that would fight M-044
// and silently eat fields a future client adds.
function sanitizeState(parsed) {
  for (const k of STATE_ARRAYS) {
    if (!(k in parsed)) continue;
    if (!Array.isArray(parsed[k])) { parsed[k] = []; continue; }
  }
  if (Array.isArray(parsed.bookmarks)) {
    parsed.bookmarks = parsed.bookmarks.filter(isUsableBookmark);
  }
  return parsed;
}
// One bad entry must not take the whole array with it: the deployed client's
// sanitizeBookmarkEmoji calls String.replace on a truthy non-string emoji and
// throws, wiping the layer (219 built-in pins included).
function isUsableBookmark(b) {
  if (!b || typeof b !== 'object' || Array.isArray(b)) return false;
  if (b.emoji != null && typeof b.emoji !== 'string') return false;
  return true;
}

// M-043: a client that sends no baseV is running the pre-2026-07-28 shell and
// would otherwise replace the whole blob. Union instead — worst case it
// revives something it had deleted; it can never drop another device's data.
function unionValues(curArr, newArr) {
  const out = [];
  const seen = new Set();
  for (const list of [curArr, newArr]) {
    if (!Array.isArray(list)) continue;
    for (const item of list) {
      let k;
      try { k = typeof item === 'string' ? item : 's:' + JSON.stringify(item); }
      catch (_) { k = null; }
      if (k === null) { out.push(item); continue; }
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(item);
    }
  }
  return out;
}
function unionBookmarks(curArr, newArr) {
  const out = [];
  const byId = new Map();
  const seenAnon = new Set();
  const push = (b) => {
    if (!b || typeof b !== 'object' || Array.isArray(b)) return;
    const id = typeof b.id === 'string' && b.id ? b.id : null;
    if (!id) {
      // No id — keep it, but don't let repeated legacy pushes duplicate it.
      let k;
      try { k = JSON.stringify(b); } catch (_) { k = null; }
      if (k !== null) {
        if (seenAnon.has(k)) return;
        seenAnon.add(k);
      }
      out.push(b);
      return;
    }
    if (byId.has(id)) { out[byId.get(id)] = b; return; }   // later wins
    byId.set(id, out.length);
    out.push(b);
  };
  if (Array.isArray(curArr)) for (const b of curArr) push(b);
  if (Array.isArray(newArr)) for (const b of newArr) push(b);
  return out;
}
function mergeLegacyPut(parsed, curObj) {
  for (const k of ['favorites', 'blacklist']) {
    if (!(k in parsed) && !(k in curObj)) continue;
    parsed[k] = unionValues(curObj[k], parsed[k]);
  }
  if ('bookmarks' in parsed || 'bookmarks' in curObj) {
    parsed.bookmarks = unionBookmarks(curObj.bookmarks, parsed.bookmarks);
  }
  return parsed;
}

// M-129: hand the client an empty object rather than an unparseable one — it
// voids its merge base, unions and re-uploads, which self-heals.
function readableBlob(raw) {
  if (!raw) return null;
  try {
    const o = JSON.parse(raw);
    if (!o || typeof o !== 'object' || Array.isArray(o)) return null;
    return o;
  } catch (_) { return null; }
}

async function handleState(req, env, cors) {
  const auth = await resolveAuth(req, env);
  if (!auth) return jsonError(401, 'no_auth', 'no auth', cors);

  const key = 'state:' + auth.sub;
  if (req.method === 'GET') {
    const data = await env.KV.get(key);
    return jsonOk(readableBlob(data) ? data : '{}', cors);
  }
  if (req.method === 'DELETE') return handleStateDelete(env, auth, cors);

  // PUT
  const body = await req.text();
  // M-053: bytes, not UTF-16 code units — 150k CJK characters used to slip
  // past a 200k limit at 450k bytes.
  const used = TEXT.encode(body).byteLength;
  if (used > MAX_BODY_BYTES) {
    return jsonError(413, 'payload_too_large', 'payload too large', cors, {limit: MAX_BODY_BYTES, used});
  }
  let parsed;
  try { parsed = JSON.parse(body); }
  catch (_) { return jsonError(400, 'invalid_json', 'invalid json', cors); }
  // M-127: a JSON primitive or array body used to throw on `parsed.v = …`
  // (strict mode) and surface as a 500 with no CORS headers.
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return jsonError(400, 'invalid_shape', 'body must be a JSON object', cors);
  }
  sanitizeState(parsed);

  // Optimistic concurrency. The stored blob carries a monotonically
  // increasing `v`. New clients echo the version they last saw as `baseV`;
  // a mismatch means another device wrote in between, so we return 409
  // with the current blob and let the client three-way-merge and retry.
  // Legacy clients don't send baseV and get a server-side union (M-043).
  // (KV has no true CAS — concurrent writes at different PoPs can still
  // race — but the realistic conflict is a stale device pushing hours or
  // days later, which this catches.)
  const cur = await env.KV.get(key);
  const curObj = readableBlob(cur);
  const curV = curObj && typeof curObj.v === 'number' ? curObj.v : 0;
  const versioned = typeof parsed.baseV === 'number';
  if (versioned && parsed.baseV !== curV) {
    return new Response(curObj ? cur : '{}', {
      status: 409,
      headers: Object.assign({}, cors, NO_STORE, {'Content-Type': 'application/json'}),
    });
  }
  delete parsed.baseV;
  if (!versioned && curObj) mergeLegacyPut(parsed, curObj);
  if (curObj) {
    // M-044: the three known arrays are replaced wholesale so deletions
    // propagate, but anything else the blob carries (a future client's
    // prefs, the write id w, …) survives a client that has never heard of it.
    for (const k of Object.keys(curObj)) {
      if (KNOWN_STATE_FIELDS.includes(k)) continue;
      if (k in parsed) continue;
      parsed[k] = curObj[k];
    }
  }
  // M-044: an explicit null is how a client deletes one of those fields.
  for (const k of Object.keys(parsed)) {
    if (KNOWN_STATE_FIELDS.includes(k)) continue;
    if (parsed[k] === null) delete parsed[k];
  }
  parsed.v = curV + 1;
  try {
    await env.KV.put(key, JSON.stringify(parsed));
  } catch (e) {
    // M-040: KV's 1 write/s/key and the daily quota used to surface as an
    // uncaught 500 with no CORS headers, so the page only saw "network error".
    const msg = String((e && e.message) || e);
    const throttled = /429|too many|rate/i.test(msg);
    return jsonError(
      throttled ? 429 : 503,
      throttled ? 'rate_limited' : 'storage_unavailable',
      'could not save state, retry shortly',
      cors, null, {'Retry-After': throttled ? '2' : '10'},
    );
  }
  if (versioned) {
    return jsonOk(JSON.stringify({v: parsed.v}), cors);
  }
  return new Response('ok', {headers: Object.assign({}, cors, NO_STORE)});
}

// M-008: "delete my cloud data". Removes every key this Worker writes for the
// user and signs the browser out. GET/PUT are untouched.
async function handleStateDelete(env, auth, cors) {
  try {
    await env.KV.delete('state:' + auth.sub);
    await env.KV.delete('profile:' + auth.sub);
    await env.KV.delete('sv:' + auth.sub);
  } catch (e) {
    return jsonError(503, 'storage_unavailable', 'could not delete state, retry shortly', cors, null, {'Retry-After': '10'});
  }
  const headers = headersWithCookies(Object.assign({}, cors, NO_STORE), clearSessionCookies());
  return new Response(null, {status: 204, headers});
}

// ---------- Router ----------

// M-132: 405s advertise what the route does accept.
function methodNotAllowed(allow, cors) {
  return jsonError(405, 'method_not_allowed', 'method not allowed', cors, null, {'Allow': allow});
}

async function route(req, env, cors) {
  if (req.method === 'OPTIONS') return new Response(null, {status: 204, headers: cors});

  const url = new URL(req.url);
  const path = url.pathname;

  if (path === '/api/session') {
    if (req.method === 'POST')   return handleSessionPost(req, env, cors);
    if (req.method === 'DELETE') return handleSessionDelete(req, env, cors);
    return methodNotAllowed('POST, DELETE, OPTIONS', cors);
  }
  if (path === '/api/me') {
    if (req.method === 'GET') return handleMe(req, env, cors);
    return methodNotAllowed('GET, OPTIONS', cors);
  }
  if (path === '/api/state') {
    if (req.method === 'GET' || req.method === 'PUT' || req.method === 'DELETE') {
      return handleState(req, env, cors);
    }
    return methodNotAllowed('GET, PUT, DELETE, OPTIONS', cors);
  }
  return jsonError(404, 'not_found', 'not found', cors);
}

export default {
  async fetch(req, env) {
    // M-127: anything that still escapes a handler comes back as a 500 that
    // the browser can actually read — an uncaught throw answered without CORS
    // headers, so the page saw a bare network error instead of a status.
    let cors = {};
    try {
      cors = corsHeaders(req, env);
      return await route(req, env, cors);
    } catch (e) {
      console.log('jpfoodmap-api: unhandled error', String((e && e.stack) || e));
      return jsonError(500, 'internal_error', 'internal error', cors);
    }
  },
};
