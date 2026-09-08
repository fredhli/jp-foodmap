#!/usr/bin/env bash
#
# The geometry acceptance: the three Fold 8 windows, both orientations of the two that have
# them, and the fold proxy — docs/STANDARDS.md §2, §3 and §15.
#
#     ./verify-geometry.sh                          # all three AVDs, the release APK
#     JPFM_APK=~/.cache/.../app-debug.apk ./verify-geometry.sh
#     ./verify-geometry.sh foldcover                # just one
#     JPFM_KEEP=1 ./verify-geometry.sh              # leave the emulator running afterwards
#
# WHAT IT ASSERTS, per window:
#   innerWidth   475 (cover) / 932 (inner landscape) / 704 (inner portrait) /
#                591 (60 % split portrait) / 689 (60 % split landscape)
#   dpr          2.625 everywhere — one density, two screens
#   imeMode      WEBVIEW (Chromium shrinks the visual viewport itself; §2.5)
#   the map is painted, not a flat grey slab, in every frame it shoots
# and once, on fold8inner, the fold proxy: cover -> inner -> cover with
#   activityCreates = 1, pageLoads unchanged, bootId unchanged, the open card still open.
#
# WHY THE ORDER IS COLD START PER ORIENTATION. `wm size` is a display override, so it
# survives a process restart; rotating first and starting second is the only way to know the
# page saw that width from its first layout rather than through a resize. The fold proxy is
# the opposite on purpose: it MUST happen to a running app, because "did anything reload"
# is the entire question.
#
# Evidence: audit_outputs/android-2026-09-06/<who>/<avd>/{summary.txt,*.png,*.json}.
# Exit code is the number of FAILs across every AVD, capped at 250; SKIPs never fail a run.

# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib-verify.sh"

AVDS=("$@")
if [ "${#AVDS[@]}" -eq 0 ]; then
    # shellcheck disable=SC2206
    AVDS=(${JPFM_AVDS:-foldcover fold8inner fold8inner60})
fi

TOTAL_FAIL=0

# orientation -> expected CSS width, per AVD. "natural" means: leave the display alone.
geometry_plan() {
    case "$1" in
        foldcover)    printf 'natural:475\n' ;;
        fold8inner)   printf 'landscape:932\nportrait:704\n' ;;
        # 688, not the 689 dp the framework reports. 1808 physical px / 2.625 = 688.76: the
        # window rounds up to 689 dp (and the page's outerWidth / screen.w read 689), while
        # the layout viewport floors to 688 CSS px. Measured 2026-09-06 on this AVD with the
        # 2.0.0 release APK — widthDp 689 and innerWidth 688 in the same diagnostics object,
        # which is Chromium being right, not the shell losing a pixel. It is the only one of
        # the five windows whose division does not land within half a pixel of an integer.
        fold8inner60) printf 'portrait:591\nlandscape:688\n' ;;
        flow)         printf 'natural:411\n' ;;
        *)            printf 'natural:0\n' ;;
    esac
}

