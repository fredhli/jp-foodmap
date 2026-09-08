#!/usr/bin/env bash
#
# Inspect background components and recorded activity; this does not measure energy or heat.
#
#     android/tools/power-audit.sh                      # APK checks + device checks + 5 min soak
#     android/tools/power-audit.sh --apk path/to.apk    # audit a specific APK
#     android/tools/power-audit.sh --soak 60            # a shorter background soak
#     android/tools/power-audit.sh --no-soak            # skip the soak (the components are the point)
#     android/tools/power-audit.sh --no-device          # APK checks only, no emulator needed
#
#     JPFM_EMU_PORT=5564 android/tools/power-audit.sh   # which emulator (default 5554)
#     JPFM_PKG=com.fredhli.jpfoodmap.debug ...          # which installed package
#
# WHY. STANDARDS §10 says this app has no Service, no BroadcastReceiver, no WorkManager, no
# AlarmManager, no JobScheduler, no wakelock, no FCM and no background network — the whole
# power story is "a WebView that is paused when it is not on screen". That is a claim about
# what ISN'T there, and the only honest way to check it is to look: at the merged manifest
# inside the APK (a library can add a component nobody asked for), and at what the running
# system says it is holding on this package's behalf.
#
# The soak is the second half. Components can be absent and the app can still be busy — a
# JS timer that keeps running because pauseTimers() was forgotten, a fetch loop the page
# starts. Backgrounding the app and comparing batterystats before and after is the check
# that notices, and it is deliberately over the SAME counters (wakelocks, jobs, syncs,
# network bytes) that the component checks would have caught statically.
#
# Exit codes: 0 all green · 1 something failed · 2 the environment is not ready.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SRC="$(cd "$HERE/.." && pwd -P)"
# shellcheck source=/dev/null
source "$HERE/env.sh" 2>/dev/null || true

APK=""
SOAK=300
DO_DEVICE=1
while [ "$#" -gt 0 ]; do
    case "$1" in
        --apk)       APK="$2"; shift 2 ;;
        --soak)      SOAK="$2"; shift 2 ;;
        --no-soak)   SOAK=0; shift ;;
        --no-device) DO_DEVICE=""; shift ;;
        -h|--help)   sed -n '2,30p' "$0"; exit 0 ;;
        *) printf 'power-audit: unknown argument %s\n' "$1" >&2; exit 2 ;;
    esac
done

EMU_PORT="${JPFM_EMU_PORT:-5554}"
SERIAL="emulator-${EMU_PORT}"
PKG="${JPFM_PKG:-com.fredhli.jpfoodmap.debug}"

# The APK to read. The default is the release build in whichever scratch this shell points
# at — what ships — falling back to the debug build, then to the published copy.
if [ -z "$APK" ]; then
    for c in \
        "${JPFM_ANDROID_BUILD:-}/app/build/outputs/apk/release/app-release.apk" \
        "${JPFM_ANDROID_BUILD:-}/app/build/outputs/apk/debug/app-debug.apk" \
        "$SRC/apk/jpfoodmap.apk"
    do
        [ -n "$c" ] && [ -f "$c" ] && { APK="$c"; break; }
    done
fi

pass_n=0; fail_n=0; warn_n=0; skip_n=0
ok()   { pass_n=$((pass_n + 1)); printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
bad()  { fail_n=$((fail_n + 1)); printf '  \033[31mFAIL\033[0m  %s\n' "$1"
         [ -n "${2:-}" ] && printf '%s\n' "$2" | sed 's/^/          /'; }
warn() { warn_n=$((warn_n + 1)); printf '  \033[33mWARN\033[0m  %s\n' "$1"
         [ -n "${2:-}" ] && printf '%s\n' "$2" | sed 's/^/          /'; }
skip() { skip_n=$((skip_n + 1)); printf "  SKIP  %s\n" "$1"; }
section() { printf '\n\033[1m%s\033[0m\n' "$1"; }
adbs() { timeout 30 adb -s "$SERIAL" "$@"; }

printf '\033[1mpower-audit\033[0m  apk=%s  pkg=%s  serial=%s\n' "${APK:-none}" "$PKG" "$SERIAL"

# ================================================================= 1. what is in the APK
section '1. the merged manifest (STANDARDS §10.1, §10.3)'

if [ -z "$APK" ] || [ ! -f "$APK" ]; then
    bad 'an APK to audit' 'build one first: android/build.sh assembleDebug (or --apk PATH)'
elif ! command -v aapt2 >/dev/null; then
    bad 'aapt2 on PATH' 'source ~/tools/android-env.sh'
else
    perms="$(aapt2 dump permissions "$APK" | sed -n "s/^uses-permission: name='\([^']*\)'.*/\1/p" | sort -u)"

    # OURS — the five in AndroidManifest.xml, each with a user-visible reason.
    # ACCESS_NETWORK_STATE (added 2026-09-06) is the only one no code of ours calls: it is
    # what lets Chromium's NetworkChangeNotifierAutoDetect register a connectivity callback,
    # which is what makes navigator.onLine — and therefore the site's #net-offline bar —
    # tell the truth in the WebView (STANDARDS §9.1). "normal" level, install-time grant,
    # never prompted, invisible on the system permission page. Battery cost: the callback is
    # the framework's own and Chromium unregisters it while the app is backgrounded.
    ours='android.permission.ACCESS_COARSE_LOCATION
