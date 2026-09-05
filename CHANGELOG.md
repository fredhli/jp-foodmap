# Changelog

All notable changes to jpfoodmap.com. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the site is
versioned loosely — a release is a batch of milestones, not a semver API.

Entries reference the audit ids (`M-###`) from
`audit_outputs/integrated-2026-09-05/00-final/附录C-问题全文卡片.md`, which
carry the mechanism, the evidence and the red lines for each change.

> Milestone agents append to `[Unreleased]` — add to a section, don't rewrite
> someone else's line. The integrator cuts the version heading at the end.

## [Unreleased]

### Added

- `DELETE /api/state`: deletes every key the Worker holds for the signed-in
  user (`state:`, `profile:`, `sv:`) and signs the browser out. `GET`/`PUT`
  are untouched; the route's `Allow` header advertises the new method.
  (M-008)
- `docs/privacy.html` — one page in Chinese, English and Japanese saying what
  is collected, where it lives, and how to delete it. Self-contained: no
  external CSS, JS or fonts, readable in light and dark. (M-008)
- `worker/scripts/backup-kv.sh` — timestamped local backup of the sync KV
  namespace (`wrangler kv key list` + one `get` per key, a manifest with the
  restore command). Refuses to run and exits non-zero when wrangler is not
  authenticated, so it can never write an empty backup that looks fine.
  (M-005)
- `tests/worker/` — offline test bench for the Worker: the real
  `worker/src/index.js` driven by `Request` objects against a Map-backed KV
  mock and a stubbed tokeninfo. 93 assertions, each tagged with the audit id
  it guards, including the new-Worker × old-client and old-Worker ×
  new-client compatibility matrix. `node tests/worker/run.mjs`.
- `SESSION_HMAC_PREV`: the session secret can be rotated without signing
  everyone out — verification tries the current secret then the previous one,
  signing always uses the current. (M-126)
- Non-HttpOnly hint cookie `tabelog_has_session=1` (domain-wide, no secret in
  it) so the page can tell whether probing `/api/me` is worth a round-trip
  even after a `localStorage` wipe. (M-013)

### Changed

- `PUT /api/state` preserves unknown top-level fields. The three known arrays
  are still replaced wholesale so deletions propagate, but anything else the
  stored blob carries is copied forward instead of being erased by a client
  that never heard of it; an explicit `null` deletes a field on purpose.
  This unblocks the write-id and preferences fields the sync work depends on.
  (M-044)
- A `PUT` without `baseV` (the pre-2026-07-28 page shell) is now merged into
  the stored blob — favorites/blacklist unioned, bookmarks unioned by id —
  instead of replacing it wholesale. It still answers the literal `ok`.
  Worst case it revives something that client had deleted; it can no longer
  wipe another device's data. (M-043)
- The 200 KB request cap is measured in bytes, not UTF-16 code units (450 KB
  of CJK used to slip through), and the 413 body now carries
  `{limit, used}`. (M-053)
- Session cookie is host-only (`api.jpfoodmap.com`) instead of domain-wide,
  and the JWT carries only `{sub, iat, exp, sv}` — the profile moved to the
  `profile:<sub>` KV key. `/api/me` returns the same shape as before and
  falls back to the old cookie's inline claims. Sign-in also clears the old
  domain-wide cookie. Everyone signs in once more after the deploy; no data
  is affected (KV is keyed by Google sub). (M-037)
- Every error response is a JSON envelope (`{error, message}`); every 405
  carries `Allow`; `/api/me` and `/api/state` carry
  `Cache-Control: private, no-store`. Success bodies are unchanged. (M-132)
- Documentation: `CLAUDE.md` gains `tabelog.syncBase` (flagged as
  un-deletable — clearing it makes a merge read the remote as deleted) and
  `tabelog.lang` in the localStorage list, the real Worker size, the new KV
  keys, and the read-modify-write semantics of `PUT`; `README.md` no longer
  claims the payload is embedded in `index.html`. (M-051)

### Fixed

- A cookie whose signature is not valid base64url returned 500 (`atob` threw
  outside the try block) instead of 401. `verifyJWT` is fully guarded, the
  router has an outer catch, and error responses now carry CORS headers — a
  500 used to reach the browser as an unreadable network error. (M-127)
- A JSON primitive or array request body returned 500 and, for arrays, was
  stored verbatim (losing the version counter). Now 400 with nothing
  written. (M-127)
- One bookmark with a non-string `emoji` no longer travels to every device:
  the entry is dropped server-side, the rest of the array survives. The
  deployed client throws on such an entry and wipes the whole bookmarks
  layer, built-in pins included. (M-042)
- An unparseable stored blob is returned as `{}` on `GET` and in the 409
  body, so the client voids its base, unions and re-uploads instead of
  getting stuck permanently dirty. (M-129)
- A failed `KV.put` (1 write/s/key, or the daily quota) answers 429/503 with
  `Retry-After` and CORS headers instead of an uncaught 500. (M-040)

### Security

- Signing out is now a real revocation: `DELETE /api/session` bumps
  `sv:<sub>`, which invalidates cookies minted before it. Cookies issued
  before this deploy carry no `sv` and stay valid, as intended. (M-037)
- The session JWT no longer carries email, display name or avatar URL, and is
  no longer sent to every `*.jpfoodmap.com` host. (M-037)
- A malformed `id_token` is rejected before the outbound call to Google, and
  that call has a 5 s timeout. (M-040)
