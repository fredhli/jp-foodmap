// Tabelog map sync API.
// Auth: client sends Google ID token as Bearer; we delegate verification to
// Google's tokeninfo endpoint (one outbound fetch, no JWKS bookkeeping).
// Storage: one JSON blob per user under KV key state:<google_sub>.

export default {
  async fetch(req, env) {
    const origin = req.headers.get('Origin') || '';
    const allowed = (env.ALLOWED_ORIGINS || '').split(',').map(s => s.trim());
    const cors = {
      'Access-Control-Allow-Origin': allowed.includes(origin) ? origin : allowed[0] || '*',
      'Access-Control-Allow-Headers': 'Authorization,Content-Type',
      'Access-Control-Allow-Methods': 'GET,PUT,OPTIONS',
      'Access-Control-Max-Age': '86400',
      'Vary': 'Origin',
    };

    if (req.method === 'OPTIONS') return new Response(null, {status: 204, headers: cors});

    const url = new URL(req.url);
    if (url.pathname !== '/api/state') {
      return new Response('not found', {status: 404, headers: cors});
    }

    const auth = req.headers.get('Authorization') || '';
    const token = auth.startsWith('Bearer ') ? auth.slice(7) : '';
    if (!token) return new Response('no auth', {status: 401, headers: cors});

    let info;
    try {
      const r = await fetch('https://oauth2.googleapis.com/tokeninfo?id_token=' + encodeURIComponent(token));
      if (!r.ok) return new Response('bad token', {status: 401, headers: cors});
      info = await r.json();
    } catch (e) {
      return new Response('tokeninfo failed', {status: 502, headers: cors});
    }
    if (info.aud !== env.GOOGLE_CLIENT_ID) {
      return new Response('aud mismatch', {status: 401, headers: cors});
    }
    if (!info.sub) return new Response('no sub', {status: 401, headers: cors});

    const key = 'state:' + info.sub;

    if (req.method === 'GET') {
      const data = await env.KV.get(key);
      return new Response(data ?? '{}', {
        headers: {...cors, 'Content-Type': 'application/json'},
      });
    }
    if (req.method === 'PUT') {
      const body = await req.text();
      // 200 KB cap — sanity check, single user shouldn't approach this.
      if (body.length > 200_000) {
        return new Response('payload too large', {status: 413, headers: cors});
      }
      try { JSON.parse(body); }
      catch (_) { return new Response('invalid json', {status: 400, headers: cors}); }
      await env.KV.put(key, body);
      return new Response('ok', {headers: cors});
    }
    return new Response('method not allowed', {status: 405, headers: cors});
  },
};
