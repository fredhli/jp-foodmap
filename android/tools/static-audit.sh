#!/usr/bin/env bash
#
# Read the shell's SOURCES and assert the things STANDARDS.md states in prose.
#
#     android/tools/static-audit.sh              # audit the working tree
#     android/tools/static-audit.sh --verbose    # print every match, not just failures
#
# No device, no build, no network — it is grep over android/app/src plus the manifest, so it
# runs in a second and can be run after every edit. `tools/power-audit.sh` is the other half:
# it needs a built APK and a running emulator and checks what actually shipped.
#
# WHY A SCRIPT AND NOT A CHECKLIST. Every rule below is one a future edit could undo without
# looking wrong in review: a `addJavascriptInterface` added while debugging the bridge, a
# `cleartextTrafficPermitted="true"` added to reach a laptop's dev server, a WorkManager
# dependency dragged in by a "just prefetch the tiles" idea, a URL that ends up in a Toast.
# STANDARDS §0.4, §8.3, §10.1, §10.3–10.6, §13.
#
# TWO KINDS OF CHECK.
#   ABSENCE checks ("this must not appear") always run. They are the hardening invariants
#     and none of them depends on a task being finished.
#   WIRING checks ("this must appear") are skipped, with a printed reason, while the file
#     that owns them is still one of T1's compilable stubs — a stub is detected by the
#     literal TODO("Tn: …") it carries, so a check un-skips itself the moment the real code
#     lands and cannot be forgotten. Skips are counted and printed; they are not passes.
#
# Exit codes: 0 all enforced checks pass · 1 at least one failed · 2 the tree is not there.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SRC="$(cd "$HERE/.." && pwd -P)"
APP="$SRC/app"
MAIN="$APP/src/main"
KT="$MAIN/kotlin/com/fredhli/jpfoodmap"
MANIFEST="$MAIN/AndroidManifest.xml"
NSC="$MAIN/res/xml/network_security_config.xml"
VERBOSE=""
[ "${1:-}" = "--verbose" ] && VERBOSE=1

[ -d "$KT" ] || { printf 'static-audit: no sources at %s\n' "$KT" >&2; exit 2; }

