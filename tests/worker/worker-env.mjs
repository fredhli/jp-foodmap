// Fake env + tokeninfo stub shared by the Worker tests.
// Lifted from audit_outputs/fable-2026-09-05/04-sync-and-worker/harness/,
// with repo-relative imports so it runs from a fresh clone.
//
// Nothing here touches the network, the production Worker, KV, or Google.
import {KVMock} from './kv-mock.mjs';

export const CLIENT_ID = '536198170238-me7dpu2og75tseuekl3pu8rjjgo2ig2p.apps.googleusercontent.com';
export const ORIGIN = 'https://jpfoodmap.com';

// A syntactically plausible id_token — the Worker now shape-checks before it
// spends a subrequest on tokeninfo (M-040), so harness tokens need three
// dot-separated parts and some length.
const pad = (s) => s + 'x'.repeat(Math.max(0, 20 - s.length));
export const tok = (name) => `hdr.${pad(name)}.sig${'z'.repeat(20)}`;

// id_token string -> tokeninfo payload. Anything else -> HTTP 400 like Google.
export const TOKENS = {
  [tok('alice')]:    {sub: '1001', email: 'alice@example.com', name: 'Alice', picture: 'https://p/alice.png', aud: CLIENT_ID},
  [tok('bob')]:      {sub: '1002', email: 'bob@example.com',   name: 'Bob',   picture: '',                    aud: CLIENT_ID},
  [tok('wrongaud')]: {sub: '1003', email: 'x@example.com', aud: 'other-client-id'},
  [tok('nosub')]:    {email: 'nosub@example.com', aud: CLIENT_ID},
};
export const tokeninfoState = {mode: 'ok', calls: 0};   // 'ok' | 'down' | 'timeout' | 'html'

globalThis.fetch = async function stubbedFetch(url, opts) {
  const u = String(url);
  if (u.startsWith('https://oauth2.googleapis.com/tokeninfo')) {
    tokeninfoState.calls++;
    if (opts && opts.signal && opts.signal.aborted) throw new Error('aborted');
    if (tokeninfoState.mode === 'down') throw new TypeError('fetch failed');
    if (tokeninfoState.mode === 'html') return new Response('<html>503</html>', {status: 503});
    if (tokeninfoState.mode === 'timeout') await new Promise((r) => setTimeout(r, 200));
    const t = new URL(u).searchParams.get('id_token');
    const info = TOKENS[t];
    if (!info) return new Response(JSON.stringify({error: 'invalid_token'}), {status: 400});
    return new Response(JSON.stringify(info), {status: 200, headers: {'Content-Type': 'application/json'}});
  }
  throw new Error('tests: unexpected outbound fetch ' + u);
};

export function makeEnv(kv, pop = 'FRA', overrides = {}) {
  return Object.assign({
    KV: kv.binding(pop),
    SESSION_HMAC: 'harness-secret-not-real',
    GOOGLE_CLIENT_ID: CLIENT_ID,
    ALLOWED_ORIGINS: 'https://jpfoodmap.com,https://www.jpfoodmap.com,http://localhost:8000',
  }, overrides);
}

// Mint a JWT the way the Worker does, so tests can forge expired / legacy /
// PREV-signed cookies without reaching into the Worker's private helpers.
export async function signJWT(payloadObj, secret) {
  const enc = new TextEncoder();
  const b64 = (b) => Buffer.from(b).toString('base64').replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
  const data = b64(enc.encode(JSON.stringify({alg: 'HS256', typ: 'JWT'}))) + '.' + b64(enc.encode(JSON.stringify(payloadObj)));
  const key = await crypto.subtle.importKey('raw', enc.encode(secret), {name: 'HMAC', hash: 'SHA-256'}, false, ['sign']);
  const sig = await crypto.subtle.sign('HMAC', key, enc.encode(data));
  return data + '.' + b64(new Uint8Array(sig));
}
export function decodeJWT(token) {
  return JSON.parse(Buffer.from(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString());
}

export {KVMock};
