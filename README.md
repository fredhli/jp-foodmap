# jp-foodmap

A personal interactive map of restaurants across Japan, live at
[jpfoodmap.com](https://jpfoodmap.com). The static page lives in `docs/`
and is served via Cloudflare Pages.

Per-user favorites, dismissals, and bookmarks sync through a small
Cloudflare Worker (`worker/`) backed by Google Sign-In. Visitors who skip
sign-in keep their state purely in `localStorage`.

## Local build

```bash
uv sync
uv run python src/tabelog/scrape/map.py
open docs/index.html
```

The build pulls geocodes via GSI AddressSearch (cached locally in
`data/cache/`) and embeds the payload directly into `docs/index.html`.

`data/` is mostly gitignored — only `data/favorites_builtin.json` and
`data/i18n/*.json` are committed (build-time inputs).

## Deploy

The repo is wired up to Cloudflare Pages serving from `main` / `/docs`.
`docs/_headers` sets cache rules (immutable emoji PNGs, no-cache sw.js).

```bash
uv run python src/tabelog/scrape/map.py  # regenerate after data changes
git add docs/index.html docs/data/restaurants.json
git commit -m "rebuild map"
git push
```

Pages rebuilds in ~1 minute.

The sync Worker deploys separately:

```bash
cd worker
wrangler deploy
```

## Sync (favorites + dismissals + bookmarks)

Sign in with Google on the deployed page → settings modal → click the
Google button. The Worker at `api.jpfoodmap.com` verifies the ID token via
Google's tokeninfo endpoint and persists state to Cloudflare KV, keyed by
Google `sub`. There is no setup beyond clicking sign-in.

See `CLAUDE.md` for the project-internal architecture notes.
