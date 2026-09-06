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
}
