# Shared plumbing for verify-geometry.sh and verify-flows.sh. Sourced, never run.
#
# Everything here exists so the two verify scripts can be read as a list of acceptances
# instead of a list of adb calls, and so both write the same evidence layout:
#
#   audit_outputs/android-2026-09-06/<who>/<avd>/  summary.txt, *.png, *.json, *.log
#
# THE THREE VERDICTS. PASS and FAIL are obvious; SKIP is the one that makes these scripts
# usable during the build. Half the acceptances in docs/STANDARDS.md name a feature some
# task has not written yet (the T1 skeleton has no deep links, no diagnostics, no bridge),
# and a script that goes red on those teaches everyone to ignore red. So: an assertion whose
# SUBJECT is missing is SKIP with the reason; an assertion whose subject answered wrongly is
# FAIL. Only FAIL sets the exit code.

set -euo pipefail

LIBV_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ANDROID_DIR="$(cd "$LIBV_HERE/.." && pwd -P)"
REPO_DIR="$(cd "$ANDROID_DIR/.." && pwd -P)"

# shellcheck source=/dev/null
source "$HOME/tools/android-env.sh" >/dev/null 2>&1 || true

JPFM_WHO="${JPFM_WHO:-emu-acceptance}"
OUT_ROOT="${JPFM_OUT:-$REPO_DIR/audit_outputs/android-2026-09-06/$JPFM_WHO}"
APK="${JPFM_APK:-$ANDROID_DIR/apk/jpfoodmap.apk}"
EMU_PORT="${JPFM_EMU_PORT:-5554}"
SERIAL="emulator-${EMU_PORT}"
# A restaurant that is in every published corpus; ?r= is base36 of the numeric tail of the
# Tabelog detail URL (map.py shareIdOf). Override with JPFM_SHARE_ID / JPFM_SHARE_ID2.
SHARE_ID="${JPFM_SHARE_ID:-7r0vm}"      # 日本橋蛎殻町 すぎた  /13018162/
SHARE_ID2="${JPFM_SHARE_ID2:-7qymr}"    # 鮨 さいとう          /13015251/
SITE="${JPFM_SITE:-https://jpfoodmap.com}"

PASS_N=0; FAIL_N=0; SKIP_N=0
SUMMARY=""          # set by open_summary
CUR_AVD=""

# ------------------------------------------------------------------ evidence + verdicts
# Each script names its own file: geometry writes summary.txt, flows writes
# summary-flows.txt. They share the per-AVD directory, and one clobbering the other's
# verdicts is a silent loss of exactly the thing the run was for.
SUMMARY_FILE="${SUMMARY_FILE:-summary.txt}"

open_summary() {   # open_summary <avd> <title>
    CUR_AVD="$1"
    mkdir -p "$OUT_ROOT/$1"
    SUMMARY="$OUT_ROOT/$1/$SUMMARY_FILE"
    {
        printf '%s\n' "$2"
        printf 'avd        : %s\n' "$1"
        printf 'serial     : %s\n' "$SERIAL"
        printf 'apk        : %s\n' "$APK"
        printf 'package    : %s\n' "${PKG:-unknown}"
        printf 'when       : %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
        printf '%s\n' "----------------------------------------------------------------"
    } > "$SUMMARY"
}

_say() { printf '%s\n' "$*"; [ -n "$SUMMARY" ] && printf '%s\n' "$*" >> "$SUMMARY" || true; }
pass() { PASS_N=$(( PASS_N + 1 )); _say "PASS  $*"; }
fail() { FAIL_N=$(( FAIL_N + 1 )); _say "FAIL  $*"; }
skip() { SKIP_N=$(( SKIP_N + 1 )); _say "SKIP  $*"; }
info() { _say "      $*"; }
head2() { _say ""; _say "== $* =="; }

