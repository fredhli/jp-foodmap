#!/usr/bin/env bash
#
# The behaviour acceptance: everything in docs/STANDARDS.md that is a sequence rather than a
# measurement — back, deep links, external links, share, offline, location, night mode, font
# scale, and the sign-in degradation.
#
#     ./verify-flows.sh                     # every flow, on the default AVD (foldcover)
#     ./verify-flows.sh back deeplink       # just these
#     JPFM_AVD=fold8inner ./verify-flows.sh
#     JPFM_APK=~/.cache/.../app-debug.apk ./verify-flows.sh
#
# Flows: back deeplink external share offline location night fontscale signin
#
# READ THIS BEFORE BELIEVING A GREEN RUN. Four of these can only be *finished* on the phone,
# and the script says so rather than pretending:
#   external  the emulator image has no Chrome, so the Custom Tab rung of the ladder cannot
#             be reached; what is checkable here is that the main WebView never navigates
#             off jpfoodmap.com and that the shell does not crash.
#   share     the chooser is a system UI; the assertion is that it comes up in our own task.
#   location  a permission dialog can be seen and answered, and `geo fix` moves the map, but
#             a real GPS fix cannot.
#   signin    the emulator has no Google account, so only the degradation path (STANDARDS
#             §7.4) is reachable — which is exactly the thing worth testing before the phone.
#
# Every flow is independent and starts from a cold app. A flow whose subject does not exist
# in this build reports SKIP; only a wrong answer is FAIL. Exit code = number of FAILs.

# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib-verify.sh"

AVD="${JPFM_AVD:-foldcover}"
FLOWS=("$@")
if [ "${#FLOWS[@]}" -eq 0 ]; then
    FLOWS=(back deeplink external share offline location night fontscale signin)
fi

# ------------------------------------------------------------------ device introspection
# top_window / top_pkg / app_alive live in lib-verify.sh: cold_start uses them too.

# The task an activity sits in, from the activity dump. "Same task" is the whole point of
# the Custom Tab acceptance (STANDARDS §5.2: no second card in Recents).
# NOTE (gate/emulator 2026-09-06): this used to `break` out of the loop on the first match.
# Python then closed its stdin while `adb shell dumpsys activity activities` was still
# writing, the writer took SIGPIPE, and under `set -o pipefail` this assignment returned
# 141 — which `set -e` turned into "the whole flows run aborted", silently, right after the
# external flow printed its first info line. Twice (T8's report, and again here). Drain the
# input instead and keep the FIRST match; a dumpsys is a few hundred KB, the cost is nil.
task_of() {
    { adbs shell dumpsys activity activities 2>/dev/null || true; } | tr -d '\r' \
    | python3 -c '
import re, sys
want = sys.argv[1]; cur = ""; ans = ""
for line in sys.stdin:
    m = re.search(r"Task\{[0-9a-f]+ #(\d+)", line) or re.search(r"Task id #(\d+)", line)
    if m:
        cur = m.group(1)
    if not ans and want in line and cur:
        ans = cur
print(ans)
' "$1"
}

logcat_has_secret() {
    adbs shell "logcat -d -t 2000" 2>/dev/null | tr -d '\r' \
        | grep -Ec 'id_token|idToken|eyJhbGciOi' || true
}

