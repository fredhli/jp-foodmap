// Tabelog map sync API.
// Two auth paths:
//   1) Cookie session — POST /api/session exchanges a Google id_token for an
//      HMAC-signed JWT stored in a HttpOnly cookie scoped to .jpfoodmap.com.
//      The cookie lasts SESSION_TTL_SECS (90 days). All /api/state requests
//      auto-ride it; clients never have to round-trip Google again until the
//      session expires or is revoked via DELETE /api/session.
//   2) Bearer id_token (legacy) — kept alive for one release so pages loaded
//      against the previous Worker keep working. Will be removed.

const COOKIE_NAME = 'tabelog_session';
const SESSION_TTL_SECS = 90 * 24 * 60 * 60;   // 90 days
const COOKIE_DOMAIN = 'jpfoodmap.com';        // covers api.jpfoodmap.com + www
const TEXT = new TextEncoder();

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
async function verifyJWT(token, secret) {
  if (!token || typeof token !== 'string') return null;
  const parts = token.split('.');
  if (parts.length !== 3) return null;
  const data = parts[0] + '.' + parts[1];
  const sig = b64urlDecodeBytes(parts[2]);
  const key = await hmacKey(secret);
  let ok;
  try { ok = await crypto.subtle.verify('HMAC', key, sig, TEXT.encode(data)); }
  catch (_) { return null; }
  if (!ok) return null;
  let payload;
  try { payload = JSON.parse(b64urlDecodeStr(parts[1])); }
  catch (_) { return null; }
  if (!payload || typeof payload !== 'object') return null;
  if (typeof payload.exp !== 'number' || payload.exp * 1000 <= Date.now()) return null;
  if (!payload.sub) return null;
  return payload;
}

// ---------- Cookie helpers ----------

function readCookie(req, name) {
  const raw = req.headers.get('Cookie') || '';
  for (const part of raw.split(/;\s*/)) {
    const i = part.indexOf('=');
    if (i < 0) continue;
    if (part.slice(0, i) === name) {
      try { return decodeURIComponent(part.slice(i + 1)); }
      catch (_) { return part.slice(i + 1); }
    }
  }
  return '';
}
function buildSetCookie(value, maxAgeSecs) {
  // Domain=jpfoodmap.com is same-site for api.jpfoodmap.com → SameSite=Lax
  // suffices; no need for SameSite=None (which would invite ITP cleanup).
  return [
    `${COOKIE_NAME}=${value}`,
    `Domain=${COOKIE_DOMAIN}`,
    `Path=/`,
    `Max-Age=${maxAgeSecs}`,
    `SameSite=Lax`,
    `Secure`,
    `HttpOnly`,
  ].join('; ');
}

// ---------- Google id_token verification (existing path) ----------

async function verifyGoogleIdToken(token, expectedAud) {
  // tokeninfo endpoint validates signature, exp, aud, iss for us. One
  // outbound fetch per call — fine at our traffic shape, and avoids
  // having to maintain Google's JWKS rotation.
  let info;
  try {
    const r = await fetch('https://oauth2.googleapis.com/tokeninfo?id_token=' + encodeURIComponent(token));
    if (!r.ok) return null;
    info = await r.json();
  } catch (_) { return null; }
  if (info.aud !== expectedAud) return null;
  if (!info.sub) return null;
  return info;
}

// ---------- Auth resolution ----------