# expect <label> <got> <want>  — the workhorse. An empty "got" is SKIP, not FAIL.
expect() {
    local label="$1" got="$2" want="$3"
    if [ -z "$got" ] || [ "$got" = "null" ]; then
        skip "$label (not reported by this build)"
        return 0
    fi
    if [ "$got" = "$want" ]; then pass "$label = $got"; else fail "$label expected $want, got $got"; fi
}

# ------------------------------------------------------------------------- device glue
emu()  { JPFM_AVD="$AVD" JPFM_EMU_PORT="$EMU_PORT" JPFM_PKG="$PKG" "$LIBV_HERE/emu.sh" "$@"; }
diagsh() { JPFM_AVD="$AVD" JPFM_EMU_PORT="$EMU_PORT" JPFM_PKG="$PKG" "$LIBV_HERE/diag.sh" "$@"; }
adbs() { adb -s "$SERIAL" "$@"; }

# The package the APK under test actually declares — a debug APK is <id>.debug and every
# `am start` in here would otherwise address the wrong one.
detect_pkg() {
    local p=""
    if [ -f "$APK" ] && command -v aapt2 >/dev/null 2>&1; then
        p="$(aapt2 dump badging "$APK" 2>/dev/null \
             | sed -n "s/^package: name='\([^']*\)'.*/\1/p" | head -1)"
    fi
    PKG="${p:-${JPFM_PKG:-com.fredhli.jpfoodmap}}"
}

# Is the page reachable over DevTools? Debug builds only (setWebContentsDebuggingEnabled is
# BuildConfig.DEBUG), so every DOM-level assertion is conditional on this.
#
# Three tries: the socket appears a beat after the process does, and `am start -W` returns
# when the activity is drawn, which can be before the WebView has published a page target.
# A single try right after a cold start reports "release APK" on a debug one.
have_probe() {
    local i
    for i in 1 2 3; do
        if JPFM_SERIAL="$SERIAL" JPFM_PKG="$PKG" python3 "$LIBV_HERE/wv-eval.py" '1' \
           >/dev/null 2>&1; then return 0; fi
        sleep 2
    done
    return 1
}

page_json() {   # page_json <expression> -> the JS value, or "" when unreachable
    JPFM_SERIAL="$SERIAL" JPFM_PKG="$PKG" python3 "$LIBV_HERE/wv-eval.py" "$1" 2>/dev/null || true
}

# One boolean out of the page. Prints true/false, or "" when the page cannot be asked.
page_bool() {
    local v; v="$(page_json "!!($1)")"
    case "$v" in true|false) printf '%s' "$v" ;; *) printf '' ;; esac
}

# Wait for the page to be usable: document complete AND markers on the map. Falls back to a
# flat sleep when there is no probe (a release build), which is the honest thing to do —
# nothing else on this box can see inside the WebView.
wait_page() {
    local budget="${1:-40}" waited=0 out
    if ! have_probe; then sleep "${JPFM_BLIND_WAIT:-12}"; return 0; fi
    while [ "$waited" -lt "$budget" ]; do
        out="$(page_json 'document.readyState+"|"+document.querySelectorAll(".leaflet-marker-icon").length')"
        case "$out" in
            # An `if`, not `[ … ] && { … }`: an AND-list whose left side fails IS the exit
            # status of this case branch, and under `set -e` that ends the run — which is
            # exactly the common case here (the document is complete but no marker has been
            # drawn yet, one poll before the page is ready).
            *complete*) if [ "${out##*|}" != '0"' ]; then sleep 1; return 0; fi ;;
        esac
        sleep 2; waited=$(( waited + 2 ))
    done
    sleep 1
}

# Wait until the framework is actually serving. On this box system_server dies now and then
# (the goldfish mapper assertion, and plain memory pressure when several AVDs are up at
# once) and comes back a few seconds later; every adb command in between fails with
# "Can't find service: package", which reads like a product defect and is not one.
device_settle() {
    local i
    for i in $(seq 1 "${1:-20}"); do
        if adbs shell cmd package path android >/dev/null 2>&1; then return 0; fi
        sleep 3
    done
    return 1
}