android.permission.ACCESS_FINE_LOCATION
android.permission.ACCESS_NETWORK_STATE
android.permission.INTERNET
android.permission.POST_NOTIFICATIONS'

    # MERGED IN by libraries, each resolved to its source in
    # app/build/outputs/logs/manifest-merger-*-report.txt and recorded in docs/STATUS.md
    # (T1's deviation note). All three are invisible to the user and cost no battery:
    #   USE_BIOMETRIC / USE_FINGERPRINT  androidx.biometric, a hard dependency of
    #       androidx.credentials — which is the native Google sign-in, the one thing this
    #       app cannot do without. Both are "normal" permissions: granted at install, never
    #       prompted, and removing them would be betting that Credential Manager never
    #       reaches BiometricPrompt internally, on the one path that cannot be tested here.
    #   *.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION  androidx.core's own signature-level
    #       permission, used by ContextCompat.registerReceiver below API 33. Signature level
    #       means no other app can ever hold it.
    allowed_extra='android.permission.USE_BIOMETRIC
android.permission.USE_FINGERPRINT
com.fredhli.jpfoodmap.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION
com.fredhli.jpfoodmap.debug.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION'

    missing="$(comm -23 <(printf '%s\n' "$ours") <(printf '%s\n' "$perms"))"
    extra="$(comm -13 <(printf '%s\n' "$ours" "$allowed_extra" | sort -u) <(printf '%s\n' "$perms"))"

    if [ -z "$missing" ]; then ok 'the five declared permissions are all present'
    else bad 'the five declared permissions are all present' "missing: $missing"; fi

    if [ -z "$extra" ]; then ok 'no permission beyond the five and the three explained library ones'
    else bad 'no permission beyond the five and the three explained library ones' \
        "$extra
