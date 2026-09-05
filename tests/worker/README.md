# Worker tests (offline)

```bash
node tests/worker/run.mjs            # one line per assertion, non-zero exit on failure
node --test tests/worker/*.test.mjs  # same suite through node:test
```

Node 24, no dependencies, no config. Everything is offline: the suite imports
the real `worker/src/index.js`, drives it with `Request` objects, and gives it
a Map-backed KV mock plus a stubbed `oauth2.googleapis.com/tokeninfo`. It never
touches the production Worker, the production KV namespace, Google, wrangler,
or git. (`globalThis.fetch` is replaced and throws on any unexpected host, so
an accidental outbound call fails loudly instead of leaking.)

## Files

| file | what it is |
| --- | --- |
| `run.mjs` | the suite — every assertion is tagged with the `M-###` it guards |
| `worker.test.mjs` | thin `node:test` wrapper around `run.mjs` |
| `kv-mock.mjs` | Map-backed KV with optional cross-PoP propagation delay, latency, 1-write/s/key enforcement, and failure hooks |
| `worker-env.mjs` | fake `env`, tokeninfo stub, JWT sign/decode helpers |
| `fixtures/worker-legacy.mjs` | snapshot of the Worker as deployed before this milestone (`git show HEAD:worker/src/index.js`), used for the old-Worker half of the compatibility matrix |

## What it covers

Grouped by the audit id the assertion defends:

- **M-044** unknown top-level fields (`prefs`, `favMeta`, the write id `w`)
  survive a PUT from a client that has never heard of them; the three known
  arrays are still replaced wholesale so deletions propagate; an explicit
  `null` deletes a field.
- **M-043** a PUT without `baseV` (pre-2026-07-28 client) is unioned into the
  stored blob instead of replacing it, and still answers the literal `ok`.
- **M-042 / M-127** known-bad shapes: a JSON primitive or array body → 400
  (was an uncaught 500 with no CORS headers), a mistyped `favorites` → `[]`,
  a bookmark whose `emoji` is not a string is dropped without taking the rest
  of the array with it, a garbage cookie → 401 + cleared cookie.
- **M-129** an unparseable stored blob reads back as `{}` on GET and on the
  409 body, so the client can self-heal.
- **M-053** the size limit is bytes, not UTF-16 code units, and the 413 body
  carries `{limit, used}`.
- **M-132** `Cache-Control: private, no-store` on `/api/me` and `/api/state`,
  `Allow` on every 405, one JSON envelope for every error.
- **M-040** malformed `id_token` never reaches Google; KV write failures
  become 429/503 with `Retry-After` instead of an uncaught 500.
- **M-037 / M-126 / M-013** host-only session cookie + one-shot clear of the
  old domain-wide one, PII out of the JWT and into `profile:<sub>`,
  `sv:<sub>` revocation on sign-out (cookies without `sv` stay valid),
  `SESSION_HMAC_PREV` dual-key verification, and the non-HttpOnly
  `tabelog_has_session` hint cookie.
- **M-008** `DELETE /api/state` — 401 unauthenticated, 204 + every key for
  that user gone + browser signed out.
- **Compatibility matrix** — new Worker × new client, new Worker × old client,
  old Worker × new client, old Worker × old client, and a new-Worker cookie
  against the old Worker. Since the Worker deploys separately from the page,
  all four combinations run in production at some point.

## Adding an assertion

`rec(id, name, pass, detail)` inside `runAll()`. Use the `M-###` as the id
prefix so a failure points straight at the audit card that explains why the
behaviour matters.