# Install, with the same patience. Three tries, because losing the race with a restarting
# system_server is common enough here to be worth surviving; a persistent failure is
# reported and the run carries on against whatever is already installed.
install_apk() {
    local try
    for try in 1 2 3; do
        device_settle 20 || true
        if emu install "$APK" 2>&1 | sed 's/^/  /'; then return 0; fi
        info "install attempt $try failed (system_server is probably restarting) — retrying"
        sleep 8
    done
    info "could not install $APK — continuing against whatever is on the device"
    return 0
}

# What is on screen, and is our app alive? Both verify scripts need these, and cold_start
# needs them to tell "the shell misbehaved" from "this emulator image dropped the app".
# `awk NR==1`, not `head -1`: head exits on the first line, the dumpsys writer upstream takes
# SIGPIPE, and with `set -o pipefail` this function returns 141 — which `set -e` turns into an
# aborted run at the next `w="$(top_window)"`. awk drains the pipe. (gate/emulator 2026-09-06)
top_window() { { adbs shell dumpsys window 2>/dev/null || true; } | tr -d '\r' \
               | sed -n 's/.*mCurrentFocus=Window{[^ ]* [^ ]* \([^}]*\)}.*/\1/p' | awk 'NR==1'; }
top_pkg()    { local w; w="$(top_window)"; printf '%s' "${w%%/*}"; }
# top_pkg, but waits out the transition. `mCurrentFocus` reads empty while a window is
# animating in or out, and the back acceptance samples exactly then — a BACK that leaves
# the app used to report "could not read the focused window" as often as it reported the
# launcher. Polls for up to ~8s and prints whatever it settles on, empty included.
top_pkg_settled() {
    local i p
    for i in 1 2 3 4; do
        p="$(top_pkg)"
        [ -n "$p" ] && { printf '%s' "$p"; return 0; }
        sleep 2
    done
    printf ''
}
app_alive()  { [ -n "$(adbs shell pidof "$PKG" 2>/dev/null | tr -d '\r')" ] && echo true || echo false; }

cold_start() {   # cold_start [url] — force-stop first, so this really is a cold start
    adbs shell am force-stop "$PKG" >/dev/null 2>&1 || true
    sleep 1
    local log="$OUT_ROOT/$CUR_AVD/launch.log" try i
    for try in 1 2 3; do
        # `|| true`, deliberately: `am start` on this image returns non-zero now and then
        # while the framework is mid-reconfiguration (right after `wm size`, during the fold
        # proxy, or while system_server is coming back), and under `set -e` + `pipefail` that
        # killed the whole verify run at its first hiccup — which made the three retries
        # below unreachable dead code and reported an environment fault as an aborted
        # acceptance. What decides whether a launch worked is the poll underneath, not the
        # exit code of `am start`.
        if [ -n "${1:-}" ]; then emu launch "$1" | tee -a "$log" || true
        else emu launch | tee -a "$log" || true; fi
        # POLL, do not sample once. `am start -W` returns when the first window is drawn,
        # and on a box this loaded the process can still be spawning, the transition can
        # still be running (mCurrentFocus reads empty mid-transition), and the framework
        # can be restarting under it. A single check three seconds later reports a healthy
        # launch as a dead app, and everything downstream reads as a product defect.
        for i in $(seq 1 10); do
            if [ "$(app_alive)" = true ] && [ "$(top_pkg)" = "$PKG" ]; then
                if [ "$try" -gt 1 ]; then printf 'note: launch %d succeeded\n' "$try" >> "$log"; fi
                return 0
            fi
            sleep 2
        done
        printf 'note: 20s after launch %d the app is alive=%s, front=%s — retrying\n' \
            "$try" "$(app_alive)" "$(top_pkg)" >> "$log"
        adbs wait-for-device >/dev/null 2>&1 || true
        device_settle 10 || true
        adbs shell wm dismiss-keyguard >/dev/null 2>&1 || true
    done
    printf 'note: the app would not stay in the foreground after three launches\n' >> "$log"
}