// Try cookie first; on miss, fall back to Bearer id_token. Returns
// {sub, email, name, picture} or null. Also returns `source` so callers
// can decide whether to refresh-as-cookie on the response.
async function resolveAuth(req, env) {
  if (env.SESSION_HMAC) {
    const c = readCookie(req, COOKIE_NAME);
    if (c) {
      const claims = await verifyJWT(c, env.SESSION_HMAC);
      if (claims) {
        return {
          source: 'cookie',
          sub: claims.sub,
          email: claims.email || '',
          name: claims.name || '',
          picture: claims.picture || '',
          exp: claims.exp,
        };
      }
    }
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

// ---------- Handlers ----------

async function handleSessionPost(req, env, cors) {
  if (!env.SESSION_HMAC) {
    return new Response('session disabled', {status: 503, headers: cors});
  }
  let body;
  try { body = await req.json(); }
  catch (_) { return new Response('invalid json', {status: 400, headers: cors}); }
  const idToken = body && body.id_token;
  if (!idToken) return new Response('no id_token', {status: 400, headers: cors});

  const info = await verifyGoogleIdToken(idToken, env.GOOGLE_CLIENT_ID);
  if (!info) return new Response('bad id_token', {status: 401, headers: cors});

  const nowSecs = Math.floor(Date.now() / 1000);
  const claims = {
    sub: info.sub,
    email: info.email || '',
    name: info.name || '',
    picture: info.picture || '',
    iat: nowSecs,
    exp: nowSecs + SESSION_TTL_SECS,
  };
  const jwt = await signJWT(claims, env.SESSION_HMAC);
  const headers = {
    ...cors,
    'Content-Type': 'application/json',
    'Set-Cookie': buildSetCookie(jwt, SESSION_TTL_SECS),
  };
  return new Response(JSON.stringify({
    sub: claims.sub,
    email: claims.email,
    name: claims.name,
    picture: claims.picture,
    exp: claims.exp * 1000,
  }), {headers});
}

function handleSessionDelete(_req, _env, cors) {
  const headers = {
    ...cors,
    'Set-Cookie': buildSetCookie('deleted', 0),
  };
  return new Response(null, {status: 204, headers});
}

async function handleMe(req, env, cors) {
  if (!env.SESSION_HMAC) return new Response('no session', {status: 401, headers: cors});
  const c = readCookie(req, COOKIE_NAME);
  if (!c) return new Response('no session', {status: 401, headers: cors});
  const claims = await verifyJWT(c, env.SESSION_HMAC);
  if (!claims) {
    // Bad/expired cookie — proactively clear it so the browser doesn't keep
    // sending a value we'll just keep rejecting.
    return new Response('no session', {
      status: 401,
      headers: {...cors, 'Set-Cookie': buildSetCookie('deleted', 0)},
    });
  }
  return new Response(JSON.stringify({
    sub: claims.sub,
    email: claims.email || '',
    name: claims.name || '',
    picture: claims.picture || '',
    exp: claims.exp * 1000,
  }), {headers: {...cors, 'Content-Type': 'application/json'}});
}

async function handleState(req, env, cors) {
  const auth = await resolveAuth(req, env);
  if (!auth) return new Response('no auth', {status: 401, headers: cors});

  const key = 'state:' + auth.sub;
  if (req.method === 'GET') {
    const data = await env.KV.get(key);
    return new Response(data ?? '{}', {
      headers: {...cors, 'Content-Type': 'application/json'},
    });
  }
  // PUT
  const body = await req.text();
  if (body.length > 200_000) {
    return new Response('payload too large', {status: 413, headers: cors});
  }
  try { JSON.parse(body); }
  catch (_) { return new Response('invalid json', {status: 400, headers: cors}); }
  await env.KV.put(key, body);
  return new Response('ok', {headers: cors});
}

// ---------- Router ----------

export default {
  async fetch(req, env) {
    const cors = corsHeaders(req, env);
    if (req.method === 'OPTIONS') return new Response(null, {status: 204, headers: cors});

    const url = new URL(req.url);
    const path = url.pathname;

    if (path === '/api/session') {
      if (req.method === 'POST')   return handleSessionPost(req, env, cors);
      if (req.method === 'DELETE') return handleSessionDelete(req, env, cors);
      return new Response('method not allowed', {status: 405, headers: cors});
    }
    if (path === '/api/me') {
      if (req.method === 'GET') return handleMe(req, env, cors);
      return new Response('method not allowed', {status: 405, headers: cors});
    }
    if (path === '/api/state') {
      if (req.method === 'GET' || req.method === 'PUT') return handleState(req, env, cors);
      return new Response('method not allowed', {status: 405, headers: cors});
    }
    return new Response('not found', {status: 404, headers: cors});
  },
};