resolve each against app/build/outputs/logs/manifest-merger-*-report.txt before allowing it"; fi

    forbidden="$(printf '%s\n' "$perms" | grep -E 'READ_EXTERNAL_STORAGE|WRITE_EXTERNAL_STORAGE|MANAGE_EXTERNAL_STORAGE|CAMERA|RECORD_AUDIO|READ_CONTACTS|READ_PHONE_STATE|QUERY_ALL_PACKAGES|REQUEST_INSTALL_PACKAGES|SYSTEM_ALERT_WINDOW|WAKE_LOCK|RECEIVE_BOOT_COMPLETED|FOREGROUND_SERVICE|ACCESS_BACKGROUND_LOCATION' || true)"
    if [ -z "$forbidden" ]; then ok 'none of the hard-no permissions shipped'
    else bad 'none of the hard-no permissions shipped' "$forbidden"; fi

    tree="$(aapt2 dump xmltree --file AndroidManifest.xml "$APK" 2>/dev/null)"

    # The component names actually declared, by kind. `aapt2 dump xmltree` prints the
    # element on one line and its android:name on a following one, so the name is taken from
    # the first `:name(0x01010003)=` after each element header.
    components() { # components <service|receiver|provider>
        printf '%s\n' "$tree" | awk -v want="E: $1 " '
            index($0, want) { grab = 1; next }
            grab && /:name\(0x01010003\)=/ {
                if (match($0, /"[^"]+"/)) print substr($0, RSTART + 1, RLENGTH - 2)
                grab = 0
            }
            /E: (activity|service|receiver|provider|application|activity-alias) / && !index($0, want) { grab = 0 }
        ' | sort -u
    }

    # NOTHING WE WROTE MAY BE A COMPONENT. What a library merges in is a different question,
    # and each one below is named because it was resolved to its library and to a reason it
    # costs no battery:
    #   androidx.credentials.playservices.CredentialProviderMetadataHolder
    #       credentials-play-services-auth. A disabled-by-default metadata holder with no
    #       intent-filter: Credential Manager reads its <meta-data> to discover the provider.
    #       It is never started, and native Google sign-in (docs/PLAN.md D4) is why the
    #       dependency is here at all.
    #   com.google.android.gms.auth.api.signin.RevocationBoundService
    #       play-services-auth, guarded by a GMS signature permission. Google Play services
    #       binds it when an account revokes this app's grant. Nothing else can reach it.
    #   androidx.profileinstaller.ProfileInstallReceiver
    #       androidx.profileinstaller (transitive under core/startup), guarded by
    #       android.permission.DUMP — i.e. only `adb shell cmd package` can send to it. It
    #       writes a baseline profile once at install; it schedules nothing.
    allowed_components='androidx.credentials.playservices.CredentialProviderMetadataHolder
com.google.android.gms.auth.api.signin.RevocationBoundService
androidx.profileinstaller.ProfileInstallReceiver'

    for kind in service receiver; do
        found="$(components "$kind")"
        ours="$(printf '%s\n' "$found" | grep -E '^com\.fredhli\.jpfoodmap' || true)"
        unknown="$(comm -13 <(printf '%s\n' "$allowed_components" | sort -u) \
                            <(printf '%s\n' "$found" | grep . | sort -u) || true)"
        if [ -n "$ours" ]; then
            bad "the app itself declares no <$kind>" "$ours"
        elif [ -z "$unknown" ]; then
            ok "no <$kind> beyond the explained library ones$([ -n "$found" ] && printf ' (%s)' "$(printf '%s' "$found" | tr '\n' ' ')")"
        else
            bad "no <$kind> beyond the explained library ones" \
                "$unknown
resolve each against app/build/outputs/logs/manifest-merger-*-report.txt before allowing it"
        fi
    done

    # A provider is not a wakeup, but androidx.startup's InitializationProvider runs code at
    # process start, so it is worth naming rather than ignoring.
    prov="$(components provider)"
    if [ -z "$prov" ]; then ok 'no <provider> either'
    elif [ "$prov" = "androidx.startup.InitializationProvider" ]; then
        ok 'the only <provider> is androidx.startup.InitializationProvider'
    else warn 'a <provider> other than androidx.startup was merged in' "$prov"; fi
fi

# ================================================================= 2. what the system holds
if [ -z "$DO_DEVICE" ]; then
    printf '\n(device checks skipped: --no-device)\n'
else
section '2. the running system (STANDARDS §10.1)'

if ! adbs get-state >/dev/null 2>&1; then
    bad "$SERIAL is reachable" 'boot it: JPFM_EMU_PORT='"$EMU_PORT"' tools/emu.sh start'
elif ! adbs shell pm path "$PKG" >/dev/null 2>&1; then
    bad "$PKG is installed on $SERIAL" 'install it: tools/emu.sh install'
else
    uid="$(adbs shell dumpsys package "$PKG" 2>/dev/null | tr -d '\r' \
        | grep -oE '(userId|appId)=[0-9]+' | head -1 | cut -d= -f2)"
    printf '          uid=%s\n' "${uid:-?}"

    # Services actually RUNNING under this package's name.
    #
    # One always is, and it is not ours: org.chromium.content.app.SandboxedProcessService is
    # the WebView's renderer, declared in the WebView APK's manifest and hosted under the
    # embedding app's uid. Every WebView app has it, it lives and dies with the WebView, and
    # onPause/pauseTimers is what makes it idle. Anything ELSE running here is a finding.
    running_services() {
        adbs shell dumpsys activity services "$PKG" 2>/dev/null | tr -d '\r' \
            | sed -n 's/.*ServiceRecord{[^ ]* [^ ]* \([^ }]*\).*/\1/p' | sort -u
    }
    svcs="$(running_services)"
    unexpected="$(printf '%s\n' "$svcs" | grep . | grep -vE '/org\.chromium\.' || true)"
    if [ -z "$unexpected" ]; then
        ok "no service running but the WebView renderer$(printf '%s' "$svcs" | grep -c . | sed 's/^/ (/;s/$/ found)/')"
    else
        bad 'no service running but the WebView renderer' "$unexpected"
    fi

    # JobScheduler prints far more than registrations — quota trackers, TopAppTimers and a
    # uid→package map, all of which name a package that has merely been in the foreground.
    # A real registration is a `JOB #<uid>/<id>:` line, and that is the only thing that means
    # this app has scheduled work.
    jobs="$(adbs shell dumpsys jobscheduler 2>/dev/null | tr -d '\r' \
        | grep -E '^\s*JOB #' | grep -F "$PKG" || true)"
    if [ -z "$jobs" ]; then ok 'JobScheduler holds no job for the package'
    else bad 'JobScheduler holds no job for the package' "$jobs"; fi

    # Same trap in the alarm dump: the app-standby and quota sections mention every package
    # the system has seen. A pending alarm is an `Alarm{...}` / `*walarm*` / `RTC` entry.
    alarms="$(adbs shell dumpsys alarm 2>/dev/null | tr -d '\r' \
        | grep -E 'Alarm\{|\*walarm\*|RTC_WAKEUP|ELAPSED_WAKEUP' | grep -F "$PKG" || true)"
    if [ -z "$alarms" ]; then ok 'AlarmManager holds no alarm for the package'
    else bad 'AlarmManager holds no alarm for the package' "$alarms"; fi

    # PowerManager's dump lists every held wakelock. The package name appears in the tag or
    # the owner of any it holds.
    locks="$(adbs shell dumpsys power 2>/dev/null | tr -d '\r' \
        | sed -n '/Wake Locks:/,/^$/p' | grep -F "$PKG" || true)"
    if [ -z "$locks" ]; then ok 'no wakelock is held for the package'
    else bad 'no wakelock is held for the package' "$locks"; fi

    syncs="$(adbs shell dumpsys content 2>/dev/null | tr -d '\r' | grep -F "$PKG" || true)"
    if [ -z "$syncs" ]; then ok 'no sync adapter for the package'
    else warn 'no sync adapter for the package' "$syncs"; fi

    # The notification channel: it must NOT exist until the settings switch has been turned
    # on (STANDARDS §11.2), so this is informational rather than a verdict — which state is
    # correct depends on what the person running the audit has just done.
    chan="$(adbs shell dumpsys notification --noredact 2>/dev/null | tr -d '\r' \
        | grep -c 'jpfoodmap_general' || true)"
    printf '          notification channel jpfoodmap_general: %s\n' \
        "$([ "${chan:-0}" -gt 0 ] && echo 'present (the switch has been turned on)' || echo 'absent (never turned on — correct for a fresh install)')"

    # ------------------------------------------------------- 3. the soak
    if [ "$SOAK" -gt 0 ]; then
        section "3. $SOAK s in the background (STANDARDS §10.2)"
        # Leave time for the page's final keepalive before sampling stable background activity.
        adbs shell input keyevent KEYCODE_HOME >/dev/null 2>&1
        sleep 10
        sample_dir="$(mktemp -d "${TMPDIR:-/tmp}/jpfm-power.XXXXXX")"
        uid="$(adbs shell pm list packages -U --user current "$PKG" 2>/dev/null | tr -d '\r' | awk -v pkg="$PKG" '$1 == "package:" pkg && $2 ~ /^uid:[0-9]+$/ {sub(/^uid:/, "", $2); print $2; exit}')"
        printf '          numeric samples: %s\n' "$sample_dir"
        if [ -z "$uid" ]; then
            skip 'background counters: application UID unavailable'
        elif ! adbs shell dumpsys batterystats --checkin --charged "$PKG" >"$sample_dir/before.csv" 2>/dev/null; then
            skip 'background counters: first sample unavailable'
        else
            printf '          backgrounded; sampling for %ss…\n' "$SOAK"
            sleep "$SOAK"
            if ! adbs shell dumpsys batterystats --checkin --charged "$PKG" >"$sample_dir/after.csv" 2>/dev/null; then
                skip 'background counters: second sample unavailable'
            else
                python3 "$HERE/battery-counters.py" "$uid" "$sample_dir/before.csv" "$sample_dir/after.csv" >"$sample_dir/delta.json"
                result=$?
                case "$result" in
                    0) ok "observed UID activity counters unchanged across ${SOAK}s; not an energy measurement" ;;
                    1) bad "UID activity counters grew across ${SOAK}s" "$(cat "$sample_dir/delta.json")" ;;
                    *) skip "background counters invalid/unavailable; see $sample_dir/delta.json" ;;
                esac
            fi
        fi
        # A second look at the schedulers: something could have registered while we waited.
        after_jobs="$(adbs shell dumpsys jobscheduler 2>/dev/null | tr -d '\r' \
            | grep -E '^\s*JOB #' | grep -F "$PKG" || true)"
        after_alarms="$(adbs shell dumpsys alarm 2>/dev/null | tr -d '\r' \
            | grep -E 'Alarm\{|\*walarm\*|RTC_WAKEUP|ELAPSED_WAKEUP' | grep -F "$PKG" || true)"
        if [ -z "$after_jobs$after_alarms" ]; then ok 'still no job and no alarm after the soak'
        else bad 'still no job and no alarm after the soak' "$after_jobs
$after_alarms"; fi
    fi
fi
fi

# ================================================================= summary
printf '\n\033[1msummary\033[0m  %d passed, %d failed, %d warnings, %d skipped\n' "$pass_n" "$fail_n" "$warn_n" "$skip_n"
[ "$fail_n" -gt 0 ] && { printf 'power-audit: RED\n'; exit 1; }
[ "$skip_n" -gt 0 ] && { printf 'power-audit: INCOMPLETE (invalid or unavailable samples)\n'; exit 2; }
printf 'power-audit: GREEN (observed checks only; no energy/thermal measurement)\n'
exit 0