shot() {   # shot <name> -> $OUT_ROOT/<avd>/<name>.png
    emu shot "$OUT_ROOT/$CUR_AVD/$1.png" >/dev/null
    printf '%s' "$OUT_ROOT/$CUR_AVD/$1.png"
}

# Screen-space tap on a page element, by CSS rect x dpr. A real MotionEvent through the
# whole stack, not a synthetic DOM click — the difference matters for anything that checks
# for a user gesture.
#
# SCROLL FIRST, THEN MEASURE (gate/emulator 2026-09-06). The old version measured whatever
# rect the element happened to have. In the bottom sheet the Tabelog link sits far below the
# fold, so the rect was real (non-zero w/h) but off-screen, `input tap` landed on whatever
# was at that y instead, and the external-link acceptance reported "nothing came to the
# front" about a tap that never hit the link. Now: scrollIntoView, re-measure, and refuse to
# tap a point outside the viewport — a SKIP that says "could not reach it" beats a PASS or a
# FAIL about the wrong pixel.
# THE FIRST *VISIBLE* MATCH, not the first match: the page ships one markup for the phone
# layout and another for the workbench, and querySelector('.wb-filter-btn') hands back the
# hidden one (a 0x0 rect at 0,0) on exactly the wide windows where the visible one exists.
tap_element() {   # tap_element <css selector> -> 0 tapped, 1 not found / not reachable / no probe
    local sel="$1" i r
    i="$(page_json "(function(){var l=document.querySelectorAll(${sel@Q});for(var i=0;i<l.length;i++){var b=l[i].getBoundingClientRect();if(b.width&&b.height){try{l[i].scrollIntoView({block:'center',inline:'center'})}catch(x){l[i].scrollIntoView()}return String(i)}}return ''})()")"
    i="${i%\"}"; i="${i#\"}"
    [ -n "$i" ] || return 1
    sleep 1
    r="$(page_json "(function(){var e=document.querySelectorAll(${sel@Q})[$i];if(!e)return '';var b=e.getBoundingClientRect();if(!b.width||!b.height)return '';var x=b.left+b.width/2;if(x<0||b.top<0||x>innerWidth||b.top>innerHeight)return '';var d=devicePixelRatio;return [Math.round(x*d),Math.round((b.top+b.height/2)*d),Math.round(b.top*d),Math.round(b.bottom*d)].join(' ');})()")"
    r="${r%\"}"; r="${r#\"}"
    [ -n "$r" ] || return 1
    # KEEP THE TAP OUT OF THE STATUS BAR (measured 2026-09-06, gate/emulator). The page draws
    # edge to edge, so an element pinned to the top of the window — the mid/wide 筛选 pill,
    # whose CSS centre lands at y≈63 device px — has its centre inside the system status bar
    # strip, and `input tap` there goes to the system, not the page. The panel then "did not
    # open" on a build where tapping four pixels lower opens it every time. So: push the y
    # down to the first row below the strip that is still inside the element.
    local tx ty top bot safe="${JPFM_TAP_TOP_SAFE:-70}"
    read -r tx ty top bot <<<"$r"
    if [ "$ty" -lt "$safe" ]; then
        if [ "$((bot - 6))" -ge "$safe" ]; then ty="$safe"; else return 1; fi
    fi
    : "$top"
    adbs shell input tap "$tx" "$ty" >/dev/null 2>&1 || return 1
    sleep 1
    return 0
}