pass_n=0; fail_n=0; skip_n=0
ok()   { pass_n=$((pass_n + 1)); printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
bad()  { fail_n=$((fail_n + 1)); printf '  \033[31mFAIL\033[0m  %s\n' "$1"
         [ -n "${2:-}" ] && printf '%s\n' "$2" | sed 's/^/          /'; }
skip() { skip_n=$((skip_n + 1)); printf '  \033[33mSKIP\033[0m  %s\n' "$1"
         printf '          %s\n' "${2:-}"; }
section() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# Is a source file still one of T1's compilable stubs? Two markers, because T1 wrote two
# kinds: a function body that is a literal TODO("Tn: key"), and — where an empty body still
# compiles, as in an Activity — a header line saying "T1 STUB". Both disappear when the
# owning task lands, which is what makes the skip self-clearing.
is_stub() { [ -f "$1" ] && grep -qE 'TODO\("T[0-9]|T1 STUB' "$1"; }

# absent <label> <pattern> [path...]  — an extended-regex that must match nothing.
absent() {
    local label="$1" pat="$2"; shift 2
    local hits
    hits="$(grep -rnE "$pat" "$@" 2>/dev/null)"
    if [ -z "$hits" ]; then ok "$label"; else bad "$label" "$hits"; fi
}

# present <label> <pattern> <path...> — must match at least once.
present() {
    local label="$1" pat="$2"; shift 2
    local hits
    hits="$(grep -rnE "$pat" "$@" 2>/dev/null)"
    if [ -n "$hits" ]; then
        ok "$label"; [ -n "$VERBOSE" ] && printf '%s\n' "$hits" | sed 's/^/          /'
    else
        bad "$label" "no match for /$pat/ in $*"
    fi
}

printf '\033[1mstatic-audit\033[0m  %s\n' "$SRC"

# ============================================================ 1. the bridge and the WebView
section '1. bridge / WebView surface (STANDARDS §10.4, §10.6)'

# The ONLY bridge is addWebMessageListener, origin-scoped. addJavascriptInterface hands a
# Java object to every frame the WebView loads; for an app whose entire content is remote
# that is the difference between four named calls and arbitrary reflection.
# The pattern is the CALL (a leading dot), not the word: Bridge.kt's header explains in
# prose why it is not used, and a check that fails on its own documentation is a check
# people learn to ignore.
absent 'no addJavascriptInterface call anywhere' \
    '\.addJavascriptInterface\s*\(' "$APP/src"

# Contents debugging must follow BuildConfig.DEBUG, never a literal.
absent 'no setWebContentsDebuggingEnabled(true) literal' \
    'setWebContentsDebuggingEnabled\(\s*true\s*\)' "$APP/src"
present 'contents debugging is tied to BuildConfig.DEBUG' \
    'setWebContentsDebuggingEnabled\(BuildConfig\.DEBUG\)' "$KT"

# The four settings that are off because the page is remote. They are set in whichever file
# currently builds the WebView; SiteWebView takes over from MainActivity when T2 lands.
present 'allowFileAccess = false'                    'allowFileAccess\s*=\s*false' "$KT"
present 'allowContentAccess = false'                 'allowContentAccess\s*=\s*false' "$KT"
present 'javaScriptCanOpenWindowsAutomatically=false' 'javaScriptCanOpenWindowsAutomatically\s*=\s*false' "$KT"
present 'mediaPlaybackRequiresUserGesture = true'    'mediaPlaybackRequiresUserGesture\s*=\s*true' "$KT"
absent  'nothing sets allowFileAccess/allowContentAccess to true' \
    '(allowFileAccess|allowContentAccess|allowFileAccessFromFileURLs|allowUniversalAccessFromFileURLs)\s*=\s*true' "$APP/src"

if is_stub "$KT/SiteWebView.kt"; then
    skip 'mixedContentMode = MIXED_CONTENT_NEVER_ALLOW' \
        'SiteWebView.kt is still a T1 stub — this check belongs to T2 (shell) and turns on with it.'
else
    present 'mixedContentMode = MIXED_CONTENT_NEVER_ALLOW' \
        'mixedContentMode\s*=\s*WebSettings\.MIXED_CONTENT_NEVER_ALLOW' "$KT"
fi

if is_stub "$KT/Bridge.kt"; then
    skip 'bridge origin allow-list is exactly https://jpfoodmap.com' \
        'Bridge.kt is still a T1 stub — this check belongs to T3 (signin-bridge).'
else
    present 'bridge uses addWebMessageListener' 'addWebMessageListener' "$KT"
    # Every https origin literal that reaches an allowed-origin set must be ours. The gsi
    # script URL is allowed to appear as a URL (T3 intercepts it) but never as an origin.
    origins="$(grep -rnE 'allowedOriginRules|setOf\("https://' "$KT" 2>/dev/null \
        | grep -vE 'https://jpfoodmap\.com' || true)"
    if [ -z "$origins" ]; then ok 'bridge origin allow-list is exactly https://jpfoodmap.com'
    else bad 'bridge origin allow-list is exactly https://jpfoodmap.com' "$origins"; fi
fi

# ============================================================ 2. network
section '2. network (STANDARDS §10.5, §13)'

present 'network_security_config: cleartext refused' \
    'cleartextTrafficPermitted="false"' "$NSC"
absent  'network_security_config: no cleartext exception anywhere' \
    'cleartextTrafficPermitted="true"' "$MAIN/res"
absent  'network_security_config: no custom trust anchors' \
    '<(trust-anchors|certificates)' "$NSC"
absent  'manifest: no usesCleartextTraffic="true"' \
    'usesCleartextTraffic="true"' "$MANIFEST"
present 'manifest points at the network security config' \
    'networkSecurityConfig="@xml/network_security_config"' "$MANIFEST"

