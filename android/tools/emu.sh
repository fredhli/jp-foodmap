#!/usr/bin/env bash
#
# Headless Android emulator for the jpfoodmap shell.
#
#     ./emu.sh start           # boot the AVD headless, wait for sys.boot_completed = 1
#     ./emu.sh stop            # shut it down (adb emu kill), clean up the pidfile
#     ./emu.sh status          # running? booted? which accel? where is the log?
#     ./emu.sh restart         # stop, then start
#     ./emu.sh wait            # wait for an already-launched emulator to finish booting
#     ./emu.sh shot out.png    # screencap the current frame to a PNG on this box
#     ./emu.sh rotate landscape|portrait     # swap the window's long axis (see ROTATE below)
#     ./emu.sh install [apk]   # adb install -r -d (defaults to the debug APK in the scratch)
#     ./emu.sh launch [url]    # am start -W; with a url, ACTION_VIEW (the deep-link path)
#     ./emu.sh diag <out.json> # read one diagnostics line out of logcat (tag JpfmDiag)
#     ./emu.sh fold cover|inner # the fold proxy on fold8inner (wm size / wm size reset)
#     ./emu.sh net on|off      # svc wifi/data, for the offline acceptance
#
#     JPFM_AVD=fold8inner ./emu.sh start     # pick the AVD (default: foldcover)
#     JPFM_EMU_PORT=5556 JPFM_EMU_READONLY=1 ./emu.sh start   # a second instance of an AVD
#
# PARALLEL USE (docs/PLAN.md §10). One AVD directory cannot be opened read-write twice, so
# a second instance must set JPFM_EMU_READONLY=1 (-read-only): it boots from the same
# images with its own writable overlay and does not touch the AVD's own data partition.
# Port 5554 is the read-write one; every other port in the plan is a read-only rider. The
# log and the pidfile are per-port for the same reason.
#
# THE AVDs (measured 2026-09-02 by the dashboard project; "this software serves one phone:
# Fred's Galaxy Z Fold 8"). Every number below is what the booted emulator PRINTED, not
# arithmetic — `adb shell wm size` / `wm density`, and the dp the framework itself reports
# in `dumpsys window displays` (the `swNNNdp wNNNdp hNNNdp` field of overrideConfig).
#   foldcover    1248x1972 @ 420 dpi = the Fold 8 COVER screen, dpr 2.625 -> 475x751 dp.
#                The default: the phone spends most of its life shut.
#   fold8inner   2446x1848 @ 420 dpi = the INNER screen (7.6", 4:3, landscape-natural),
#                framework prints `sw704dp w932dp h704dp 420dpi` -> 932x704 dp. Why 2446
#                and not 2448: at 2448 the framework printed w933dp; 2446 is what makes it
#                print the phone's own 932.
#   fold8inner60 1552x1808 @ 420 dpi = the app's window in Samsung's 60/40 split on the
#                inner screen, framework prints `sw591dp w591dp h689dp` -> 591x689 dp.
#                Why not 1551x1809 (the exact product): odd pixel counts upset the display
#                path, and Android ROUNDS px/density to dp — both axes even AND both dp
#                exact is 1552x1808.
#   flow         1080x2400 @ 420 dpi -> 411x914 dp. A plain phone, kept for "does it still
#                work on a normal phone". Not part of the acceptance set.
#
# AN AVD IS A DISPLAY, NEVER A MULTI-WINDOW APP WINDOW. fold8inner60 is a whole screen the
# size of the split window, so it carries the system bars the real split window does not:
# `dumpsys window displays` on it shows statusBars 63 px and navigationBars 63 px (24 dp
# each) where the phone's real 60 % window reported env() bottom = 0. The WIDTH is the
# thing this AVD reproduces faithfully; the vertical insets are the emulator's, so never
# read an inset number off it.
#
# ROTATE is a display-size override, not a rotation: `wm size` grows an "Override size:"
# line, the framework config really does flip (the app and the WebView see the orientation
# change), and `Display.getRotation()` does not move. The override survives until `rotate`
# puts it back or the emulator stops.
#
# WHY THIS EXISTS: the boot line below is not obvious and gets it wrong in expensive ways —
# a missing -no-window opens a WSLg window nobody asked for, a missing -gpu falls back to a
# host GL stack that does not exist here, and without /dev/kvm the emulator dies with a bare
# "KVM is not installed" unless -no-accel is passed.
#
# On the android-37.0 image `adb screencap` fails ("hasReadColorBufferDma") — `shot` falls
# back to the emulator console's host-side screenshot, so always take frames through it.
#
set -euo pipefail