# ---------------------------------------------------------------------------- the flows
flow_back() {
    head2 "back key — one BACK closes the topmost overlay, the last one leaves the app (§4.1)"

    # WHY THIS FLOW LOOKS LIKE THIS (2.2.0, android-back). Until 2.2.0 the whole flow hung
    # off open_card, whose only release-capable rung is the cold `?r=` deep link — and that
    # is the ONE path where leaving the app on the first BACK is correct, because the page
    # opened the card with no user activation and Chromium's history-manipulation
    # intervention makes the entry underneath unreachable (the same thing Chrome does with
    # that URL in a fresh tab). So the acceptance was measuring the exception, reporting the
    # two interesting lines as probe-only SKIPs on every release build, and INTEGRATE-1 §6
    # read the result as "the App's back key is broken". It is not: an overlay the user
    # opened with a finger pops one layer per press.
    #
    # Now there are three segments, and the assertions in all three are probe-free — they
    # read `dumpsys window` and the shell's own `back` block (2.2.0+) and nothing else, so
    # they can go red on the release APK that ships.
    local st base idx t
    cold_start >/dev/null
    wait_page 40
    # A first run — a fresh AVD, or anything after `pm clear` — puts the page's language
    # chooser over the map, and its scrim eats every tap the segment below wants to make.
    dismiss_first_run
    wait_page 20
    back_source_reset
    st="$(back_state cold)"
    # back_state retries with the native source forced before it gives up, so the two cases
    # below really are different things and must not read the same. Saying "pre-2.2.0" about
    # a 2.2.0 APK whose block was simply unreadable is how this segment quietly went back to
    # being decorative (review-1).
    if [ "$(back_source)" = native ]; then
        info "back readings: forced the native JpfmDiag source (this run detected '${JPFM_DIAG_SOURCE:-auto}', which carries no \`back\` block)"
    fi
    if [ -z "$st" ] && [ "$(back_source)" = none ]; then
        skip "back stack readings (this build's diagnostics carry no \`back\` block — pre-2.2.0)"
    elif [ -z "$st" ]; then
        skip "back stack readings (the app reported no diagnostics line at all, native included — the device, not the build)"
    else
        info "back stack after a cold start: index ${st%% *}, callback enabled ${st#* }"
    fi

    # ---- 1. an overlay opened WITH A FINGER pops, and the app stays ----------------------
    base="${st%% *}"
    if [ -n "$st" ]; then
        if ! tap_element '#fab-layers'; then tap_map_fab "$base"; fi
        st="$(back_state opened)"
        idx="${st%% *}"
        if [ -z "$st" ] || [ "$idx" = "$base" ]; then
            skip "first BACK on a finger-opened overlay (could not open one: index still ${idx:-?})"
        else
            info "overlay open: index $base -> $idx, callback enabled ${st#* }"
            expect "the shell takes the back press while an overlay is open" "${st#* }" "true"
            adbs shell input keyevent KEYCODE_BACK >/dev/null 2>&1 || true
            sleep 2
            # The focused window FIRST: back_state asks for the diagnostics line with
            # `am start`, which would haul the app back to the front and hide the answer.
            t="$(top_pkg_settled)"
            shot back-1-after-first >/dev/null
            if [ "$t" != "$PKG" ]; then
                fail "the first BACK left the app with an overlay open (top window is now ${t:-<none>})"
            else
                pass "the app is still in front after the first BACK (top window $t)"
                st="$(back_state popped)"
                expect "the overlay's history entry is gone after the first BACK" \
                       "${st%% *}" "$base"
            fi
        fi
    fi

    # ---- 2. with the overlay stack empty, BACK leaves --------------------------------------
    adbs shell input keyevent KEYCODE_BACK >/dev/null 2>&1 || true
    sleep 3
    t="$(top_pkg_settled)"
    if [ -z "$t" ]; then
        skip "BACK on an empty overlay stack (could not read the focused window)"
    elif [ "$t" = "$PKG" ]; then
        fail "BACK on an empty overlay stack stayed in the app (top window is still $t)"
    else
        pass "BACK on an empty overlay stack left the app (top window is now $t)"
    fi
    shot back-2-after-second >/dev/null

    # ---- 3. a cold `?r=` deep link: one BACK leaves, and NEVER does nothing ---------------
    # The documented exception, asserted as an invariant rather than as an outcome so that a
    # future change which makes the card pop instead is a PASS and only a SWALLOWED press —
    # app still in front, history where it was, nothing on screen changed — is a FAIL. That
    # is the failure mode a naive fix produces: trusting copyBackForwardList().currentIndex
    # enables the callback, goBackOrForward(-1) then moves nothing, and the back key dies.
    local rung
    rung="$(open_card "$SHARE_ID" cold || true)"
    if [ -z "$rung" ]; then
        # No probe to confirm the card with, so fall back to the shell's own witness: the
        # page pushed an entry for the card it opened, which is index 1 on a fresh document.
        st="$(back_state deeplink)"
        if [ -z "$st" ] || [ "${st%% *}" = "0" ] || [ "${st%% *}" = "-1" ]; then
            skip "cold deep-link BACK (no card could be opened — nothing to pop)"
            return
        fi
        info "cold ?r= pushed a history entry (index ${st%% *}, callback enabled ${st#* })"
    else
        info "card opened via $rung"
        st="$(back_state deeplink)"
        [ -n "$st" ] && info "cold ?r= back stack: index ${st%% *}, callback enabled ${st#* }"
    fi
    local before="${st%% *}"
    adbs shell input keyevent KEYCODE_BACK >/dev/null 2>&1 || true
    sleep 3
    t="$(top_pkg_settled)"
    shot back-3-deeplink >/dev/null
    if [ "$t" != "$PKG" ]; then
        pass "one BACK on a cold ?r= deep link left the app (as Chrome does with that URL)"
    else
        st="$(back_state deeplink-after)"
        if [ -z "$st" ]; then
            skip "cold deep-link BACK (app still in front and no back stack to read)"
        elif [ "${st%% *}" != "$before" ]; then
            pass "one BACK on a cold ?r= deep link popped the card instead (index $before -> ${st%% *})"
        else
            fail "one BACK on a cold ?r= deep link did nothing at all (index still $before)"
        fi
    fi
}