# The card-open ladder. Rung 1 is the acceptance itself (STANDARDS §6.2/§6.3); the rest are
# fallbacks so the *layout* frames still get shot on a build whose shell cannot deep-link
# yet. Prints the rung it managed, or "" when the card could not be opened at all.
open_card() {   # open_card <share-id> [cold|hot]
    local id="$1" how="${2:-cold}"
    if [ "$how" = cold ]; then cold_start "$SITE/?r=$id" >/dev/null; else emu launch "$SITE/?r=$id" >/dev/null; fi
    wait_page 40
    if [ "$(page_bool 'document.getElementById("bs-sheet")&&document.getElementById("bs-sheet").classList.contains("bs-open")||/\bwb-detail-open\b/.test(document.body.className)')" = "true" ]; then
        printf 'deeplink'; return 0
    fi
    # Rung 2: the page's own hot-path hook, once the app-bridge block is deployed.
    if [ "$(page_bool 'typeof window.__jpfmOpenShare==="function"')" = "true" ]; then
        page_json "(function(){try{return !!window.__jpfmOpenShare(${id@Q})}catch(e){return false}})()" >/dev/null
        sleep 2
        if [ "$(page_bool 'document.getElementById("bs-sheet").classList.contains("bs-open")||/\bwb-detail-open\b/.test(document.body.className)')" = "true" ]; then
            printf 'hook'; return 0
        fi
    fi
    # Rung 3: tap a restaurant marker if one happens to be on screen (never a cluster, never
    # a landmark pin — those do something else).
    if tap_element '.leaflet-marker-icon:not(.marker-cluster):not(.bm-mk)'; then
        sleep 2
        if [ "$(page_bool 'document.getElementById("bs-sheet").classList.contains("bs-open")||/\bwb-detail-open\b/.test(document.body.className)')" = "true" ]; then
            printf 'marker-tap'; return 0
        fi
    fi
    printf ''
    return 1
}