# ---------------------------------------------------------------------------- config
AVD_NAME="${JPFM_AVD:-foldcover}"
EMU_PORT="${JPFM_EMU_PORT:-5554}"          # even port; serial is emulator-<port>
SERIAL="emulator-${EMU_PORT}"
STATE_DIR="$HOME/.android"
LOG="$STATE_DIR/jpfm-emu-${EMU_PORT}.log"
PIDFILE="$STATE_DIR/jpfm-emu-${EMU_PORT}.pid"
PKG="${JPFM_PKG:-}"                         # empty = resolve from the device, see pkg()
MAIN="com.fredhli.jpfoodmap.MainActivity"   # class names are NOT suffixed by the build type

# shellcheck source=/dev/null
source "$HOME/tools/android-env.sh"
EMULATOR="$ANDROID_HOME/emulator/emulator"

die() { printf 'emu.sh: %s\n' "$*" >&2; exit 1; }
note() { printf '  %s\n' "$*"; }

# ------------------------------------------------------------------------ package id
# Which of the two application ids to address. The debug build carries `.debug`, the
# shipped one does not, and `am start -n` at the wrong one is not an error: the activity
# manager answers `result code=-92` on stderr that nobody reads and the app never starts.
# That is exactly how the 2.0.0 audit concluded "release builds have no JpfmDiag line" —
# `emu.sh diag` had defaulted to the debug id against a release install, waited out its
# timeout and exited 3 (2.1.0, audit_outputs/2.1.0-verify/app/REPORT.md "顺带发现 1").
#
# So: JPFM_PKG wins if set (that is how lib-verify.sh passes down what `aapt2 dump badging`
# read off the APK under test), otherwise ask the device. Debug first when BOTH are
# installed, which keeps every earlier invocation of this script doing what it did.
# Memoised because it costs an adb round trip and `diag` calls it in a loop.
pkg() {
    if [ -z "$PKG" ]; then
        local id
        for id in com.fredhli.jpfoodmap.debug com.fredhli.jpfoodmap; do
            if adb -s "$SERIAL" shell pm path "$id" >/dev/null 2>&1; then PKG="$id"; break; fi
        done
        # Neither installed (or no device yet): name the shipped one, so the failure that
        # follows is "not installed" rather than a silent no-op against a phantom .debug.
        PKG="${PKG:-com.fredhli.jpfoodmap}"
    fi
    printf '%s' "$PKG"
}

# ------------------------------------------------------------------------ accel probe
# Writable /dev/kvm is the real test, not group membership: a stale shell can carry the
# group without the device, and a permissive box can grant the device without the group.
have_kvm() { [ -w /dev/kvm ]; }

accel_flags() {
    # -gpu swiftshader_indirect even with KVM: this is a headless WSL box with no usable
    # host GL, and the software rasterizer is what actually renders frames.
    if have_kvm; then echo "-accel on -gpu swiftshader_indirect"
    else echo "-no-accel -gpu swiftshader_indirect"; fi
}

boot_timeout() {
    if [ -n "${JPFM_EMU_BOOT_TIMEOUT:-}" ]; then echo "$JPFM_EMU_BOOT_TIMEOUT"
    elif have_kvm; then echo 420
    else echo 2700
    fi
}