flow_deeplink() {
    head2 "deep links — cold ?r= opens that card, hot ?r= switches without reloading (§6.2/6.3)"
    local host="${SITE#https://}"; host="${host%%/*}"
    adbs shell pm set-app-links --package "$PKG" 1 "$host" >/dev/null 2>&1 || true
    local links; links="$(adbs shell pm get-app-links "$PKG" 2>/dev/null | tr -d '\r' || true)"
    printf '%s\n' "$links" > "$OUT_ROOT/$AVD/app-links.txt"
    # A case match, not `printf | grep -q`: under `set -o pipefail` a grep that exits on
    # its first match can leave the pipeline reporting failure, and this check then says
    # "no App Links" about a package whose table is right there in app-links.txt.
    case "$links" in
        *"$host"*) pass "$host is in this build's App Links table (see app-links.txt)" ;;
        *)         skip "App Links table does not list $host (no autoVerify filter in this build?)" ;;
    esac

    # Cold: no -n, so the intent has to be resolved by the link filter, which is the thing
    # the phone will do.
    adbs shell am force-stop "$PKG" >/dev/null 2>&1 || true
    sleep 1
    adbs shell am start -a android.intent.action.VIEW -d "$SITE/?r=$SHARE_ID" \
        >"$OUT_ROOT/$AVD/deeplink-cold.log" 2>&1 || true
    wait_page 40
    local t; t="$(top_pkg)"
    if [ "$t" != "$PKG" ]; then
        skip "cold deep link (the VIEW intent went to $t, not to the app — no verified links)"
    else
        expect "cold ?r= opened a card" "$(card_open)" "true"
        shot deeplink-cold >/dev/null
    fi

    # Hot: same app, a different restaurant. Two independent witnesses that the page was
    # not reloaded, because neither is always available:
    #   pageLoads   the shell's own counter, out of the JpfmDiag line. Present on a RELEASE
    #               APK and needs nothing from the page, so this is the one that survives
    #               when there is no DevTools probe — which is every gate run.
    #   bootId      window.__jpfmBootId, minted once per document by the page's app-bridge
    #               block. Only there once that block is deployed, but it is the page's own
    #               word for "this is the same document".
    # A recreated activity resets pageLoads to 1, so activityCreates gates the reading:
    # without that guard a 1 -> 1 after a process restart reads as a PASS about nothing.
    local before after b2 a2 dbefore dafter bpl apl bac aac
    dbefore="$OUT_ROOT/$AVD/deeplink-hot-before.json"
    dafter="$OUT_ROOT/$AVD/deeplink-hot-after.json"
    # rm first: diag.sh leaves the previous file in place when it cannot read a line, and
    # answering with a stale JSON is worse than reporting no reading.
    rm -f "$dbefore" "$dafter"
    diagsh capture "$dbefore" >/dev/null 2>&1 || true
    bpl="$(diagsh get "$dbefore" pageLoads 2>/dev/null || true)"
    bac="$(diagsh get "$dbefore" activityCreates 2>/dev/null || true)"
    before="$(page_json '(function(){var e=document.getElementById("bs-content");return e?e.textContent.slice(0,60):""})()')"
    b2="$(page_json 'window.__jpfmBootId||window.__jpfmProbeBoot||""')"
    adbs shell am start -a android.intent.action.VIEW -d "$SITE/?r=$SHARE_ID2" \
        >>"$OUT_ROOT/$AVD/deeplink-cold.log" 2>&1 || true
    sleep 4
    diagsh capture "$dafter" >/dev/null 2>&1 || true
    apl="$(diagsh get "$dafter" pageLoads 2>/dev/null || true)"
    aac="$(diagsh get "$dafter" activityCreates 2>/dev/null || true)"
    after="$(page_json '(function(){var e=document.getElementById("bs-content");return e?e.textContent.slice(0,60):""})()')"
    a2="$(page_json 'window.__jpfmBootId||window.__jpfmProbeBoot||""')"
    if [ -z "$before$after" ]; then
        skip "hot deep link (no DevTools probe — cannot read the card without one)"
    elif [ "$before" = "$after" ]; then
        fail "hot deep link did not switch the card (content unchanged)"
    else
        pass "hot deep link switched the card"
    fi
    info "pageLoads ${bpl:-?} -> ${apl:-?}   activityCreates ${bac:-?} -> ${aac:-?}"
    if [ -z "$bpl" ] || [ -z "$apl" ]; then
        skip "pageLoads across the hot deep link (no JpfmDiag line — run with JPFM_DIAG_SOURCE=native)"
    elif [ -z "$bac" ] || [ "$bac" != "$aac" ]; then
        skip "pageLoads across the hot deep link (the activity was recreated: ${bac:-?} -> ${aac:-?})"
    else
        expect "pageLoads unchanged across the hot deep link" "$apl" "$bpl"
    fi
    if [ -n "$b2" ] && [ "$b2" != '""' ]; then
        expect "the page did not reload on the hot deep link" "$a2" "$b2"
    else
        skip "reload check on the hot deep link (no boot id available)"
    fi
    shot deeplink-hot >/dev/null
}