one_orientation() {   # one_orientation <orient> <expected width>
    local orient="$1" want="$2" tag="$orient" d probe rung
    head2 "$AVD / $orient (expect innerWidth $want)"

    case "$orient" in
        natural) adbs shell wm size reset >/dev/null 2>&1 || true; sleep 3 ;;
        *)       emu rotate "$orient" | sed 's/^/      /' >> "$SUMMARY" ;;
    esac

    cold_start >/dev/null
    wait_page 40

    d="$OUT_ROOT/$AVD/$tag-home.json"
    # Two tries: right after a cold start the WebView's DevTools target can still be a
    # second away, and one miss would report the whole build as undiagnosable.
    if diagsh capture "$d" 2>>"$OUT_ROOT/$AVD/diag.log" >/dev/null \
       || { sleep 4; diagsh capture "$d" 2>>"$OUT_ROOT/$AVD/diag.log" >/dev/null; }; then
        info "diagnostics: $(diagsh get "$d" source 2>/dev/null || echo unknown) -> $(basename "$d")"
        expect "innerWidth"  "$(diagsh get "$d" innerWidth 2>/dev/null || true)" "$want"
        expect "dpr"         "$(diagsh get "$d" dpr 2>/dev/null || true)" "2.625"
        expect "imeMode"     "$(diagsh get "$d" imeMode 2>/dev/null || true)" "WEBVIEW"
        info "innerHeight $(diagsh get "$d" innerHeight 2>/dev/null || echo '?')" \
             "· env t/b $(diagsh get "$d" env.t 2>/dev/null || echo '?')/$(diagsh get "$d" env.b 2>/dev/null || echo '?')" \
             "· wbMode $(diagsh get "$d" wbMode 2>/dev/null || echo '?')" \
             "· widthDp $(diagsh get "$d" widthDp 2>/dev/null || echo '?')"
    else
        skip "$orient diagnostics (no JpfmDiag line and no DevTools probe — release APK?)"
    fi

    assert_map_painted "$(shot "$tag-home")" "$orient home frame"

    # --- the open card -------------------------------------------------------------
    rung="$(open_card "$SHARE_ID" cold || true)"
    if [ -n "$rung" ]; then
        pass "$orient card opens (via $rung)"
        shot "$tag-card" >/dev/null
        assert_map_painted "$OUT_ROOT/$AVD/$tag-card.png" "$orient card frame"
    else
        probe="$(have_probe && echo yes || echo no)"
        skip "$orient card frame (no deep-link path in this build; DevTools probe: $probe)"
        cold_start >/dev/null; wait_page 30
    fi

    # --- the filter panel ----------------------------------------------------------
    # From a clean page: the filter FAB is deliberately hidden while a card is up (M-071),
    # so the frame the card step just left behind has nothing to tap.
    # TWO ENTRANCES, ONE ACCEPTANCE (measured 2026-09-06 by gate/emulator). The FAB is the
    # phone layout's filter entrance only. From the mid/split layout up (750 CSS px and
    # wider, since 2.3.0 — fold8inner60's 591/688 now falls in phone layout too) the page
    # sets `#ff-fab{display:none}` and the entrance is the 筛选 pill in the result column
    # header — an element with CLASS wb-filter-btn, no id. Trying only #ff-fab made this
    # frame a permanent SKIP on four of the five windows, i.e. no filter evidence on exactly
    # the geometries the Fold is bought for. The `||` fallback below keeps both entrances
    # covered regardless of which tier a given AVD width lands in.
    if [ "$(card_open)" = "true" ]; then cold_start >/dev/null; wait_page 30; fi
    # AND get the first-visit language chooser out of the way (2.2.0, android-back). On a
    # freshly installed APK that modal's scrim is over the FAB — which still measures a real
    # rect, so tap_element reports a tap it made into a scrim and the panel "did not open".
    # This was FAIL on foldcover natural and fold8inner60 landscape against the pending build
    # AND against the live one; it is device state, not a product defect.
    dismiss_first_run
    if tap_element '#wb-seg [data-ux-tab="filter"]' || tap_element '.wb-filter-btn'; then
        # POLL, and re-tap once. The mid/wide pill sits in the page's top bar, a few CSS px
        # under the status bar, and a tap there is occasionally eaten by the system window
        # instead of the page — measured: the same coordinate opens the popover by hand.
        # A single sample one second later reported "expected true, got false" about a
        # perfectly working panel.
        local fo="" try i
        for try in 1 2; do
            for i in 1 2 3 4; do
                fo="$(filter_open)"; [ "$fo" = "true" ] && break
                sleep 1
            done
            [ "$fo" = "true" ] && break
            [ "$try" = 1 ] && { tap_element '#wb-seg [data-ux-tab="filter"]' || tap_element '.wb-filter-btn' || true; }
        done
        expect "$orient filter panel open" "$fo" "true"
        shot "$tag-filter" >/dev/null
        adbs shell input keyevent KEYCODE_BACK >/dev/null 2>&1 || true
        sleep 1
    else
        skip "$orient filter frame (could not reach #wb-seg or .wb-filter-btn — no probe?)"
    fi
}