# ------------------------------------------------------------------------------ state
# The pidfile is the honest liveness check for an emulator THIS script started; the pgrep
# fallback catches one started by another session or left by a lost pidfile. Without the
# fallback, `status` says "not running" next to a live adb device and the next session
# cold-boots a second emulator onto the same port.
running() {
    [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null && return 0
    pgrep -f "qemu-system-x86_64.*-port[= ]$EMU_PORT( |\$)" >/dev/null 2>&1
}

emu_pid() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
        cat "$PIDFILE"; return 0
    fi
    # Collect first, take the first line second: `pgrep | head -1` closes the pipe, pgrep
    # takes SIGPIPE, and pipefail turns a successful match into a failure.
    local pids
    pids="$(pgrep -f "qemu-system-x86_64.*-port[= ]$EMU_PORT( |\$)" 2>/dev/null || true)"
    printf '%s\n' "$pids" | sed -n '1p'
}

emu_pid_label() {
    local p; p="$(emu_pid)"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
        printf 'pid %s' "${p:-unknown}"
    else
        printf 'pid %s, started outside this script' "${p:-unknown}"
    fi
}

# A pidfile whose process is gone is a lie, and it is only removed by cmd_stop — so a
# crash or a `wsl --shutdown` strands it. Clear it before anything reads it.
drop_stale_pidfile() {
    if [ -f "$PIDFILE" ] && ! kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
        rm -f "$PIDFILE"
    fi
}

booted() {
    [ "$(adb -s "$SERIAL" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r\n')" = "1" ]
}

# ------------------------------------------------------------------------------ start
cmd_start() {
    [ -x "$EMULATOR" ] || die "no emulator binary at $EMULATOR — run: sdkmanager 'emulator'"

    # Deliberately NOT `avdmanager list avd -c | grep -qx`: under pipefail, grep -q exits on
    # the first match, avdmanager takes SIGPIPE, and the whole pipeline reports failure —
    # an intermittent "no AVD" on a box where the AVD is right there.
    local avds
    avds="$("$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager" list avd -c 2>/dev/null || true)"
    printf '%s\n' "$avds" | grep -qx "$AVD_NAME" \
        || die "no AVD named '$AVD_NAME' (have: $(printf '%s' "$avds" | tr '\n' ' '))"

    drop_stale_pidfile
    if running; then
        # WHICH AVD is on this port? An emulator left over from an earlier `JPFM_KEEP=1` run
        # answers on the same port, and the old code adopted it silently: verify-geometry
        # then measured fold8inner60's 591/688 window and reported "innerWidth expected 932,
        # got 688" — a FAIL about the wrong device, on a build that was fine. Measured
        # 2026-09-06 by gate/emulator; refuse instead, and say how to fix it.
        local live=""
        live="$(adb -s "$SERIAL" emu avd name 2>/dev/null | tr -d '\r' | head -1 || true)"
        if [ -n "$live" ] && [ "$live" != "$AVD_NAME" ]; then
            die "port ${EMU_PORT} already hosts AVD '$live', not '$AVD_NAME' — stop it first (JPFM_AVD=$live tools/emu.sh stop) or use another JPFM_EMU_PORT"
        fi
        note "already running ($(emu_pid_label)); waiting for boot"
        cmd_wait
        return
    fi

    if have_kvm; then note "KVM: available — hardware accelerated"
    else note "KVM: UNAVAILABLE (/dev/kvm not writable) — software emulation, this is SLOW."; fi

    local ro=""
    if [ -n "${JPFM_EMU_READONLY:-}" ]; then
        ro="-read-only"
        note "read-only: this instance shares the AVD images and keeps no state"
    fi

    adb start-server >/dev/null 2>&1 || true
    mkdir -p "$STATE_DIR"

    # -no-snapshot-load forces a real cold boot; a half-written snapshot from a killed run
    # is a classic "boots to a black screen forever". -wipe-data is deliberately NOT here:
    # it would throw away installed APKs on every start.
    # shellcheck disable=SC2086
    nohup "$EMULATOR" -avd "$AVD_NAME" \
        -port "$EMU_PORT" \
        -no-window -no-audio -no-boot-anim \
        -no-snapshot-load $ro \
        -camera-back none -camera-front none \
        $(accel_flags) \
        > "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    note "launched pid $(cat "$PIDFILE") as $SERIAL; log: $LOG"

    # An AVD directory can only be opened read-write once, and on this box several tasks
    # share the same four AVDs. When someone else already holds this one, the emulator
    # exits within a couple of seconds with "Another emulator instance is running" — which
    # is not a reason to fail a verify run, because -read-only boots from the same images
    # with a private overlay and is good for everything except keeping state.
    if [ -z "$ro" ]; then
        sleep 4
        if ! running && grep -q "Another emulator instance is running" "$LOG" 2>/dev/null; then
            note "that AVD is already open read-write elsewhere — retrying with -read-only"
            rm -f "$PIDFILE"
            # shellcheck disable=SC2086
            nohup "$EMULATOR" -avd "$AVD_NAME" \
                -port "$EMU_PORT" \
                -no-window -no-audio -no-boot-anim \
                -no-snapshot-load -read-only \
                -camera-back none -camera-front none \
                $(accel_flags) \
                > "$LOG" 2>&1 &
            echo $! > "$PIDFILE"
            note "relaunched pid $(cat "$PIDFILE") as $SERIAL (read-only)"
        fi
    fi
    cmd_wait
}

