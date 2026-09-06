#!/usr/bin/env bash
#
# App Links, end to end: does the APK's signature match what the site publishes, and does
# the device actually route https://jpfoodmap.com/... to this app?
#
#   tools/applinks-check.sh                      # signature + assetlinks, offline
#   tools/applinks-check.sh --live               # ...and fetch the deployed assetlinks.json
#   tools/applinks-check.sh --device             # ...and read pm get-app-links off a device
#   tools/applinks-check.sh --approve            # ...and force-approve the domain first
#   tools/applinks-check.sh --apk PATH --package com.fredhli.jpfoodmap.debug --approve --device
#
# Three separate claims, checked separately because they fail for different reasons:
#
#   1. the APK is signed with the key whose SHA-256 the site names (apksigner vs the JSON);
#   2. the site's assetlinks.json is well-formed, lists this package and that fingerprint;
#   3. the device has the domain associated with the package.
#
# (3) is the one that will not hold on an emulator on its own. Domain verification needs
# https://jpfoodmap.com/.well-known/assetlinks.json to already carry this package, and until
# main is pushed it does not — so --approve sets the association by hand, which is exactly
# what `pm set-app-links` exists for (docs/STANDARDS.md §6.1). On the real phone, after the
# site ships, verification happens on install and --approve is unnecessary.
#
# Exit codes: 0 all requested checks passed, 1 a check failed, 2 usage / missing tool.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "$HERE/env.sh" >/dev/null 2>&1 || true
SRC="${JPFM_ANDROID_SRC:-$(cd "$HERE/.." && pwd -P)}"
REPO="$(cd "$SRC/.." && pwd -P)"

ASSETLINKS="$REPO/docs/.well-known/assetlinks.json"
LIVE_URL="https://jpfoodmap.com/.well-known/assetlinks.json"
DOMAIN="jpfoodmap.com"
PKG="com.fredhli.jpfoodmap"
APK=""
DO_LIVE=0
DO_DEVICE=0
DO_APPROVE=0
SERIAL="${ANDROID_SERIAL:-}"

fails=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; fails=$((fails + 1)); }
skip() { printf '  SKIP  %s\n' "$*"; }
head_() { printf '\n%s\n' "$*"; }
die()  { printf 'applinks-check.sh: %s\n' "$*" >&2; exit 2; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --apk)     APK="${2:-}"; shift 2 ;;
        --package) PKG="${2:-}"; shift 2 ;;
        --serial)  SERIAL="${2:-}"; shift 2 ;;
        --domain)  DOMAIN="${2:-}"; shift 2 ;;
        --live)    DO_LIVE=1; shift ;;
        --device)  DO_DEVICE=1; shift ;;
        --approve) DO_APPROVE=1; DO_DEVICE=1; shift ;;
        -h|--help) sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)         die "unknown argument: $1" ;;
    esac
done

# The release APK is what ships; the debug one is what the emulator gets. Whichever is
# asked for, the certificate is the same (~/.android/debug.keystore, docs/PLAN.md D2) —
# that is the whole reason one fingerprint covers both package names.
if [ -z "$APK" ]; then
    for cand in \
        "$SRC/apk/jpfoodmap.apk" \
        "${JPFM_ANDROID_BUILD:-$HOME/.cache/jpfoodmap-android}/app/build/outputs/apk/release/app-release.apk" \
        "${JPFM_ANDROID_BUILD:-$HOME/.cache/jpfoodmap-android}/app/build/outputs/apk/debug/app-debug.apk"
    do
        [ -f "$cand" ] && { APK="$cand"; break; }
    done
fi

command -v python3 >/dev/null || die "python3 is not installed"

# ---- 1. the file in the repo ------------------------------------------------------------
head_ "assetlinks.json  ($ASSETLINKS)"
if [ ! -f "$ASSETLINKS" ]; then
    bad "missing — the site publishes nothing, so nothing can verify"
    JSON_FPS=""
else
    # One python pass: it validates the JSON, checks the shape Google's verifier requires,
    # and prints "package<TAB>fingerprint" lines for the shell to compare against.
    parsed="$(python3 - "$ASSETLINKS" <<'PY'