flow_external() {
    head2 "external links — they leave the WebView, in our own task (§5.2/5.3)"
    local browser
    browser="$(adbs shell cmd package resolve-activity --brief -a android.intent.action.VIEW \
               -d 'https://example.com/' 2>/dev/null | tr -d '\r' | tail -1)"
    info "the image resolves https:// to: ${browser:-<nothing>}"
    local rung; rung="$(open_card "$SHARE_ID" cold || true)"
    if [ -z "$rung" ]; then skip "external link (no card to click one in)"; return; fi
    local before_url our_task lg
    before_url="$(page_json 'location.origin')"
    our_task="$(task_of "$PKG/.MainActivity")"
    lg="$OUT_ROOT/$AVD/external-logcat.txt"
    adbs logcat -c >/dev/null 2>&1 || true
    if tap_element '#bs-content a[href*="tabelog.com"]'; then
        sleep 4
        # THE LOGCAT IS THE WITNESS, not the screen. Opening the Custom Tab makes WM persist
        # a task snapshot, and on this system image that aborts system_server outright
        # (mapper.ranchu "Assertion failed: !rcEnc->featureInfo()->hasReadColorBufferDma",
        # via TaskSnapshotConvertUtil.copyToSwBitmapDirect). The app then dies WITH the
        # framework, seconds after it did exactly the right thing, and every screen-based
        # check reports a product defect that is not one. So: record what the framework
        # logged at the moment of the tap, and read the verdict out of that.
        { adbs logcat -d 2>/dev/null || true; } | tr -d '\r' > "$lg"
        local t tt; t="$(top_pkg)"; tt="$(task_of "$t")"
        adbs shell dumpsys activity activities > "$OUT_ROOT/$AVD/external-activities.txt" 2>/dev/null || true
        shot external-after-tap >/dev/null

        # 1. did the shell hand the URL out at all, and to a browser?
        if grep -qE "START u0 \{act=android.intent.action.VIEW dat=https://tabelog.com.*from uid [0-9]+ \($PKG\)" "$lg"; then
            pass "the Tabelog link left the WebView as a VIEW intent from $PKG (external-logcat.txt)"
        else
            fail "no VIEW intent for tabelog.com from $PKG after the tap (see external-logcat.txt)"
        fi
        # 2. Custom Tab shape: Chrome's own dispatcher says it is not a plain browser launch.
        if grep -q 'CustomTabsIntent#shouldAlwaysUseBrowserUI() = false' "$lg"; then
            pass "the browser received it as a Custom Tab (shouldAlwaysUseBrowserUI = false)"
        else
            skip "Custom Tab shape (this browser did not log a CustomTabsIntent decision)"
        fi
        # 3. same task = no second card in Recents: the transition's triggerTask is OUR task,
        #    with the browser activity on top of our MainActivity.
        if grep -qE "triggerTask = TaskInfo\{.*baseActivity=ComponentInfo\{$PKG/.*topActivity=ComponentInfo\{(com\.android\.chrome|${browser%%/*})/" "$lg"; then
            pass "the browser opened inside our own task — no second Recents card (§5.2)"
        else
            skip "same-task check (no transition record naming both activities in this log)"
        fi

        if grep -q 'hasReadColorBufferDma' "$lg"; then
            info "NOTE: system_server aborted on the task snapshot right after the launch" \
                 "(emulator image bug, see tools/VERIFY.md §6) — the screen-side checks below are void"
            skip "the app survived the external link (the framework itself died: image bug)"
        else
            expect "the main WebView stayed on the site" "$(page_json 'location.origin')" "$before_url"
            expect "the app did not die opening an external link" "$(app_alive)" "true"
            if [ -z "$t" ] || [ "$t" = "$PKG" ]; then
                skip "external target on screen (nothing else came to the front)"
            elif [ -n "$our_task" ] && [ "$tt" = "$our_task" ]; then
                pass "the external page is on screen in our own task ($t, task $tt)"
            else
                info "the external page opened as $t (task ${tt:-?}, ours ${our_task:-?})"
                pass "the external page left the WebView"
            fi
        fi
        adbs shell input keyevent KEYCODE_BACK >/dev/null 2>&1 || true
        sleep 2
    else
        skip "external link (could not reach a Tabelog link in the card — no probe, or an empty card)"
    fi
}

