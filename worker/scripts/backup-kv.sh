#!/usr/bin/env bash
# M-005 — back up the sync KV namespace to a timestamped local directory.
#
# Every signed-in user's favorites / blacklist / bookmarks live in exactly one
# KV key (state:<google-sub>) in one namespace. Cloudflare KV has no
# point-in-time restore and namespace deletion is irreversible, so the only
# safety net is a copy somewhere else. Run this before every Worker deploy and
# once a week otherwise; keep the output OUTSIDE this repo (it contains
# personal data — email, display name, saved places).
#
#   bash worker/scripts/backup-kv.sh [output-dir]
#
# Requires `wrangler login` first (see worker/DEPLOY-PENDING.md). Without it
# the script stops with a clear message and a non-zero exit — it never writes
# an empty backup that looks like a successful one.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER_DIR="$(dirname "$HERE")"
CONFIG="$WORKER_DIR/wrangler.toml"

# Namespace id comes from wrangler.toml so this can never drift from the
# binding the Worker actually uses.
NAMESPACE_ID="$(sed -n '/\[\[kv_namespaces\]\]/,/^\[/p' "$CONFIG" | sed -n 's/^id *= *"\([^"]*\)".*/\1/p' | head -n1)"
if [ -z "$NAMESPACE_ID" ]; then
  echo "backup-kv: could not read the KV namespace id from $CONFIG" >&2
  exit 1
fi

OUT_ROOT="${1:-$HOME/jpfoodmap-kv-backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$OUT_ROOT/$STAMP"

WRANGLER=(npx --yes wrangler)

echo "backup-kv: namespace $NAMESPACE_ID"
echo "backup-kv: checking wrangler auth…"
# `wrangler whoami` exits 0 even when it is NOT authenticated, so check what it
# actually said — a backup that quietly writes zero keys is worse than none.
WHOAMI_OUT="$("${WRANGLER[@]}" whoami 2>&1 || true)"
if ! printf '%s' "$WHOAMI_OUT" | grep -qi 'you are logged in'; then
  cat >&2 <<'EOF'
backup-kv: wrangler is not authenticated on this machine.

  Run this first (opens a browser):

      cd worker && npx wrangler login

  Then re-run this script. Nothing was written.
EOF
  exit 2
fi

mkdir -p "$OUT/keys"
echo "backup-kv: writing to $OUT"

KEYLIST="$OUT/keys.json"
"${WRANGLER[@]}" kv key list --remote --namespace-id "$NAMESPACE_ID" > "$KEYLIST"

# No jq dependency — node is already required to run wrangler.
mapfile -t KEYS < <(node -e '
  const fs = require("fs");
  const rows = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
  for (const r of rows) process.stdout.write(r.name + "\n");
' "$KEYLIST")

echo "backup-kv: ${#KEYS[@]} keys"
COUNT=0
for KEY in "${KEYS[@]}"; do
  [ -n "$KEY" ] || continue
  # ':' and '/' are legal in KV key names but awkward in filenames.
  SAFE="$(printf '%s' "$KEY" | tr '/:' '__')"
  "${WRANGLER[@]}" kv key get --remote --namespace-id "$NAMESPACE_ID" "$KEY" > "$OUT/keys/$SAFE.json"
  COUNT=$((COUNT + 1))
done

cat > "$OUT/MANIFEST.txt" <<EOF
jpfoodmap KV backup
taken:        $STAMP (UTC)
namespace:    $NAMESPACE_ID
keys:         $COUNT
wrangler:     $("${WRANGLER[@]}" --version 2>/dev/null | tail -n1)
restore:      npx wrangler kv key put --remote --namespace-id $NAMESPACE_ID "<key>" --path keys/<file>.json
note:         contains personal data (email, display name, saved places).
              Keep it out of the repo and out of any shared folder.
EOF

echo "backup-kv: done — $COUNT keys in $OUT"