fold_proxy() {
    head2 "$AVD / fold proxy  cover <-> inner  (STANDARDS §3.1-3.3)"
    local rung a b c
    rung="$(open_card "$SHARE_ID" cold || true)"
    if [ -n "$rung" ]; then info "card opened via $rung before folding"
    else info "no card could be opened first — the state half of §3.2 will SKIP"; fi

    a="$OUT_ROOT/$AVD/fold-a-inner.json"; diagsh capture "$a" >/dev/null 2>&1 || true
    local card_before; card_before="$(card_open)"

    emu fold cover | sed 's/^/      /' >> "$SUMMARY"
    sleep 1
    b="$OUT_ROOT/$AVD/fold-b-cover.json"; diagsh capture "$b" >/dev/null 2>&1 || true
    assert_map_painted "$(shot "fold-b-cover")" "folded (cover geometry) frame"
    expect "folded innerWidth" "$(diagsh get "$b" innerWidth 2>/dev/null || true)" "475"

    emu fold inner | sed 's/^/      /' >> "$SUMMARY"
    sleep 1
    c="$OUT_ROOT/$AVD/fold-c-inner.json"; diagsh capture "$c" >/dev/null 2>&1 || true
    assert_map_painted "$(shot "fold-c-inner")" "unfolded (inner geometry) frame"
    expect "unfolded innerWidth" "$(diagsh get "$c" innerWidth 2>/dev/null || true)" "932"

    # The three questions §3.1 actually asks, each read from all three files.
    local ka kb kc
    for key in bootId pageLoads activityCreates pid; do
        ka="$(diagsh get "$a" "$key" 2>/dev/null || true)"
        kb="$(diagsh get "$b" "$key" 2>/dev/null || true)"
        kc="$(diagsh get "$c" "$key" 2>/dev/null || true)"
        # "null" is what a JSON reader hands back for a key the build reports as absent
        # (bootId is null until the page's app-bridge block is deployed). Comparing three
        # nulls to each other used to print PASS, which is a verdict about nothing.
        if [ -z "$ka" ] || [ -z "$kb" ] || [ -z "$kc" ] \
           || [ "$ka" = null ] || [ "$kb" = null ] || [ "$kc" = null ]; then
            skip "$key across the fold (not reported by this build)"
        elif [ "$key" = activityCreates ]; then
            if [ "$ka" = "1" ] && [ "$kb" = "1" ] && [ "$kc" = "1" ]; then
                pass "activityCreates stayed 1 across the fold"
            else
                fail "activityCreates moved: $ka -> $kb -> $kc (the activity was recreated)"
            fi
        elif [ "$ka" = "$kb" ] && [ "$kb" = "$kc" ]; then
            pass "$key unchanged across the fold ($ka)"
        else
            fail "$key changed across the fold: $ka -> $kb -> $kc"
        fi
    done

    if [ "$card_before" = "true" ]; then
        expect "the open card survived the fold" "$(card_open)" "true"
    else
        skip "the open card across the fold (no card was open to begin with)"
    fi
}

for AVD in "${AVDS[@]}"; do
    detect_pkg
    PASS_N=0; FAIL_N=0; SKIP_N=0
    open_summary "$AVD" "jpfoodmap Android — geometry acceptance"
    printf '\n### %s ###\n' "$AVD"

    emu start | sed 's/^/  /'
    if [ -z "${JPFM_NO_INSTALL:-}" ]; then install_apk; else info "JPFM_NO_INSTALL — using whatever is on the device"; fi
    # One detection for the whole AVD: `capture` on a build with no JpfmDiag pays the full
    # logcat wait every time, and there are a dozen captures below.
    # An explicit JPFM_DIAG_SOURCE from the caller wins. The detection below runs seconds
    # after install, before the app has ever reached READY, so it answers "probe" on a build
    # that reports JpfmDiag perfectly well a minute later — and the page half has no
    # imeMode / pageLoads / activityCreates, which are exactly the keys the fold acceptance
    # needs. Overwriting the caller's choice made `JPFM_DIAG_SOURCE=native` a no-op.
    if [ -z "${JPFM_DIAG_SOURCE:-}" ]; then
        if JPFM_DIAG_TIMEOUT=8 diagsh native "$OUT_ROOT/$AVD/diag-source-probe.json" >/dev/null 2>&1; then
            export JPFM_DIAG_SOURCE=native
        else
            export JPFM_DIAG_SOURCE=probe
        fi
    fi
    info "diagnostics source: $JPFM_DIAG_SOURCE"

    # `</dev/null` on the body, not decoration: the plan is fed in on stdin, and `adb shell`
    # inside one_orientation reads stdin — it swallowed every line after the first, so
    # fold8inner never measured portrait (704) and fold8inner60 never measured landscape
    # (689). Both simply did not appear in the summary, which is the worst way to lose a
    # check.
    while IFS=: read -r orient want; do
        [ -n "$orient" ] || continue
        one_orientation "$orient" "$want" </dev/null
    done < <(geometry_plan "$AVD")

    if [ "$AVD" = fold8inner ]; then
        adbs shell wm size reset >/dev/null 2>&1 || true
        sleep 2
        fold_proxy
    fi

    adbs shell wm size reset >/dev/null 2>&1 || true
    finish_avd
    TOTAL_FAIL=$(( TOTAL_FAIL + FAIL_N ))
    if [ -z "${JPFM_KEEP:-}" ]; then emu stop | sed 's/^/  /'; fi
done

printf '\ngeometry: %d FAIL across %s — evidence under %s\n' \
    "$TOTAL_FAIL" "${AVDS[*]}" "$OUT_ROOT"
if [ "$TOTAL_FAIL" -gt 250 ]; then exit 250; fi
exit "$TOTAL_FAIL"