# ------------------------------------------------------------------------- the back stack
# THE PROBE-FREE WITNESS (2.2.0, android-back). Everything else in this file that can see
# whether an overlay is open goes through the DevTools endpoint, which a release APK does
# not publish — so the back acceptance used to SKIP on exactly the build that ships, and
# INTEGRATE-1 §6 could report "one BACK leaves the app" for weeks without the gate noticing.
# The shell's own diagnostics line carries the back stack since 2.2.0 (`back.index` /
# `back.enabled` / `back.canGoBack` / `back.size`), and that line exists on a release build.
#
# Prints "<index> <enabled>" — e.g. "1 true" — or nothing at all when the shell under test
# does not report the block (a pre-2.2.0 APK), which every caller treats as SKIP.
#
# READ THE FOCUSED WINDOW BEFORE CALLING THIS, never after: `diag.sh capture` asks for the
# line with `am start`, which brings the app back to the front and would erase the very
# thing "did BACK leave the app" is trying to measure.
#
# AND IT MUST NOT TRUST THE RUN-WIDE SOURCE DETECTION (review-1, 2026-09-07). Both verify
# scripts pick native-vs-probe once, seconds after `install_apk`, with an 8s budget and
# before the app has ever been READY — a cold start on this image regularly takes longer
# than that, so a perfectly good 2.2.0 APK is labelled "probe" for the whole run. The probe
# half is the PAGE's view (diag.sh probe_js) and has no `back` block at all, with or without
# DevTools, so every reading here came back empty and the whole segment fell through to the
# pre-2.2.0 SKIP — which is exactly the hole INTEGRATE-1 §6 went unseen through, reopened by
# a different door. So: an empty reading is retried ONCE with the native source forced, and
# only a build that stays silent under `native` is reported as carrying no `back` block.
#
# The latch that remembers the answer is a FILE, not a variable: every caller reads
# back_state through `$(...)`, and a variable set inside a command substitution dies with
# its subshell — which is how the first cut of this fix silently retried on every single
# call and never got its wording branch. `back_source` is the accessor; the values are
#   ""        not decided yet
#   native    the run-wide source cannot see the block; force `native` from here on
#   none      `native` answered and this build really has no `back` block (pre-2.2.0)
back_source_file() { printf '%s' "$OUT_ROOT/$CUR_AVD/back-source.txt"; }
back_source()      { cat "$(back_source_file)" 2>/dev/null || true; }
back_source_reset() { rm -f "$(back_source_file)" 2>/dev/null || true; }
back_state() {   # back_state <name-for-the-json>
    local f="$OUT_ROOT/$CUR_AVD/back-$1.json" i e src
    src="$(back_source)"
    # rm first, same reason as the deep-link flow: diag.sh leaves the previous file behind
    # when it cannot read a line, and a stale reading is worse than no reading. `|| true`
    # because OUT_ROOT can live on a Windows-backed mount, where a file the previous capture
    # has only just closed answers EPERM for a moment.
    rm -f "$f" 2>/dev/null || true
    if [ "$src" = native ]; then
        ( export JPFM_DIAG_SOURCE=native; diagsh capture "$f" ) >/dev/null 2>&1 || true
    else
        diagsh capture "$f" >/dev/null 2>&1 || true
    fi
    i="$(diagsh get "$f" back.index 2>/dev/null || true)"
    e="$(diagsh get "$f" back.enabled 2>/dev/null || true)"
    if [ -z "$i" ] && [ -z "$src" ]; then
        rm -f "$f" 2>/dev/null || true
        # A subshell, not a `VAR=x func` prefix: an assignment in front of a shell FUNCTION
        # can survive the call, and leaking JPFM_DIAG_SOURCE into the rest of the run would
        # silently re-source every other capture in the script.
        ( export JPFM_DIAG_SOURCE=native; diagsh capture "$f" ) >/dev/null 2>&1 || true
        i="$(diagsh get "$f" back.index 2>/dev/null || true)"
        e="$(diagsh get "$f" back.enabled 2>/dev/null || true)"
        if [ -n "$i" ]; then
            printf 'native' > "$(back_source_file)" 2>/dev/null || true
        elif [ -s "$f" ]; then
            # native answered with a JSON line, and there is no `back` in it.
            printf 'none' > "$(back_source_file)" 2>/dev/null || true
        fi
    fi
    [ -n "$i" ] || return 0
    printf '%s %s' "$i" "${e:-?}"
}

# Open ONE overlay with a real touch and no help from the page. The map's FAB stack is
# position:fixed to the bottom-right corner of the window in every layout (map.py
# `.map-fab-stack`: bottom 18px + safe-area, right 14px + safe-area), and the 图层 circle is
# its last child — so it can be aimed at from `wm size` and `wm density` alone. Two rungs,
# because the safe-area inset the emulator's navigation bar contributes moves the circle up
# by exactly its height: 64 dp above the bottom edge with a bar, 40 dp without.
#
# The locate circle above it is deliberately NOT in the ladder: tapping it raises the system
# location dialog, which would sit on top of the app and make every later reading a lie.
#
# Never asserts and never fails — the caller decides from the shell's own back index whether
# anything actually opened. On a debug build the caller has tap_element and should prefer it.
tap_map_fab() {   # tap_map_fab <the back index before the tap>
    local base="${1:-0}" size dens w h d dy x y st
    size="$({ adbs shell wm size 2>/dev/null || true; } | tr -d '\r' \
            | sed -n 's/.*: *\([0-9]\{2,\}\)x\([0-9]\{2,\}\).*/\1 \2/p' | tail -1)"
    dens="$({ adbs shell wm density 2>/dev/null || true; } | tr -d '\r' \
            | sed -n 's/.*: *\([0-9]\{2,\}\).*/\1/p' | tail -1)"
    [ -n "$size" ] && [ -n "$dens" ] || return 0
    read -r w h <<<"$size"
    d="$dens"
    for dy in 64 40; do
        x=$(( w - 36 * d / 160 ))
        y=$(( h - dy * d / 160 ))
        adbs shell input tap "$x" "$y" >/dev/null 2>&1 || true
        sleep 2
        info "tapped the map FAB corner at ${x},${y} (${dy}dp above the bottom edge)"
        # One rung is enough whenever it worked; the caller re-reads the index either way,
        # so a second tap on an already-open popover is the only cost of guessing wrong.
        st="$(back_state fabtry)"
        [ -n "$st" ] || return 0            # no witness: nothing to iterate on
        [ "${st%% *}" != "$base" ] && return 0
    done
}