import json, re, sys
path = sys.argv[1]
try:
    doc = json.load(open(path, encoding="utf-8"))
except Exception as exc:
    print("ERR\tnot valid JSON: %s" % exc)
    raise SystemExit(0)
if not isinstance(doc, list) or not doc:
    print("ERR\ttop level must be a non-empty array")
    raise SystemExit(0)
FP = re.compile(r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
for i, entry in enumerate(doc):
    rel = entry.get("relation")
    if rel != ["delegate_permission/common.handle_all_urls"]:
        print("ERR\tentry %d: relation is %r" % (i, rel))
    tgt = entry.get("target") or {}
    if tgt.get("namespace") != "android_app":
        print("ERR\tentry %d: namespace is %r" % (i, tgt.get("namespace")))
    pkg = tgt.get("package_name") or ""
    fps = tgt.get("sha256_cert_fingerprints") or []
    if not pkg:
        print("ERR\tentry %d: no package_name" % i)
    if not fps:
        print("ERR\tentry %d: no sha256_cert_fingerprints" % i)
    for fp in fps:
        if not FP.match(fp):
            print("ERR\tentry %d: fingerprint is not 32 upper-case hex bytes: %r" % (i, fp))
        else:
            print("PKG\t%s\t%s" % (pkg, fp))
PY
)"
    if printf '%s\n' "$parsed" | grep -q '^ERR'; then
        printf '%s\n' "$parsed" | sed -n 's/^ERR\t/  /p' | while read -r line; do
            printf '  \033[31mFAIL\033[0m  %s\n' "$line"
        done
        fails=$((fails + 1))
    else
        ok "valid JSON, every entry is a handle_all_urls android_app delegation"
    fi
    JSON_FPS="$(printf '%s\n' "$parsed" | sed -n 's/^PKG\t//p')"
    printf '%s\n' "$JSON_FPS" | sed 's/\t/  ->  /' | sed 's/^/        /'
    for want in "$PKG" "$PKG.debug"; do
        case "$want" in *.debug.debug) continue ;; esac
        if printf '%s\n' "$JSON_FPS" | cut -f1 | grep -qx "$want"; then
            ok "lists $want"
        else
            bad "does not list $want"
        fi
    done
fi

# ---- 2. the signature on the APK ---------------------------------------------------------
head_ "APK signature"
if [ -z "$APK" ] || [ ! -f "$APK" ]; then
    skip "no APK found — build one with android/build.sh (or pass --apk PATH)"
elif ! command -v apksigner >/dev/null; then
    skip "apksigner is not on PATH — source android/tools/env.sh first"
else
    printf '        %s\n' "$APK"
    apk_fp="$(apksigner verify --print-certs "$APK" 2>/dev/null \
        | sed -n 's/.*SHA-256 digest: *//p' | head -1)"
    if [ -z "$apk_fp" ]; then
        bad "apksigner printed no SHA-256 digest (is the APK signed?)"
    else
        # apksigner prints 64 lower-case hex characters; assetlinks wants colon-separated
        # upper case. Normalise both to bare lower-case hex before comparing.
        apk_norm="$(printf '%s' "$apk_fp" | tr -d ': \r\n' | tr 'A-F' 'a-f')"
        printf '        apk:  %s\n' "$apk_norm"
        matched=0
        while IFS=$'\t' read -r p f; do
            [ -n "${f:-}" ] || continue
            n="$(printf '%s' "$f" | tr -d ':' | tr 'A-F' 'a-f')"
            if [ "$n" = "$apk_norm" ]; then matched=1; fi
        done <<EOF
$JSON_FPS
EOF
        if [ "$matched" = 1 ]; then
            ok "matches every fingerprint in assetlinks.json"
        else
            bad "does NOT match assetlinks.json — App Links would never verify"
        fi
    fi
fi

# ---- 3. the live file ---------------------------------------------------------------------
head_ "deployed file  ($LIVE_URL)"
if [ "$DO_LIVE" != 1 ]; then
    skip "not requested (--live)"