flow_share() {
    head2 "share — the card's share button raises a system chooser (§8.1)"
    local rung; rung="$(open_card "$SHARE_ID" cold || true)"
    if [ -z "$rung" ]; then skip "share (no card to share from)"; return; fi
    if [ "$(page_bool 'document.querySelector("#bs-content .rst-share")')" != "true" ]; then
        skip "share (this page build has no .rst-share button in the card)"
        return
    fi
    if tap_element '#bs-content .rst-share'; then
        sleep 3
        local w; w="$(top_window)"
        shot share-after-tap >/dev/null
        expect "the app survived the share" "$(app_alive)" "true"
        case "$w" in
            *Chooser*|*ResolverActivity*|*sharesheet*)
                pass "a system chooser is on top ($w)" ;;
            "$PKG"*)
                skip "share stayed in the page (no Native.share in this build — the page's own copy-link fallback)" ;;
            *)  info "top window after the share tap: ${w:-<none>}"
                skip "share chooser (nothing recognisable came up)" ;;
        esac
        adbs shell input keyevent KEYCODE_BACK >/dev/null 2>&1 || true
        sleep 1
    else
        skip "share (could not reach the button)"
    fi
}

flow_offline() {
    head2 "offline — a primed service worker still shows the map with both radios down (§9.1)"
    cold_start >/dev/null
    wait_page 45
    info "primed the service worker: sw=$(page_json '!!(navigator.serviceWorker&&navigator.serviceWorker.controller)')"
    sleep 5
    emu net off | sed 's/^/      /' >> "$SUMMARY"
    adbs shell am force-stop "$PKG" >/dev/null 2>&1 || true
    sleep 2
    cold_start >/dev/null
    wait_page 40
    local png; png="$(shot offline-cold)"
    assert_map_painted "$png" "offline cold start"
    local online bar
    online="$(page_json 'navigator.onLine')"
    bar="$(page_bool 'document.getElementById("net-offline")&&!document.getElementById("net-offline").hidden')"
    if [ -z "$online" ]; then
        skip "navigator.onLine offline (no probe)"
    else
        expect "the page knows it is offline" "$online" "false"
    fi
    if [ -z "$bar" ]; then
        skip "the offline bar (no probe)"
    else
        expect "the offline bar is showing" "$bar" "true"
    fi
    emu net on | sed 's/^/      /' >> "$SUMMARY"
    sleep 4
}