# ------------------------------------------------------------------------------- wait
cmd_wait() {
    local timeout deadline waited=0
    timeout="$(boot_timeout)"
    deadline=$(( $(date +%s) + timeout ))
    note "waiting up to ${timeout}s for sys.boot_completed=1"

    while [ "$(date +%s)" -lt "$deadline" ]; do
        if ! running; then
            note "emulator process exited — last log lines:"
            tail -n 20 "$LOG" >&2 || true
            die "emulator died during boot (see $LOG)"
        fi
        if booted; then
            note "booted after ${waited}s"
            adb -s "$SERIAL" shell wm dismiss-keyguard >/dev/null 2>&1 || true
            # The android-37.0 image's SurfaceFlinger crashloops on this emulator:
            # RegionSamplingThread trips the goldfish mapper's 'hasReadColorBufferDma'
            # assertion on its host-readback path under swiftshader, killing surfaceflinger
            # and system_server every couple of minutes ("Can't find service: package"
            # mid-install is the symptom). Disabling luma sampling removes the only caller.
            # The prop is read at SF start, so kill SF once when it was not set; init
            # restarts it. Harmless no-op on images that do not crash (android-35).
            if [ "$(adb -s "$SERIAL" shell getprop debug.sf.luma_sampling 2>/dev/null | tr -d '\r')" != "0" ]; then
                adb -s "$SERIAL" root >/dev/null 2>&1 || true
                adb -s "$SERIAL" wait-for-device >/dev/null 2>&1 || true
                adb -s "$SERIAL" shell setprop debug.sf.luma_sampling 0 >/dev/null 2>&1 || true
                adb -s "$SERIAL" shell pkill surfaceflinger >/dev/null 2>&1 || true
                sleep 5
            fi
            return 0
        fi
        sleep 10
        waited=$(( waited + 10 ))
        if [ $(( waited % 60 )) -eq 0 ]; then
            note "  ${waited}s: $(adb devices | grep "$SERIAL" || echo 'no device yet')"
        fi
    done
    die "timed out after ${timeout}s waiting for boot (see $LOG); raise JPFM_EMU_BOOT_TIMEOUT"
}

