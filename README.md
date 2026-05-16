# jp-foodmap

A personal interactive map of restaurants in the Kansai region. The static
page lives in `docs/` and is served via GitHub Pages.

The deployed map is gated by a 6-digit access password; only the encrypted
content lives in the public HTML. Per-user favorites and dismissals sync
through a GitHub Gist (configured per-device, no shared secrets in the repo).

## Local build

```bash
uv sync
uv run python src/tabelog/scrape/map.py
open docs/index.html
```

The build step pulls geocodes via GSI AddressSearch (cached locally in
`data/cache/`), composes the payload, and AES-256-GCM-encrypts it with a
PBKDF2-SHA256 key derived from `ACCESS_PASSWORD` in `map.py`.

`data/` is gitignored — only the encrypted `docs/index.html` is committed.

## Deploy

The repo is already wired up to GitHub Pages serving from `main` / `/docs`.
Local workflow:

```bash
uv run python src/tabelog/scrape/map.py  # regenerate after data changes
git add docs/index.html
git commit -m "rebuild map"
git push
```

Pages rebuilds in ~1 minute.

## Sync (favorites + dismissals)

Each browser keeps a local copy of these lists and pushes diffs to a shared
secret GitHub Gist.

1. Create a **secret** gist on gist.github.com containing two files:
   - `favorites.json` — JSON array of detail URLs
   - `blacklist.json` — JSON array of detail URLs
2. Create a fine-grained PAT at github.com/settings/tokens with **Gists →
   Read and write** only.
3. On the deployed page → ⚙️ 同步设置 → paste Gist ID + PAT → 保存并测试.

Read-only viewers can fill in just the Gist ID and leave the PAT blank.

## Notes on the access gate

- The password (in `ACCESS_PASSWORD` at the top of `map.py`) gets hashed
  with PBKDF2 (200k iterations, SHA-256) into an AES-256 key. The HTML
  source contains only the salt + IV + ciphertext.
- On successful unlock the derived key is cached in `localStorage` for 24h
  so reloads are instant.
- This is client-side encryption: a determined attacker with the HTML can
  brute-force 10^6 6-digit combinations against the PBKDF2 step. For real
  authentication, put the page behind Cloudflare Access (or similar).