flow_location() {
    head2 "location — nothing at launch, a prompt when the FAB asks, and geo fix moves the map (§8.2)"
    adbs shell pm revoke "$PKG" android.permission.ACCESS_FINE_LOCATION >/dev/null 2>&1 || true
    adbs shell pm revoke "$PKG" android.permission.ACCESS_COARSE_LOCATION >/dev/null 2>&1 || true
    cold_start >/dev/null
    wait_page 40
    local w; w="$(top_window)"
    case "$w" in
        *permissioncontroller*|*GrantPermissions*)
            fail "a permission dialog came up at launch ($w) — §8.2 forbids it" ;;
        *)  pass "no permission dialog at launch (top window: ${w:-<none>})" ;;
    esac
    if ! tap_element '#fab-locate'; then
        skip "the locate FAB (could not reach it)"
        return
    fi
    sleep 4
    w="$(top_window)"
    shot location-after-fab >/dev/null
    case "$w" in
        *permissioncontroller*|*GrantPermissions*)
            pass "the locate FAB raised the system permission dialog ($w)"
            # Answer it the deterministic way: pm grant, then dismiss the dialog. Tapping
            # the button by pixel would depend on this image's dialog layout.
            adbs shell input keyevent KEYCODE_BACK >/dev/null 2>&1 || true
            sleep 1
            adbs shell pm grant "$PKG" android.permission.ACCESS_FINE_LOCATION >/dev/null 2>&1 || true
            adbs shell pm grant "$PKG" android.permission.ACCESS_COARSE_LOCATION >/dev/null 2>&1 || true
            ;;
        *)  skip "the permission dialog (nothing came up — no geolocation bridge in this build)" ;;
    esac
    adbs emu geo fix 139.7649 35.6812 >/dev/null 2>&1 || true
    sleep 6
    shot location-after-geofix >/dev/null
    local mk
    mk="$(page_bool 'document.querySelector(".leaflet-control-locate-marker,.leaflet-control-locate-circle,.lc-marker")')"
    if [ -z "$mk" ]; then skip "the located-position marker (no probe)"
    elif [ "$mk" = true ]; then pass "the page painted a located-position marker after geo fix"
    else skip "the located-position marker (no fix reached the page — expected without the bridge)"; fi
}

flow_night() {
    head2 "night mode — the page stays light and the activity is not recreated (§1.3)"
    cold_start >/dev/null
    wait_page 40
    local before after bg
    before="$(page_json 'window.__jpfmBootId||window.__jpfmProbeBoot||""')"
    adbs shell cmd uimode night yes >/dev/null 2>&1 || true
    sleep 5
    after="$(page_json 'window.__jpfmBootId||window.__jpfmProbeBoot||""')"
    bg="$(page_json 'getComputedStyle(document.body).backgroundColor')"
    shot night-yes >/dev/null
    if [ -z "$before" ] || [ "$before" = '""' ]; then
        skip "the page across the night switch (no boot id available)"
    else
        expect "the page did not reload on the night switch" "$after" "$before"
    fi
    if [ -z "$bg" ]; then
        skip "the page background under night mode (no probe)"
    else
        info "body background under night mode: $bg"
        case "$bg" in
            *"rgb(0, 0, 0)"*) fail "the page went dark under night mode" ;;
            *) pass "the page stayed light under night mode ($bg)" ;;
        esac
    fi
    adbs shell cmd uimode night no >/dev/null 2>&1 || true
    sleep 3
}