# ------------------------------------------------------------------------------- stop
cmd_stop() {
    if printf '%s\n' "$(adb devices 2>/dev/null || true)" | grep -q "^$SERIAL"; then
        adb -s "$SERIAL" emu kill >/dev/null 2>&1 || true
    fi
    # Drain on `running`, not on the pidfile: an emulator another session started has no
    # pidfile here, and reporting "stopped" while qemu is still shutting down makes the next
    # `start` take the "already running" branch and sit in cmd_wait having launched nothing.
    local pid waited=0
    while running && [ "$waited" -lt 30 ]; do
        if [ "$waited" -eq 10 ]; then
            pid="$(emu_pid)"
            [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
        fi
        sleep 1
        waited=$(( waited + 1 ))
    done
    rm -f "$PIDFILE"
    if running; then
        note "still shutting down after ${waited}s — pid $(emu_pid) is still there"
    else
        note "stopped"
    fi
}

# ----------------------------------------------------------------------------- status
cmd_status() {
    printf 'AVD       : %s (serial %s%s)\n' "$AVD_NAME" "$SERIAL" \
        "$([ -n "${JPFM_EMU_READONLY:-}" ] && echo ', read-only' || true)"
    if have_kvm; then printf 'accel     : KVM available (hardware)\n'
    else printf 'accel     : NO KVM — software emulation\n'; fi
    if running; then printf 'process   : running (%s)\n' "$(emu_pid_label)"
    else printf 'process   : not running\n'; fi
    local adbline
    adbline="$(printf '%s\n' "$(adb devices 2>/dev/null || true)" | grep "$SERIAL" || echo 'device not listed')"
    if ! running && [ "$adbline" != "device not listed" ]; then
        adbline="$adbline   (stale — adb has not reaped it yet; clears in a few seconds)"
    fi
    printf 'adb       : %s\n' "$adbline"
    if booted; then
        printf 'boot      : COMPLETE (sdk %s)\n' \
            "$(adb -s "$SERIAL" shell getprop ro.build.version.sdk | tr -d '\r\n')"
        # Collect first, match second. `dumpsys | grep -m1` makes grep close the pipe, sends
        # dumpsys SIGPIPE, and under pipefail the whole pipeline "fails" — so the `|| echo
        # unknown` fired on a successful match and printed the geometry AND "unknown".
        local displays geom
        displays="$(adb -s "$SERIAL" shell dumpsys window displays 2>/dev/null | tr -d '\r' || true)"
        geom="$(printf '%s\n' "$displays" | grep -m1 -oE 'sw[0-9]+dp w[0-9]+dp h[0-9]+dp [0-9]+dpi' || true)"
        printf 'geometry  : %s\n' "${geom:-unknown}"
    else
        printf 'boot      : not complete\n'
    fi
    printf 'log       : %s\n' "$LOG"
}

# ------------------------------------------------------------------------------- shot
cmd_shot() {
    local out="${1:-$HOME/jpfm-emu-shot.png}"
    booted || die "emulator is not booted; run: $0 start"
    # On the android-37.0 image every `screencap` ABORTS in the goldfish mapper
    # ("Assertion failed: !rcEnc->featureInfo()->hasReadColorBufferDma") and leaves a
    # tombstone. One is harmless; an acceptance run takes twenty frames, so the first
    # failure is remembered per emulator and the rest go straight to the console's
    # host-side screenshot. JPFM_SHOT_MODE=screencap|console|auto overrides the memory.
    local mode="${JPFM_SHOT_MODE:-auto}" memo="$STATE_DIR/jpfm-shot-${EMU_PORT}.mode"
    if [ "$mode" = auto ] && [ -r "$memo" ]; then mode="$(cat "$memo")"; fi
    if [ "$mode" != console ]; then
        adb -s "$SERIAL" exec-out screencap -p > "$out" || true
        if [ "$(head -c4 "$out" 2>/dev/null | od -An -tx1 | tr -d ' \n')" != "89504e47" ]; then
            printf 'console\n' > "$memo"
        fi
    else
        # Truncate first: re-shooting the same path must never leave the PREVIOUS frame
        # behind for the magic check below to accept as this one.
        : > "$out"
    fi
    # exec-out avoids the CRLF mangling `shell screencap -p >` suffers from — but on the
    # android-37.0 image the guest readback itself ABORTS in the goldfish mapper, and the
    # "PNG" is then the abort message. Check the magic and fall back to the console's
    # host-side screenshot, which bypasses guest gralloc entirely.
    if [ "$(head -c4 "$out" 2>/dev/null | od -An -tx1 | tr -d ' \n')" != "89504e47" ]; then
        local tmpd; tmpd="$(mktemp -d)"
        adb -s "$SERIAL" emu screenrecord screenshot "$tmpd" >/dev/null 2>&1 || true
        sleep 1
        local shot; shot="$(ls -t "$tmpd"/Screenshot_*.png 2>/dev/null | sed -n 1p)"
        [ -n "$shot" ] || die "both screencap and the host-side console screenshot failed"
        mv -f "$shot" "$out"
        rmdir "$tmpd" 2>/dev/null || true
    fi
    [ -s "$out" ] || die "screencap produced an empty file"
    note "wrote $out ($(stat -c%s "$out") bytes)"
}

# ----------------------------------------------------------------------------- rotate
cmd_rotate() {
    local want="${1:-}"
    case "$want" in landscape|portrait) ;; *) die "usage: $0 rotate landscape|portrait" ;; esac
    booted || die "emulator is not booted; run: $0 start"

    local phys w h lo hi
    phys="$(adb -s "$SERIAL" shell wm size 2>/dev/null | tr -d '\r' | sed -n 's/^Physical size: \([0-9]*x[0-9]*\)$/\1/p')"
    [ -n "$phys" ] || die "could not read 'Physical size:' from wm size"
    w="${phys%x*}"; h="${phys#*x}"
    if [ "$w" -le "$h" ]; then lo="$w"; hi="$h"; else lo="$h"; hi="$w"; fi

    # When the target IS the natural geometry, `reset` rather than an explicit size: an
    # override equal to the physical size is still an override and shows up in every later
    # `wm size`.
    local target
    if [ "$want" = landscape ]; then target="${hi}x${lo}"; else target="${lo}x${hi}"; fi
    if [ "$target" = "$phys" ]; then
        adb -s "$SERIAL" shell wm size reset >/dev/null 2>&1 || true
        note "rotate $want: natural orientation — override reset"
    else
        adb -s "$SERIAL" shell wm size "$target" >/dev/null 2>&1 || true
        note "rotate $want: override $target"
    fi

    sleep 4
    adb -s "$SERIAL" shell wm size 2>/dev/null | tr -d '\r' | sed 's/^/  /'
    local dp
    dp="$(adb -s "$SERIAL" shell dumpsys window displays 2>/dev/null | tr -d '\r' \
          | grep -m1 -oE 'sw[0-9]+dp w[0-9]+dp h[0-9]+dp [0-9]+dpi' || true)"
    # An `if`, not `[ -n … ] && note …`: under `set -e` that one-liner makes an empty $dp
    # the function's failing exit status and takes the script down.
    if [ -n "$dp" ]; then note "config: $dp"; fi
}