# The page's first-visit language chooser (#lang-gate) is a modal with a scrim, and on a
# freshly installed APK it is up on the first load of every acceptance. It eats every tap
# aimed at the page underneath while leaving those elements a perfectly real rect, so
# tap_element "succeeds" and the thing it aimed at never opens — measured 2026-09-07, and it
# is what made `verify-geometry`'s filter frame fail on foldcover and fold8inner60 against
# BOTH the pending build and the live one (audit_outputs/2.2.0-fix/impl/android-back/).
#
# One blind tap at the middle of the window dismisses it: that is where the chooser's first
# button (简体中文, the page's own default) sits. On any later run the same point is map,
# where a tap opens a card at worst — so callers that care re-read their own state after.
# Probe-free on purpose: this has to work on the release APK too.
dismiss_first_run() {
    local wsz ww wh
    wsz="$({ adbs shell wm size 2>/dev/null || true; } | tr -d '\r' \
           | sed -n 's/.*: *\([0-9]\{2,\}\)x\([0-9]\{2,\}\).*/\1 \2/p' | tail -1)"
    [ -n "$wsz" ] || return 0
    read -r ww wh <<<"$wsz"
    adbs shell input tap $(( ww / 2 )) $(( wh * 42 / 100 )) >/dev/null 2>&1 || true
    sleep 2
}

card_open() { page_bool 'document.getElementById("bs-sheet")&&document.getElementById("bs-sheet").classList.contains("bs-open")||/\bwb-detail-open\b/.test(document.body.className)'; }
filter_open() { page_bool 'document.getElementById("ff-sheet")&&document.getElementById("ff-sheet").classList.contains("ff-open")||(document.getElementById("wb-filter-pop")||{classList:{contains:function(){return false}}}).classList.contains("open")'; }

# The map is repainted and not a flat grey slab. --crop skips the top and bottom eighths so
# the page's own chrome (search box, FAB stack, offline bar) cannot carry the check.
assert_map_painted() {   # assert_map_painted <png> <label>
    local png="$1" label="$2" out rc=0
    out="$(python3 "$LIBV_HERE/png-stats.py" "$png" --crop 0,0.12,1,0.88 --assert-map)" || rc=$?
    local dom dist grey
    dom="$(printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["dominant"], d["dominant_share"])' 2>/dev/null || true)"
    dist="$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["distinct"])' 2>/dev/null || true)"
    grey="$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["leaflet_grey_share"])' 2>/dev/null || true)"
    if [ "$rc" -eq 0 ]; then
        pass "$label — map painted (distinct $dist, dominant $dom, #ddd $grey)"
    elif [ "$rc" -eq 1 ]; then
        fail "$label — flat region, looks unpainted (distinct $dist, dominant $dom, #ddd $grey)"
    else
        skip "$label — could not read the frame ($out)"
    fi
    printf '%s\n' "$out" >> "$OUT_ROOT/$CUR_AVD/png-stats.log"
}

finish_avd() {
    _say ""
    _say "----------------------------------------------------------------"
    _say "PASS $PASS_N   FAIL $FAIL_N   SKIP $SKIP_N"
    printf '\n%s: PASS %d  FAIL %d  SKIP %d  -> %s\n' "$CUR_AVD" "$PASS_N" "$FAIL_N" "$SKIP_N" "$SUMMARY"
}
