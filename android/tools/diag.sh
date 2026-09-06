#!/usr/bin/env bash
#
# One diagnostics reading of the running app, and the assertions over it.
#
#     ./diag.sh capture out.json        # native line if the build has one, else the probe
#     ./diag.sh probe   out.json        # force the DevTools probe (debug builds)
#     ./diag.sh native  out.json        # force the JpfmDiag logcat line (exit 3 if absent)
#     ./diag.sh get out.json innerWidth # one value, from anywhere in the JSON tree
#     ./diag.sh page 'document.title'   # evaluate one expression in the page
#     ./diag.sh check out.json innerWidth=475 dpr=2.625 imeMode=WEBVIEW
#
# TWO SOURCES, ONE SHAPE. STANDARDS §12.2 has the app print its own diagnostics JSON to
# logcat (tag JpfmDiag) on `am start --ez diagnostics_log true`; that is the source of truth
# and the only one that exists for a release APK. It does not exist in every build — the T1
# skeleton has no Diagnostics at all — and the geometry acceptance has to be runnable
# anyway, so the fallback is to ask the page directly over the WebView's DevTools endpoint
# (tools/wv-eval.py; debug builds only) and to fill the shell half from adb. Both paths
# write the same key names (docs/PLAN.md §5.8), so `check` does not care which one ran;
# `source` in the JSON says which one it was, and keys the fallback cannot know are simply
# absent, which `check` reports as SKIP rather than FAIL.
#
# WHAT THE PROBE ADDS on top of §5.8, and why:
#   bootId        the page's own window.__jpfmBootId when the app-bridge block is deployed,
#                 else a marker this probe plants on first contact. Either way an unchanged
#                 value across a geometry change proves the document was not reloaded, which
#                 is the whole of STANDARDS §3.1.
#   pid           the app process. Unchanged pid + unchanged bootId is the fallback proof
#                 that the activity was not recreated when activityCreates is unavailable.
#   cardOpen      #bs-sheet.bs-open, or body.wb-detail-open in the two column layouts —
#                 the same test the page itself uses. STANDARDS §3.2.
#   filterOpen    #ff-sheet.ff-open or #wb-filter-pop.open. STANDARDS §2.2.
#   offlineBar    #net-offline is present and not [hidden]. STANDARDS §9.1.
#   wbMode        which workbench layout the page picked for this width (phone/split/mid/wide).
#   mapView       localStorage tabelog.mapView, READ ONLY — the map object itself is a local
#                 inside initMap and unreachable, and this is the only way to see where the
#                 map is looking (the geo-fix acceptance, §8.2).
# Nothing here writes to localStorage, and nothing here touches the network.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
AVD_NAME="${JPFM_AVD:-foldcover}"
EMU_PORT="${JPFM_EMU_PORT:-5554}"
SERIAL="emulator-${EMU_PORT}"
# shellcheck source=/dev/null
source "$HOME/tools/android-env.sh" >/dev/null 2>&1 || true

# Same rule as emu.sh's pkg(): JPFM_PKG wins (lib-verify.sh reads it off the APK under
# test with aapt2), otherwise take whichever of the two ids is actually installed, debug
# first. Defaulting to `.debug` against a release install used to look exactly like a build
# with no diagnostics at all — see the comment on emu.sh's pkg(). After the toolchain is
# sourced, because that is what puts adb on PATH.
PKG="${JPFM_PKG:-}"
if [ -z "$PKG" ]; then
    for _id in com.fredhli.jpfoodmap.debug com.fredhli.jpfoodmap; do
        if adb -s "$SERIAL" shell pm path "$_id" >/dev/null 2>&1; then PKG="$_id"; break; fi
    done
    PKG="${PKG:-com.fredhli.jpfoodmap}"
    unset _id
fi

die() { printf 'diag.sh: %s\n' "$*" >&2; exit 2; }