# ============================================================ 3. power
section '3. power / background (STANDARDS §10.1, §10.2)'

# Nothing may schedule work, hold the CPU awake, or run outside the foreground.
absent 'no WorkManager / JobScheduler / AlarmManager / wakelock / FCM in the sources' \
    '(androidx\.work|WorkManager|JobScheduler|JobInfo|AlarmManager|setExactAndAllowWhileIdle|PowerManager\.WakeLock|newWakeLock|FirebaseMessaging|com\.google\.firebase)' \
    "$APP/src"
absent 'no work/firebase dependency in app/build.gradle.kts' \
    '(androidx\.work|firebase|play-services-(gcm|measurement))' "$APP/build.gradle.kts"
absent 'manifest declares no <service>' '<service' "$MANIFEST"
absent 'manifest declares no <receiver>' '<receiver' "$MANIFEST"
absent 'manifest declares no <provider>' '<provider' "$MANIFEST"

# The backgrounded WebView must actually stop: its timers, its animations and Leaflet's own
# rAF loop are battery with no screen. The cookie flush in the same place is what makes a
# 90-day session survive a process kill.
onpause="$(awk '/override fun onPause/,/^    }/' "$KT/MainActivity.kt")"
missing=""
printf '%s' "$onpause" | grep -q 'onPause()'                || missing="$missing webView.onPause()"
printf '%s' "$onpause" | grep -q 'pauseTimers()'            || missing="$missing pauseTimers()"
printf '%s' "$onpause" | grep -q 'flush()'                  || missing="$missing CookieManager.flush()"
if [ -z "$missing" ]; then ok 'onPause: onPause() + pauseTimers() + flush()'
else bad 'onPause: onPause() + pauseTimers() + flush()' "missing:$missing"; fi

# The page's locate control is a one-shot getCurrentPosition with maximumAge; a watch would
# hold the GPS on for as long as the app is open (STANDARDS §8.3).
absent 'the shell starts no location watch' \
    '(watchPosition|requestLocationUpdates|LocationListener)' "$APP/src"

# ============================================================ 4. permissions
section '4. permissions (STANDARDS §10.3)'

declared="$(grep -oE 'android\.permission\.[A-Z_]+' "$MANIFEST" | sort -u)"
# FIVE since 2026-09-06. ACCESS_NETWORK_STATE is the odd one out: no line of our own Kotlin
# touches ConnectivityManager. It is there for Chromium — NetworkChangeNotifierAutoDetect
# only registers a connectivity callback when the host app holds it, and without the
# callback navigator.onLine inside the WebView never leaves true, so the site's own
# #net-offline bar cannot appear (STANDARDS §9.1, proven both ways on the emulator).
# "normal" protection level: install-time grant, no prompt, not listed on the system
# permission page, reads only whether a network exists and of what kind.
expected='android.permission.ACCESS_COARSE_LOCATION
android.permission.ACCESS_FINE_LOCATION
android.permission.ACCESS_NETWORK_STATE
android.permission.INTERNET
android.permission.POST_NOTIFICATIONS'
if [ "$declared" = "$expected" ]; then
    ok 'manifest declares exactly the five agreed permissions'
else
    bad 'manifest declares exactly the five agreed permissions' \
        "$(diff <(printf '%s\n' "$expected") <(printf '%s\n' "$declared") | sed 's/^/  /')"
fi
absent 'none of the forbidden permissions is declared' \
    '(READ_EXTERNAL_STORAGE|WRITE_EXTERNAL_STORAGE|MANAGE_EXTERNAL_STORAGE|CAMERA|RECORD_AUDIO|READ_CONTACTS|READ_PHONE_STATE|QUERY_ALL_PACKAGES|REQUEST_INSTALL_PACKAGES|SYSTEM_ALERT_WINDOW|WAKE_LOCK|RECEIVE_BOOT_COMPLETED|FOREGROUND_SERVICE|ACCESS_BACKGROUND_LOCATION)' \
    "$MANIFEST"