# ---------------------------------------------------------------------------- install
cmd_install() {
    # Defaults to the DEBUG apk: it carries the .debug applicationId, so installing it can
    # never overwrite whatever release build is on the device.
    local apk="${1:-${JPFM_ANDROID_BUILD:-$HOME/.cache/jpfoodmap-android}/app/build/outputs/apk/debug/app-debug.apk}"
    [ -f "$apk" ] || die "no APK at $apk — build one with: android/build.sh assembleDebug"
    booted || die "emulator is not booted; run: $0 start"
    # -r reinstall, -d allow downgrade: rebuilds keep the same versionCode, and without -d
    # a reinstall of an equal version is rejected as a downgrade on some API levels.
    adb -s "$SERIAL" install -r -d "$apk"
    note "installed $(basename "$apk")"
}

# ----------------------------------------------------------------------------- launch
# `launch` with no URL is the launcher tap; with one it is the App Links path, which is what
# the deep-link acceptance uses. -W waits for the launch to complete and prints the
# TotalTime the cold-start budget is measured against.
cmd_launch() {
    booted || die "emulator is not booted; run: $0 start"
    local url="${1:-}"
    if [ -n "$url" ]; then
        adb -s "$SERIAL" shell am start -W -a android.intent.action.VIEW \
            -d "$url" -n "$(pkg)/$MAIN" 2>&1 | tr -d '\r' | sed 's/^/  /'
    else
        adb -s "$SERIAL" shell am start -W -n "$(pkg)/$MAIN" 2>&1 | tr -d '\r' | sed 's/^/  /'
    fi
}

