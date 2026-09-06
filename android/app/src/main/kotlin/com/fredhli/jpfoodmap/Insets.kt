package com.fredhli.jpfoodmap

/**
 * Who handles the keyboard. Chromium's WebView shrinks its own visual viewport from
 * version 144, which is what the page's `env(safe-area-inset-*)` padding and its
 * bottom sheet are written against; older WebViews do not, and the shell has to pad the
 * view natively instead. Getting this backwards hides the search box behind the keyboard,
 * so it is decided from the WebView package's version name and nothing else.
 *
 * Pure on purpose: the whole rule is one integer comparison, it is the difference between
 * a usable and an unusable search box, and it is the only part of the insets policy that
 * can be pinned by a unit test off the device (InsetsTest). The version string comes from
 * `WebViewCompat.getCurrentWebViewPackage(...)?.versionName` — the WebView provider updates
 * itself through Play, so this is a runtime fact, never a build-time constant.
 */
object Insets {

    enum class ImeMode { WEBVIEW, NATIVE }

    /** First Chromium major that shrinks the visual viewport for the IME on its own. */
    const val IME_IN_WEBVIEW_FROM_MAJOR = 144

    /**
     * The leading integer of a WebView versionName ("145.0.7632.218" -> 145).
     *
     * null for anything that does not start with digits, and — because the answer is fed to
     * a comparison, not to arithmetic — also for a run of digits too long to be an Int:
     * `toIntOrNull` refusing is the right answer for "this is not a version number".
     */
    fun majorVersion(versionName: String?): Int? {
        val digits = versionName?.trim()?.takeWhile { it.isDigit() } ?: return null
        if (digits.isEmpty()) return null
        return digits.toIntOrNull()
    }

    /**
     * >= 144 -> WEBVIEW (Chromium shrinks the visual viewport itself), else NATIVE.
     *
     * Unknown means NATIVE: padding the container when the WebView would have done it too
     * leaves a gap above the keyboard, which is ugly; NOT padding when the WebView does not
     * leaves the focused input under the keyboard, which is broken. The conservative side
     * is the ugly one.
     */
    fun imeModeFor(versionName: String?): ImeMode {
        val major = majorVersion(versionName) ?: return ImeMode.NATIVE
        return if (major >= IME_IN_WEBVIEW_FROM_MAJOR) ImeMode.WEBVIEW else ImeMode.NATIVE
    }

    // ---- --app-inset-top: the shell's belt-and-braces copy of the top inset (A-1) --------
    //
    // The page pads its own fixed top bar with
    //   max(env(safe-area-inset-top, 0px), var(--app-inset-top, 0px))
    // and env() is normally right — the emulator reports env.t = 24 = the status bar with no
    // display cutout at all, so the shell's non-consuming insets policy (STANDARDS §1.2) is
    // already doing its job. This is the fallback for the case that policy cannot cover: a
    // WebView build that reports 0 for safe-area-inset-top on a device that really does draw
    // the page under a 40dp status bar, which is what Fred sees on the Fold 8's inner screen.
    // Because the page takes the MAX of the two, a correct env() and a correct variable agree
    // and nothing is padded twice.

    /** The CSS custom property the page reads. Changing this name is a page+shell change. */
    const val APP_INSET_TOP_VAR = "--app-inset-top"

    /**
     * Device pixels -> CSS px, rounded to the nearest whole pixel (Chromium reports env() at
     * that resolution too, so half a pixel of extra precision would only make the two
     * disagree). Never negative.
     *
     * A density of zero or NaN cannot happen on a real display, but it arrives from
     * `DisplayMetrics` rather than from us, and dividing by it would put `Infinity` into a
     * CSS declaration; the raw pixel count is the least wrong answer available.
     */
    fun cssPxFromPx(px: Int, density: Float): Int {
        if (px <= 0) return 0
        if (density.isNaN() || density <= 0f) return px
        return Math.round(px / density)
    }

    /**
     * The one-liner handed to `evaluateJavascript`. Wrapped in its own try/catch because it
     * runs against whatever document happens to be up — including Chromium's internal error
     * page, where touching `documentElement.style` is harmless but not worth a console error.
     *
     * The value is built from an Int, so there is nothing here for a string to escape out of.
     */
    fun appInsetTopJs(cssPx: Int): String =
        "(function(){try{document.documentElement.style.setProperty('" +
            APP_INSET_TOP_VAR + "','" + cssPx.coerceAtLeast(0) + "px')}catch(e){}})()"
}