# The page half. Kept as one expression so a single Runtime.evaluate answers everything and
# no two fields can come from different moments.
probe_js() {
    cat <<'JS'
(function () {
  function num(v) { return (typeof v === 'number' && isFinite(v)) ? v : null; }
  // env(safe-area-inset-*) is not readable directly; a throwaway element that takes it as
  // padding is, and getComputedStyle resolves it to px.
  var envv = {t: null, b: null, l: null, r: null};
  try {
    var p = document.createElement('div');
    p.style.cssText = 'position:fixed;left:-9999px;top:-9999px;' +
      'padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom);' +
      'padding-left:env(safe-area-inset-left);padding-right:env(safe-area-inset-right);';
    document.documentElement.appendChild(p);
    var cs = getComputedStyle(p);
    envv = {t: parseFloat(cs.paddingTop) || 0, b: parseFloat(cs.paddingBottom) || 0,
            l: parseFloat(cs.paddingLeft) || 0, r: parseFloat(cs.paddingRight) || 0};
    p.parentNode.removeChild(p);
  } catch (e) {}
  if (!window.__jpfmProbeBoot) {
    window.__jpfmProbeBoot = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  }
  var bs = document.getElementById('bs-sheet');
  var ff = document.getElementById('ff-sheet');
  var fp = document.getElementById('wb-filter-pop');
  var off = document.getElementById('net-offline');
  var cls = document.body ? document.body.className : '';
  var mode = /\bwb-wide\b/.test(cls) ? 'wide' : /\bwb-mid\b/.test(cls) ? 'mid'
           : /\bwb-split\b/.test(cls) ? 'split' : 'phone';
  var mv = null, lang = null;
  try { mv = localStorage.getItem('tabelog.mapView'); } catch (e) {}
  try { lang = localStorage.getItem('tabelog.lang'); } catch (e) {}
  var vv = window.visualViewport;
  return JSON.stringify({
    source: 'probe',
    innerWidth: num(window.innerWidth), innerHeight: num(window.innerHeight),
    outerWidth: num(window.outerWidth), outerHeight: num(window.outerHeight),
    dpr: num(window.devicePixelRatio),
    screen: {w: screen.width, h: screen.height,
             aw: num(screen.availWidth), ah: num(screen.availHeight)},
    vv: vv ? {w: Math.round(vv.width), h: Math.round(vv.height),
              ot: Math.round(vv.offsetTop), s: vv.scale} : null,
    env: envv,
    bootId: window.__jpfmBootId || window.__jpfmProbeBoot,
    bootIdSource: window.__jpfmBootId ? 'page' : 'probe',
    lang: lang || (document.documentElement.getAttribute('lang') || ''),
    url: String(location.href).split('?')[0].split('#')[0],
    ua: navigator.userAgent,
    native: !!window.Native,
    nativeApp: (window.Native && window.Native.app) || null,
    openShareHook: typeof window.__jpfmOpenShare === 'function',
    sw: !!(navigator.serviceWorker && navigator.serviceWorker.controller),
    online: !!navigator.onLine,
    readyState: document.readyState,
    markers: (function () { try { return document.querySelectorAll('.leaflet-marker-icon').length; } catch (e) { return null; } })(),
    cardOpen: !!(bs && bs.classList.contains('bs-open')) || /\bwb-detail-open\b/.test(cls),
    filterOpen: !!(ff && ff.classList.contains('ff-open')) || !!(fp && fp.classList.contains('open')),
    offlineBar: !!(off && !off.hidden),
    wbMode: mode,
    mapView: mv
  });
})()
JS
}

# The shell half the probe can reach without the app's help: the display config the
# framework reports, the system font scale, and the process id.
shell_half() {
    local displays geom fs pid
    displays="$(adb -s "$SERIAL" shell dumpsys window displays 2>/dev/null | tr -d '\r' || true)"
    geom="$(printf '%s\n' "$displays" | grep -m1 -oE 'sw[0-9]+dp w[0-9]+dp h[0-9]+dp [0-9]+dpi' || true)"
    fs="$(adb -s "$SERIAL" shell settings get system font_scale 2>/dev/null | tr -d '\r' || true)"
    pid="$(adb -s "$SERIAL" shell pidof "$PKG" 2>/dev/null | tr -d '\r' | awk '{print $1}' || true)"
    python3 - "$geom" "$fs" "$pid" "$AVD_NAME" "$SERIAL" <<'PY'
import json, sys, re
geom, fs, pid, avd, serial = sys.argv[1:6]
out = {"avd": avd, "serial": serial}
m = re.match(r"sw(\d+)dp w(\d+)dp h(\d+)dp (\d+)dpi", geom or "")
if m:
    out["swDp"] = int(m.group(1)); out["widthDp"] = int(m.group(2))
    out["heightDp"] = int(m.group(3)); out["dpi"] = int(m.group(4))
    out["density"] = round(int(m.group(4)) / 160.0, 4)
try:
    out["fontScale"] = float(fs)
except (TypeError, ValueError):
    out["fontScale"] = 1.0
if pid:
    out["pid"] = int(pid)
print(json.dumps(out))
PY
}

