package com.fredhli.jpfoodmap

import android.content.Intent

/**
 * An incoming Intent turned into one of four things to do. Kept apart from MainActivity so
 * the decision — which is all of the deep-link behaviour — can be unit-tested without an
 * emulator.
 *
 * The interesting case is [Target.Share]: `https://jpfoodmap.com/?r=<base36>` names one
 * restaurant. Cold, the URL is loaded and the page opens the card itself; hot, the page is
 * already up and reloading it would throw away the map state, so the shell asks the page to
 * switch cards through [hotShareJs] and only falls back to a load if that returns false.
 *
 * The whole decision is one expression over `(action, data, flags)`, and the plain-JVM
 * overload of [targetOf] is where it lives: an `android.content.Intent` cannot be built in
 * a unit test (the mockable android.jar throws from every method), so the Intent overload
 * is a three-line adapter and the rules are tested through the pure one.
 */
object DeepLinks {

    sealed class Target {
        /** Nothing to do — a relaunch from recents, or an Intent the shell does not own. */
        object None : Target()

        /** Load this URL in the WebView. */
        data class Load(val url: String) : Target()

        /** Open this restaurant card; [url] is the fallback if the page says no. */
        data class Share(val id: String, val url: String) : Target()

        /** Not ours: hand it to the browser and keep showing the page. */
        data class Leave(val url: String, val nav: Links.Nav) : Target()
    }

    /**
     * @param stale true when the Intent is the one this task was created with and has
     *   already been handled once (FLAG_ACTIVITY_LAUNCHED_FROM_HISTORY, or a relaunch), in
     *   which case acting on it again would yank the user off whatever they were doing.
     */
    fun targetOf(intent: Intent?, stale: Boolean, appOrigins: Set<String>): Target =
        targetOf(
            action = intent?.action,
            dataUrl = intent?.data?.toString(),
            flags = intent?.flags ?: 0,
            stale = stale,
            appOrigins = appOrigins,
        )

    /**
     * The rules, with nothing Android-shaped left in them.
     *
     * In order:
     *  1. **Stale.** A relaunch from Recents re-delivers the root Intent verbatim, and a
     *     recreated Activity gets the same Intent it already consumed. Acting on either
     *     would reopen a card the user closed twenty minutes ago, so both are [Target.None]
     *     and MainActivity is left to show whatever it was showing.
     *  2. **Not a VIEW with data.** The launcher tap (`ACTION_MAIN`), the settings shortcut,
     *     a diagnostics Intent: none of them names a URL. [Target.None]; MainActivity
     *     supplies its own home URL when it has no page yet.
     *  3. **Ours with a `?r=`.** [Target.Share] — the one case with a hot path.
     *  4. **Ours, anything else.** [Target.Load]. `?lang=en` lands here on purpose
     *     (STANDARDS §6.4): switching language IS a full navigation in the page, so a
     *     loadUrl is the correct handling rather than something cleverer.
     *  5. **Not ours.** [Target.Leave] with the classification [Links] made, so the caller
     *     can start the right thing without deciding twice. Only reachable when another
     *     app addresses this component directly — the App Links filter is host-scoped — but
     *     "another app sent us a URL" must not put a foreign page inside the shell.
     *
     * @param classify seam for the tests; production passes [Links.classify], which needs
     *   nothing from Android either but belongs to T5.
     */
    internal fun targetOf(
        action: String?,
        dataUrl: String?,
        flags: Int,
        stale: Boolean,
        appOrigins: Set<String>,
        classify: (String, Set<String>) -> Links.Nav = Links::classify,
    ): Target {
        if (stale || (flags and Intent.FLAG_ACTIVITY_LAUNCHED_FROM_HISTORY) != 0) return Target.None
        if (action != Intent.ACTION_VIEW) return Target.None
        val url = dataUrl?.trim().orEmpty()
        if (url.isEmpty()) return Target.None
        if (!Routes.isAppOrigin(url, appOrigins)) return Target.Leave(url, classify(url, appOrigins))
        val id = Routes.shareIdOf(url)
        return if (id != null) Target.Share(id, url) else Target.Load(url)
    }

    /**
     * The hot-path probe. Evaluates to `"true"` when the page opened the card and `"false"`
     * when it could not — an older page still in the service worker's shell cache has no
     * `__jpfmOpenShare` at all, and an id no longer in the corpus finds no row — and the
     * caller then falls back to `loadUrl(url)`, which is exactly the cold path.
     *
     * `!!` so the result is a bare `true`/`false` for `evaluateJavascript`'s string
     * comparison whatever the page returns; the try/catch so a page mid-boot that throws
     * out of the hook is a fallback rather than a swallowed silence.
     *
     * The id is [Routes.jsStringLiteral]-escaped even though [Routes.shareIdOf] already
     * refused anything outside `[0-9a-z]`: this string is evaluated in the page's own
     * context, and a single validation standing between a URL from another app and script
     * execution is one too few.
     */
    fun hotShareJs(id: String): String =
        "(function(id){try{return !!(window.__jpfmOpenShare&&window.__jpfmOpenShare(id));}" +
            "catch(e){return false;}})(" + Routes.jsStringLiteral(id) + ")"
}