elif ! command -v curl >/dev/null; then
    skip "curl is not installed"
else
    # A GET, not a HEAD: Cloudflare Pages answers HEAD for a missing path with the 404
    # page's headers and a 200-ish shape, so a HEAD would call a missing file "served as
    # text/html" instead of "not there". Google's verifier does a GET too.
    body="$(mktemp)"
    read -r code ctype <<EOF
$(curl -sS --max-time 15 -o "$body" -w '%{http_code} %{content_type}' "$LIVE_URL" 2>/dev/null)
EOF
    printf '        HTTP %s   content-type: %s\n' "${code:-000}" "${ctype:-<none>}"
    if [ "${code:-000}" != "200" ]; then
        bad "HTTP ${code:-000} — expected until main is pushed with docs/.well-known/assetlinks.json"
    else
        case "$ctype" in
            application/json*) ok "200 with application/json" ;;
            *)                 bad "served as '${ctype:-<none>}' — Google's verifier wants application/json" ;;
        esac
        if diff -q "$body" "$ASSETLINKS" >/dev/null 2>&1; then
            ok "byte-identical to the file in this repo"
        else
            bad "differs from docs/.well-known/assetlinks.json (deploy is behind, or was edited)"
        fi
    fi
    rm -f "$body"
fi

# ---- 4. the device -------------------------------------------------------------------------
head_ "device association  ($PKG / $DOMAIN)"
if [ "$DO_DEVICE" != 1 ]; then
    skip "not requested (--device / --approve)"
elif ! command -v adb >/dev/null; then
    skip "adb is not on PATH — source android/tools/env.sh first"
else
    ADB=(adb)
    [ -n "$SERIAL" ] && ADB=(adb -s "$SERIAL")
    if ! "${ADB[@]}" shell true >/dev/null 2>&1; then
        skip "no device (set --serial, or start one with tools/emu.sh start)"
    elif ! "${ADB[@]}" shell pm path "$PKG" >/dev/null 2>&1; then
        bad "$PKG is not installed on the device"
    else
        if [ "$DO_APPROVE" = 1 ]; then
            # Two commands, because they set two different things and Android needs both to
            # actually route a link on an unverified domain:
            #   set-app-links               the verification state (1 = STATE_SUCCESS)
            #   set-app-links-user-selection what the user picked in "Open by default"
            # Neither is available before API 31; both are no-ops that print nothing on
            # success, so their output is only shown when they complain.
            out1="$("${ADB[@]}" shell pm set-app-links --package "$PKG" 1 "$DOMAIN" 2>&1 | tr -d '\r')"
            out2="$("${ADB[@]}" shell pm set-app-links-user-selection --user 0 --package "$PKG" true "$DOMAIN" 2>&1 | tr -d '\r')"
            [ -n "$out1" ] && printf '        set-app-links: %s\n' "$out1"
            [ -n "$out2" ] && printf '        user-selection: %s\n' "$out2"
            ok "forced the association (emulator only; the phone verifies for real)"
        fi
        links="$("${ADB[@]}" shell pm get-app-links "$PKG" 2>&1 | tr -d '\r')"
        printf '%s\n' "$links" | sed 's/^/        /'
        if printf '%s\n' "$links" | grep -q "$DOMAIN"; then
            ok "pm get-app-links lists $DOMAIN"
        else
            bad "pm get-app-links does not mention $DOMAIN — is the intent-filter in the APK?"
        fi
        # "verified" on the phone, "approved" after --approve on the emulator; either one
        # means a VIEW intent for the domain resolves to this app without a chooser.
        if printf '%s\n' "$links" | grep -Eq "$DOMAIN: *(verified|approved)"; then
            ok "the domain is associated (verified/approved)"
        else
            bad "the domain is listed but not associated — links will open in the browser"
        fi
    fi
fi

printf '\n'
if [ "$fails" -eq 0 ]; then
    printf 'applinks-check.sh: all requested checks passed\n'
    exit 0
fi
printf 'applinks-check.sh: %d check(s) failed\n' "$fails"
exit 1