present 'backup and device transfer are off' 'allowBackup="false"' "$MANIFEST"
present 'dataExtractionRules is wired up' 'dataExtractionRules="@xml/data_extraction_rules"' "$MANIFEST"

# ============================================================ 5. notifications
section '5. notifications (STANDARDS §11)'

present 'one channel id, and it is the agreed one' \
    'CHANNEL_ID = "jpfoodmap_general"' "$KT/Notifications.kt"
present 'the test entry is silent'   'setSilent\(true\)' "$KT/Notifications.kt"
present 'the test entry has a fixed id' 'TEST_NOTIFICATION_ID = [0-9]+' "$KT/Notifications.kt"
present 'PendingIntent is immutable and updating' \
    'FLAG_IMMUTABLE or PendingIntent\.FLAG_UPDATE_CURRENT' "$KT/Notifications.kt"
absent  'the notification carries no URL' \
    'https?://' "$KT/Notifications.kt"
# Exactly one place may ask for the grant, and it is the settings screen (never at launch).
# Both spellings of the permission name count as an ask: the literal
# Manifest.permission.POST_NOTIFICATIONS and Notifications.PERMISSION, the constant
# Notifications.kt exposes precisely so a caller need not import Manifest and guard it by
# API level itself. Matching only the literal would let a second asker hide behind the
# constant, which is the opposite of what this check is for.
askers="$(grep -rlE 'requestPermissions\(.*(POST_NOTIFICATIONS|Notifications\.PERMISSION)|POST_NOTIFICATIONS.*requestPermissions|registerForActivityResult.*(POST_NOTIFICATIONS|Notifications\.PERMISSION)' "$KT" 2>/dev/null || true)"
if is_stub "$KT/AppSettingsActivity.kt"; then
    skip 'POST_NOTIFICATIONS is requested only from the settings screen' \
        'AppSettingsActivity.kt is still a T1 stub — this check belongs to T5 (links-settings-diag).'
elif [ "$(printf '%s' "$askers" | grep -c .)" = "1" ] && \
     printf '%s' "$askers" | grep -q 'AppSettingsActivity.kt'; then
    ok 'POST_NOTIFICATIONS is requested only from the settings screen'
else
    bad 'POST_NOTIFICATIONS is requested only from the settings screen' \
        "${askers:-nothing requests it at all}"
fi

# ============================================================ 6. secrets and logs
section '6. secrets and logs (STANDARDS §0.4, §7.7, §12.3)'

absent 'no API key / private key / client secret in the tree' \
    '(AIza[0-9A-Za-z_-]{20}|-----BEGIN [A-Z ]*PRIVATE KEY|client_secret|SESSION_HMAC|DASHBOARD_TOKEN)' \
    "$APP/src" "$APP/build.gradle.kts"
absent 'no id_token / cookie value reaches a log or a Toast' \
    '(Log\.[a-z]+\(.*(idToken|id_token|credential|Cookie|cookie|token)|Toast.*\b(idToken|token|cookie)\b)' \
    "$APP/src"
# ?lang and ?r are harmless, but the rule is a flat one: no URL in a log line or a Toast.
urls="$(grep -rnE '(Log\.[a-z]+\(|Toast\.makeText\()[^)]*https?://' "$APP/src" 2>/dev/null || true)"
if [ -z "$urls" ]; then ok 'no URL is logged or shown in a Toast'
else bad 'no URL is logged or shown in a Toast' "$urls"; fi

# ============================================================ summary
printf '\n\033[1msummary\033[0m  %d passed, %d failed, %d skipped (owner still a stub)\n' \
    "$pass_n" "$fail_n" "$skip_n"
if [ "$fail_n" -gt 0 ]; then
    printf 'static-audit: RED\n'; exit 1
fi
printf 'static-audit: GREEN%s\n' \
    "$([ "$skip_n" -gt 0 ] && echo " (with $skip_n check(s) waiting on another task)" || true)"
exit 0