cmd_probe() {
    local out="${1:-}"
    [ -n "$out" ] || die "usage: $0 probe <out.json>"
    local js; js="$(mktemp)"
    probe_js > "$js"
    local page rc=0
    page="$(JPFM_SERIAL="$SERIAL" JPFM_PKG="$PKG" python3 "$HERE/wv-eval.py" --file "$js" 2>/tmp/jpfm-wv-eval.err)" || rc=$?
    rm -f "$js"
    if [ "$rc" -ne 0 ]; then
        sed 's/^/  /' /tmp/jpfm-wv-eval.err >&2 || true
        exit "$rc"     # 3 = no devtools (SKIP), 1 = the page threw (FAIL)
    fi
    local shell_json; shell_json="$(shell_half)"
    python3 - "$out" "$page" "$shell_json" <<'PY'
import json, sys
out, page, shell = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.loads(json.loads(page))   # wv-eval prints the JS value, which is itself a JSON string
d.update(json.loads(shell))
with open(out, "w", encoding="utf-8") as fh:
    json.dump(d, fh, ensure_ascii=False, indent=1, sort_keys=True)
PY
    printf '  diag: wrote %s (probe)\n' "$out"
}

cmd_native() {
    local out="${1:-}"
    [ -n "$out" ] || die "usage: $0 native <out.json>"
    JPFM_AVD="$AVD_NAME" JPFM_EMU_PORT="$EMU_PORT" JPFM_PKG="$PKG" "$HERE/emu.sh" diag "$out"
}

# The one callers should use: the app's own line when it has one, the probe when it does not.
#
# JPFM_DIAG_SOURCE=native|probe skips the detection. A verify script captures once, reads
# the `source` key out of the answer and exports it for the rest of the run — otherwise
# every single capture against a build with no diagnostics pays the full logcat wait, and
# a geometry run takes twenty captures.
cmd_capture() {
    local out="${1:-}"
    [ -n "$out" ] || die "usage: $0 capture <out.json>"
    case "${JPFM_DIAG_SOURCE:-auto}" in
        probe)  cmd_probe "$out"; return ;;
        native) cmd_native "$out"; return ;;
    esac
    local rc=0
    JPFM_DIAG_TIMEOUT="${JPFM_DIAG_TIMEOUT:-12}" cmd_native "$out" 2>/dev/null || rc=$?
    if [ "$rc" -eq 0 ]; then
        # Tag the file so a summary can say where its numbers came from, without assuming
        # the app wrote a "source" key of its own.
        python3 - "$out" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p, encoding="utf-8"))
if isinstance(d, dict):
    d.setdefault("source", "native")
    json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1, sort_keys=True)
PY
        printf '  diag: wrote %s (native JpfmDiag line)\n' "$out"
        # The native line is the app's view of itself; the page-state keys the acceptance
        # needs (cardOpen / filterOpen / offlineBar) are not in §5.8, so merge the probe in
        # when it is available. Never fatal: a release build simply has no probe.
        local extra; extra="$(mktemp)"
        if cmd_probe "$extra" >/dev/null 2>&1; then
            python3 - "$out" "$extra" <<'PY'
import json, sys
base = json.load(open(sys.argv[1], encoding="utf-8"))
prb = json.load(open(sys.argv[2], encoding="utf-8"))
if isinstance(base, dict):
    for k in ("cardOpen", "filterOpen", "offlineBar", "wbMode", "mapView", "markers",
              "openShareHook", "readyState"):
        if k in prb and k not in base:
            base[k] = prb[k]
    base["probeMerged"] = True
    json.dump(base, open(sys.argv[1], "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, sort_keys=True)
PY
        fi
        rm -f "$extra"
        return 0
    fi
    if [ "$rc" -eq 1 ]; then
        die "the app printed a JpfmDiag line and it was not valid JSON — that is a defect"
    fi
    cmd_probe "$out"
}

cmd_page() {
    local expr="${1:-}"
    [ -n "$expr" ] || die "usage: $0 page '<expression>'"
    JPFM_SERIAL="$SERIAL" JPFM_PKG="$PKG" python3 "$HERE/wv-eval.py" "$expr"
}

cmd_get() {
    local file="${1:-}" key="${2:-}"
    [ -n "$file" ] && [ -n "$key" ] || die "usage: $0 get <file.json> <key>"
    python3 "$HERE/diagjson.py" get "$file" "$key"
}

cmd_check() {
    local file="${1:-}"; shift || true
    [ -n "$file" ] || die "usage: $0 check <file.json> key=value ..."
    python3 "$HERE/diagjson.py" check "$file" "$@"
}

case "${1:-}" in
    capture) shift; cmd_capture "$@" ;;
    probe)   shift; cmd_probe "$@" ;;
    native)  shift; cmd_native "$@" ;;
    get)     shift; cmd_get "$@" ;;
    page)    shift; cmd_page "$@" ;;
    check)   shift; cmd_check "$@" ;;
    *) die "usage: $0 {capture|probe|native} <out.json> | get <f> <key> | page <expr> | check <f> k=v ..." ;;
esac