# ------------------------------------------------------------------------------- diag
# STANDARDS §12.2: `am start --ez diagnostics_log true` makes a READY app print ONE line of
# diagnostics JSON to logcat under the tag JpfmDiag. This reads it back into a file.
#
# Three exit codes, and the difference matters to every verify script:
#   0  a valid JSON object is in <out.json>
#   3  the app printed nothing — a build without the feature (the T1 skeleton is one), or an
#      app that never reached READY. The caller reports SKIP, never FAIL.
#   1  a line arrived and was not valid JSON. That is a real defect.
#
# Deliberately NOT `logcat -c` first: a flow run collects logcat evidence around these calls
# and clearing the buffer would throw it away. The line count before the trigger is the
# marker instead, and the JSON is taken from the LAST JpfmDiag line, so a stale line from an
# earlier call can never be mistaken for this one's answer.
#
# `am start -n` with no data URI on a singleTask activity is delivered as onNewIntent — it
# does not restart the activity and does not reload the page, which is what makes it safe to
# call between the halves of the fold-proxy acceptance.
cmd_diag() {
    local out="${1:-}"
    [ -n "$out" ] || die "usage: $0 diag <out.json>"
    booted || die "emulator is not booted; run: $0 start"
    local timeout="${JPFM_DIAG_TIMEOUT:-20}"

    local before after waited=0 raw=""
    before="$(adb -s "$SERIAL" logcat -d -s JpfmDiag:V 2>/dev/null | wc -l | tr -d ' ')"
    adb -s "$SERIAL" shell am start -n "$(pkg)/$MAIN" --ez diagnostics_log true \
        >/dev/null 2>&1 || true

    while [ "$waited" -lt "$timeout" ]; do
        sleep 1
        waited=$(( waited + 1 ))
        after="$(adb -s "$SERIAL" logcat -d -s JpfmDiag:V 2>/dev/null | wc -l | tr -d ' ')"
        if [ "${after:-0}" -gt "${before:-0}" ]; then
            raw="$(adb -s "$SERIAL" logcat -d -s JpfmDiag:V 2>/dev/null | tr -d '\r' \
                   | grep -F '{' | tail -n 1 || true)"
            [ -n "$raw" ] && break
        fi
    done

    if [ -z "$raw" ]; then
        printf 'emu.sh diag: no JpfmDiag line after %ss — this build has no diagnostics log\n' \
            "$timeout" >&2
        printf '  (tools/diag.sh falls back to the DevTools probe on a debug build)\n' >&2
        exit 3
    fi

    # Everything from the first brace: the logcat prefix (date, pid, tid, level, tag) is
    # fixed-width in principle and reformatted in practice.
    printf '%s' "${raw#*\{}" | sed 's/^/{/' > "$out"
    if ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$out" 2>/dev/null; then
        printf 'emu.sh diag: the JpfmDiag line is not valid JSON (kept at %s)\n' "$out" >&2
        exit 1
    fi
    note "diag: wrote $out ($(stat -c%s "$out") bytes) after ${waited}s"
}

