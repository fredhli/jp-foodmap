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