flow_fontscale() {
    head2 "font scale — the shell multiplies the system scale into textZoom (§2.4)"
    adbs shell settings put system font_scale 1.3 >/dev/null 2>&1 || true
    sleep 2
    cold_start >/dev/null
    wait_page 40
    local d="$OUT_ROOT/$AVD/fontscale-13.json"
    if diagsh capture "$d" >/dev/null 2>&1; then
        local tz; tz="$(diagsh get "$d" textZoom 2>/dev/null || true)"
        if [ -z "$tz" ]; then
            skip "textZoom at font_scale 1.3 (this build's diagnostics does not report it)"
        else
            expect "textZoom at font_scale 1.3" "$tz" "130"
        fi
        info "fontScale reported: $(diagsh get "$d" fontScale 2>/dev/null || echo '?')"
    else
        skip "textZoom at font_scale 1.3 (no diagnostics at all)"
    fi
    # Informational only. The root font-size is the PAGE's own, and WebView does not apply
    # the system font scale by itself (that is exactly what §2.4 asks the shell to do), so
    # this number only means something next to the same reading at font_scale 1.0 — on the
    # T1 skeleton it read 20.8px at both.
    local px; px="$(page_json 'getComputedStyle(document.documentElement).fontSize')"
    info "root font-size at font_scale 1.3: ${px:-<no probe>} (compare with the 1.0 reading)"
    shot fontscale-13 >/dev/null
    adbs shell settings put system font_scale 1.0 >/dev/null 2>&1 || true
    sleep 2
}

flow_signin() {
    head2 "sign-in degradation — no Google account here, so it must fail politely (§7.4)"
    cold_start >/dev/null
    wait_page 40
    if ! tap_element '#ss-avatar'; then
        skip "sign-in (could not open the account menu)"
        return
    fi
    sleep 2
    if ! tap_element '#ssm-signin-btn'; then
        skip "sign-in (no #ssm-signin-btn in this page build)"
        adbs shell input keyevent KEYCODE_BACK >/dev/null 2>&1 || true
        return
    fi
    sleep 6
    shot signin-after-tap >/dev/null
    expect "the app survived the sign-in attempt" "$(app_alive)" "true"
    local leaks; leaks="$(logcat_has_secret)"
    if [ "${leaks:-0}" -gt 0 ]; then
        fail "logcat carries something token-shaped after the sign-in attempt ($leaks lines)"
    else
        pass "no token-shaped string in logcat after the sign-in attempt"
    fi
    info "top window after the sign-in tap: $(top_window)"
    adbs shell input keyevent KEYCODE_BACK >/dev/null 2>&1 || true
}

# --------------------------------------------------------------------------------- run
detect_pkg
SUMMARY_FILE=summary-flows.txt
open_summary "$AVD" "jpfoodmap Android — behaviour acceptance (flows)"
printf '\n### %s / flows: %s ###\n' "$AVD" "${FLOWS[*]}"

emu start | sed 's/^/  /'
if [ -z "${JPFM_NO_INSTALL:-}" ]; then install_apk; else info "JPFM_NO_INSTALL — using whatever is on the device"; fi
# An explicit JPFM_DIAG_SOURCE from the caller wins — same reason as verify-geometry.sh:
# this detection runs before the app has ever been READY, so it can answer "probe" for a
# build whose JpfmDiag line is fine, and the probe half has no imeMode / pageLoads /
# activityCreates.
if [ -z "${JPFM_DIAG_SOURCE:-}" ]; then
    if JPFM_DIAG_TIMEOUT=8 diagsh native "$OUT_ROOT/$AVD/diag-source-probe.json" >/dev/null 2>&1; then
        export JPFM_DIAG_SOURCE=native
    else
        export JPFM_DIAG_SOURCE=probe
    fi
fi
info "diagnostics source: $JPFM_DIAG_SOURCE   DevTools probe: $(have_probe && echo yes || echo no)"

for f in "${FLOWS[@]}"; do
    case "$f" in
        back|deeplink|external|share|offline|location|night|fontscale|signin) "flow_$f" ;;
        *) skip "unknown flow '$f'" ;;
    esac
done

# Leave the device the way it was found: an override or a font scale left behind would
# silently poison the next task's run on the same AVD.
adbs shell settings put system font_scale 1.0 >/dev/null 2>&1 || true
adbs shell cmd uimode night no >/dev/null 2>&1 || true
emu net on >/dev/null 2>&1 || true
adbs shell wm size reset >/dev/null 2>&1 || true

finish_avd
if [ -z "${JPFM_KEEP:-}" ]; then emu stop | sed 's/^/  /'; fi
if [ "$FAIL_N" -gt 250 ]; then exit 250; fi
exit "$FAIL_N"