# ------------------------------------------------------------------------------- fold
# The fold proxy (docs/PLAN.md D11). One AVD is one display, so a real fold cannot be
# emulated; what CAN be reproduced is the config change a fold produces, because both Fold 8
# screens run at 420 dpi and differ only in size. `wm size 1248x1972` on the inner-screen AVD
# hands the app exactly the cover screen's geometry through the same onConfigurationChanged
# path, and `wm size reset` hands it back.
#
# Refused on any other AVD on purpose: on foldcover "cover" would be a no-op and "inner"
# would invent a geometry the phone does not have, and a green run on a fake geometry is
# worse than no run.
cmd_fold() {
    local want="${1:-}"
    case "$want" in cover|inner) ;; *) die "usage: $0 fold cover|inner" ;; esac
    booted || die "emulator is not booted; run: $0 start"
    if [ "$AVD_NAME" != "fold8inner" ] && [ -z "${JPFM_FOLD_FORCE:-}" ]; then
        die "fold is only meaningful on the fold8inner AVD (this is '$AVD_NAME'); set
        JPFM_FOLD_FORCE=1 if you really mean to override the size of a different display"
    fi

    if [ "$want" = cover ]; then
        adb -s "$SERIAL" shell wm size 1248x1972 >/dev/null 2>&1 || true
    else
        adb -s "$SERIAL" shell wm size reset >/dev/null 2>&1 || true
    fi
    # 4 s, the same settle used by rotate: the config change, the WebView relayout and the
    # page's own resize handler (Leaflet's trackResize -> invalidateSize) all have to land
    # before a screenshot or a diag means anything.
    sleep 4
    adb -s "$SERIAL" shell wm size 2>/dev/null | tr -d '\r' | sed 's/^/  /'
    local dp
    dp="$(adb -s "$SERIAL" shell dumpsys window displays 2>/dev/null | tr -d '\r' \
          | grep -m1 -oE 'sw[0-9]+dp w[0-9]+dp h[0-9]+dp [0-9]+dpi' || true)"
    if [ -n "$dp" ]; then note "fold $want: $dp"; fi
}

# -------------------------------------------------------------------------------- net
# STANDARDS §9.1 wants a cold start with no network after the service worker has been
# primed. Both radios go down: wifi is what the emulator actually uses, and mobile data is
# what would silently keep the page online if only wifi were cut.
cmd_net() {
    local want="${1:-}"
    case "$want" in on|off) ;; *) die "usage: $0 net on|off" ;; esac
    booted || die "emulator is not booted; run: $0 start"
    local verb; [ "$want" = on ] && verb=enable || verb=disable
    adb -s "$SERIAL" shell svc wifi "$verb" >/dev/null 2>&1 || true
    adb -s "$SERIAL" shell svc data "$verb" >/dev/null 2>&1 || true
    sleep 3
    # `dumpsys connectivity` is enormous; the one line that answers "is there a default
    # network" is enough evidence for a summary file.
    local net
    net="$(adb -s "$SERIAL" shell dumpsys connectivity 2>/dev/null | tr -d '\r' \
           | grep -m1 -E '^ *Active default network|^ *Current state:' || true)"
    note "net $want${net:+ — $net}"
}

case "${1:-status}" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_stop; cmd_start ;;
    status)  cmd_status ;;
    wait)    cmd_wait ;;
    shot)    shift; cmd_shot "$@" ;;
    rotate)  shift; cmd_rotate "$@" ;;
    install) shift; cmd_install "$@" ;;
    launch)  shift; cmd_launch "$@" ;;
    diag)    shift; cmd_diag "$@" ;;
    fold)    shift; cmd_fold "$@" ;;
    net)     shift; cmd_net "$@" ;;
    *)       die "usage: $0 {start|stop|restart|status|wait|install [apk]|shot [out.png]|rotate landscape|portrait|launch [url]|diag <out.json>|fold cover|inner|net on|off}" ;;
esac
