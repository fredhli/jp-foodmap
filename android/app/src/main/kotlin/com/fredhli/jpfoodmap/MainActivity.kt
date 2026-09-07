package com.fredhli.jpfoodmap

import android.Manifest
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.Process
import android.os.SystemClock
import android.util.Log
import android.view.HapticFeedbackConstants
import android.view.View
import android.webkit.CookieManager
import android.webkit.GeolocationPermissions
import android.webkit.WebView
import android.widget.Button
import android.widget.FrameLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.SystemBarStyle
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.core.graphics.Insets as GraphicsInsets
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.lifecycle.Lifecycle
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import kotlin.math.max
import org.json.JSONObject

/**
 * The app. One activity, one WebView, https://jpfoodmap.com/ inside it.
 *
 * Shape of the thing:
 *  - `singleTask` in the manifest, so App Links and the diagnostics Intent arrive through
 *    [onNewIntent] while the page is alive. [handleIntent] turns each into a `pendingUrl`
 *    (via [DeepLinks]) and [applyPending] decides cold (loadUrl) / warm (wait for the load
 *    in flight) / hot (ask the running page to switch cards) — docs/PLAN.md §5.6.
 *  - The WebView is created in code and can be thrown away and rebuilt (renderer crash)
 *    without touching the rest of the activity.
 *  - Insets are recorded but never consumed: the page pads itself with
 *    env(safe-area-inset-*) and the shell only pads for the keyboard on WebViews too old to
 *    do it themselves (STANDARDS §1.2, §2.5).
 *  - Nothing in the manifest's `configChanges` list recreates this activity, which is the
 *    entire fold story: closing or opening the phone reaches [onConfigurationChanged], the
 *    WebView relayouts, the page gets a `resize` event, Leaflet re-tiles — and the map
 *    position, the open restaurant card and the search box survive because the document was
 *    never reloaded (STANDARDS §3.1).
 *
 * What the shell deliberately does NOT do: read or write anything the page owns. No
 * localStorage, no favourites, no language, no map position. The page and the Worker are
 * the only owners of user data, and the repo's CLAUDE.md red lines say so.
 */
class MainActivity : ComponentActivity() {

    enum class PageState { NONE, LOADING, READY, ERROR }

    // ---- views (activity_main.xml) -------------------------------------------------------
    private lateinit var webContainer: FrameLayout
    private lateinit var errorPanel: View
    private lateinit var errorTitle: TextView
    private lateinit var errorText: TextView
    private lateinit var errorRetry: Button
    private lateinit var errorSettings: Button

    // ---- what Bridge / SiteWebView / DeepLinks read --------------------------------------
    /** The live WebView, or null before onCreate finishes / between a crash and its rebuild. */
    internal var webView: WebView? = null
        private set

    /** The shell's own three preferences. Nothing the page owns is in here. */
    internal var prefs: ShellPrefs = ShellPrefs()
        private set

    /** The origins the bridge, the link classifier and the deep-link parser are scoped to. */
    internal val appOrigins: Set<String> = Routes.appOrigins()

    internal lateinit var popupCatcher: PopupCatcher
        private set

    private val bridge = Bridge(this)

    // ---- page state machine (docs/PLAN.md §5.6) ------------------------------------------
    private var state = PageState.NONE

    /** Full URL waiting to be shown; survives until the load for it finishes. */
    private var pendingUrl: String? = null

    /**
     * The `?r=` restaurant id that came with [pendingUrl], when the target was a share link.
     * Kept beside the URL rather than parsed back out of it because it decides HOW the
     * target is applied: with an id the running page is asked to switch cards (no reload);
     * without one the URL is simply loaded.
     */
    private var pendingShareId: String? = null

    private var pendingDiagnostics = false
    private var pendingDiagnosticsLog = false

    /**
     * The diagnostics dialog while one is up. Owned here because an AlertDialog is a window
     * on this activity, and one still showing when the activity is destroyed leaks it.
     */
    private var diagnosticsDialog: AlertDialog? = null

    /** Last site URL started or visited — what Retry, a crash reload and saved state use. */
    private var lastUrl: String? = null

    /** Main-frame URL whose load failed; onPageFinished for it means "error page shown". */
    private var errorUrl: String? = null

    /**
     * URL of the last document Chromium actually committed and painted. Only [errorUrl]'s
     * companion: "this navigation put a document on screen" is half of telling an offline
     * page served by the service worker from a navigation that died on the way (see
     * [resolveRescuedPage]). Cleared by [load] so a stale value can never speak for a new
     * navigation.
     */
    private var committedUrl: String? = null

    /**
     * The URL the current load() asked for, and whether Chromium has reported a navigation
     * for it. Together they make LOADING non-terminal: a navigation that never starts (a
     * URL Chromium refuses silently) would otherwise leave the state machine in LOADING for
     * ever, with every later intent parked behind it. [loadWatchdog] is the way out.
     */
    private var loadingUrl: String? = null
    private var navigationStarted = false

    /** lastUrl from a previous incarnation (process death), applied once. */
    private var restoredUrl: String? = null

    // ---- splash ---------------------------------------------------------------------------
    private var keepSplash = true
    private val mainHandler = Handler(Looper.getMainLooper())
    private val releaseSplash = Runnable { keepSplash = false }

    /**
     * The error panel is armed by [showError] and revealed by this, one short grace period
     * later, so that a page the service worker rescues out of its own cache never has to
     * appear from behind a panel that should not have been shown (see [resolveRescuedPage]).
     * [panelPending] is "armed but not yet on screen", which [showError] needs in order to
     * keep letting the FIRST message for a document win.
     */
    private var panelPending = false
    private val revealErrorPanel = Runnable {
        panelPending = false
        errorPanel.visibility = View.VISIBLE
    }

    /**
     * Backstop for a load() Chromium never reported on. Posted by every load(), cancelled by
     * onPageStarted (the navigation exists — from there on the WebViewClient reports how it
     * ends), onPageFinished, showError and onDestroy. When it fires with the navigation
     * still unstarted, the target is given up on and the page goes back to where it was (or
     * home) — never to NONE, which would only wait for the next onResume.
     */
    private val loadWatchdog = Runnable {
        if (state != PageState.LOADING || navigationStarted) return@Runnable
        val abandoned = loadingUrl
        pendingUrl = null
        pendingShareId = null
        loadingUrl = null
        val current = webView ?: return@Runnable
        val fallback = recoveryUrl(abandoned)
        if (fallback == null) {
            // The fallback itself is what never started: nothing left to try. The panel's
            // Retry re-runs load() from lastUrl / home.
            showError(getString(R.string.shell_error_stalled), abandoned)
            return@Runnable
        }
        load(current, fallback)
    }

    // ---- insets (STANDARDS §1.2, §2.5) ----------------------------------------------------
    private var imeMode = Insets.ImeMode.NATIVE
    private var lastBars: GraphicsInsets = GraphicsInsets.NONE
    private var lastIme: GraphicsInsets = GraphicsInsets.NONE

    /**
     * Last value written into the page's `--app-inset-top`, in CSS px, or -1 for "never
     * written". Only a change is pushed, so a fold that ends with the same status bar height
     * costs nothing; a new document resets it to -1 because the variable lives on that
     * document's `documentElement` and did not survive the navigation.
     */
    private var appInsetTopCss = -1

    /**
     * Back walks the WebView's history while there is any, and on this site that history is
     * the overlay stack: every open card, filter sheet and modal pushed one state-only
     * entry (map.py M-015), so goBack() closes the topmost one. With no history the callback
     * is disabled and the system's predictive back closes the app.
     *
     * A dispatcher callback, never an onBackPressed override: overriding it turns predictive
     * back off for the whole activity at targetSdk 36 (STANDARDS §4.1).
     *
     * `canGoBack()`, NOT `copyBackForwardList().currentIndex > 0` (measured on the emulator,
     * 2026-09-07 — the numbers are in audit_outputs/2.2.0-fix/impl/android-back.md). The two
     * disagree, and only on entries the page created with no user activation: Chromium's
     * history-manipulation intervention marks the entry underneath such a pushState
     * `skip_on_back_forward_ui`, and `CanGoBack()` walks back over every skippable entry
     * before it answers. That is why a `?r=` deep link — cold or hot — leaves the card open
     * and the first BACK leaves the app, while every overlay the user opened with a finger
     * pops one layer per press. The index is NOT the better test: `canGoBackOrForward(-1)`
     * answers false in the same state and a forced `goBackOrForward(-1)` moves nothing, so
     * trusting the index would only swallow the press and strand the user. Chrome behaves
     * the same way with the same URL in a fresh tab.
     */
    private val backCallback = object : OnBackPressedCallback(false) {
        override fun handleOnBackPressed() {
            webView?.goBack()
        }
    }

    // ---- location (STANDARDS §8.2) --------------------------------------------------------
    /**
     * Callers waiting on the permission dialog. A list, not a single field: the page can ask
     * twice (the locate control retries), and dropping the second asker would leave the map
     * spinning for ever.
     */
    private val locationWaiters = mutableListOf<(Boolean) -> Unit>()

    /**
     * Registered as a field so it exists before onCreate returns, which is what
     * registerForActivityResult requires. COARSE is requested alongside FINE because a user
     * who granted only approximate location still gets a usable "near me" on this map.
     */
    private val locationLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { result ->
            val granted = result.values.any { it }
            val waiting = locationWaiters.toList()
            locationWaiters.clear()
            for (cb in waiting) cb(granted)
        }

    // =========================================================================================
    // Lifecycle
    // =========================================================================================

    override fun onCreate(savedInstanceState: Bundle?) {
        // Must precede super.onCreate: this is what swaps Theme.Jpfm.Splash for
        // postSplashScreenTheme, and the keep-on-screen condition holds the first frame
        // until the page paints.
        val splash = installSplashScreen()
        super.onCreate(savedInstanceState)
        activityCreates++
        Diagnostics.Startup.onActivityCreate(Process.getStartUptimeMillis(), SystemClock.uptimeMillis())
        splash.setKeepOnScreenCondition { keepSplash }
        mainHandler.postDelayed(releaseSplash, SPLASH_MAX_MS)

        // Transparent bars with dark icons, pinned rather than derived: enableEdgeToEdge()'s
        // default style follows the system night mode, and the site is light-only, so a
        // phone in dark mode would get white icons on a beige page (STANDARDS §1.3).
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.light(Color.TRANSPARENT, Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.light(Color.TRANSPARENT, Color.TRANSPARENT),
        )
        applyBarAppearance()

        setContentView(R.layout.activity_main)
        webContainer = findViewById(R.id.web_container)
        errorPanel = findViewById(R.id.error_panel)
        errorTitle = findViewById(R.id.error_title)
        errorText = findViewById(R.id.error_text)
        errorRetry = findViewById(R.id.error_retry)
        errorSettings = findViewById(R.id.error_settings)
        errorRetry.setOnClickListener { retry() }
        errorSettings.setOnClickListener {
            startActivity(Intent(this, AppSettingsActivity::class.java))
        }

        prefs = ShellPrefs.load(this)

        // Who owns the keyboard inset is decided once per process from the WebView provider
        // version — the provider updates itself through Play, so this is not a constant.
        imeMode = Insets.imeModeFor(WebViewCompat.getCurrentWebViewPackage(this)?.versionName)
        installInsetsListener()

        onBackPressedDispatcher.addCallback(this, backCallback)

        popupCatcher = PopupCatcher(this) { url ->
            when (val nav = Links.classify(url, appOrigins)) {
                Links.Nav.IN_APP -> webView?.loadUrl(url)
                else -> Links.leave(this, url, prefs.linkPolicy, nav)
            }
        }

        // Cookies are the session (STANDARDS §7.2). Third-party acceptance is on because
        // api.jpfoodmap.com is a different host from jpfoodmap.com and Chromium's
        // same-site judgement for a WebView is not worth betting a login on; the cookie
        // itself is HttpOnly, Secure and host-only, so this grants nothing else.
        val cookies = CookieManager.getInstance()
        cookies.setAcceptCookie(true)

        val fresh = attachFreshWebView()
        cookies.setAcceptThirdPartyCookies(fresh, true)

        // Recreated by the system (process death, or a config change outside the manifest's
        // list): the intent's extras were consumed by the previous incarnation, so the URL
        // to restore is the one it saved, not the one the launcher still carries.
        restoredUrl = savedInstanceState?.getString(STATE_LAST_URL)
        handleIntent(intent, stale = savedInstanceState != null)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent, stale = false)
    }

    override fun onResume() {
        super.onResume()
        webView?.let {
            it.onResume()
            it.resumeTimers()
        }
        // The settings screen is a separate activity, so this is where a changed preference
        // arrives. Only text zoom needs applying; the link policy is read at click time.
        val fresh = ShellPrefs.load(this)
        if (fresh != prefs) {
            val zoomChanged = fresh.textZoom != prefs.textZoom
            prefs = fresh
            if (zoomChanged) {
                webView?.let { SiteWebView.applyTextZoom(it, fresh.textZoom, resources.configuration.fontScale) }
            }
        }
        // Nothing loaded, but there is something to load: the renderer died while the
        // activity was stopped and onRendererGone parked its target here rather than
        // loading into a WebView nobody could see.
        if (state == PageState.NONE && pendingUrl != null) applyPending()
    }

    override fun onPause() {
        // Nothing in a backgrounded WebView should keep running: timers, animations and the
        // map's own rAF loop are all battery with no screen (STANDARDS §10.2). The cookie
        // flush is what makes a 90-day session survive a process kill.
        webView?.let {
            it.onPause()
            it.pauseTimers()
        }
        CookieManager.getInstance().flush()
        super.onPause()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        // Query stripped (STANDARDS §0.4). Nothing is lost by it: `?r=` names a card the
        // user has since moved on from, and `?lang=` has already been folded into the
        // page's own stored preference.
        outState.putString(STATE_LAST_URL, lastUrl?.let { Routes.stripQuery(it) })
    }

    /**
     * Fold, unfold, rotate, split-screen resize, font-scale change. The manifest lists all
     * of them under `configChanges`, so this runs INSTEAD of the activity being recreated —
     * which is the whole point: the WebView keeps its document, so the map keeps its centre
     * and zoom, the open card stays open and the search box keeps its text (STANDARDS §3).
     *
     * There is nothing to re-resolve for colours: the theme is pinned to a light one and
     * every colour the shell draws is a literal, so a system flip to dark changes nothing
     * here. Only two things move with the configuration.
     */
    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        // enableEdgeToEdge() is not re-run (it would re-derive the icon colours from the
        // night state); the flags are simply re-asserted.
        applyBarAppearance()
        webView?.let { SiteWebView.applyTextZoom(it, prefs.textZoom, newConfig.fontScale) }
    }

    override fun onDestroy() {
        mainHandler.removeCallbacks(releaseSplash)
        mainHandler.removeCallbacks(loadWatchdog)
        mainHandler.removeCallbacks(revealErrorPanel)
        // A dialog still up is a window on this activity; take it down before the activity
        // goes, or the framework logs a leaked window and keeps the view tree.
        diagnosticsDialog?.dismiss()
        diagnosticsDialog = null
        if (::popupCatcher.isInitialized) popupCatcher.destroy()
        // Cancel any sign-in continuation still waiting on Credential Manager. Its
        // completion would post to a replyProxy belonging to a WebView about to be
        // destroyed; the coroutine already checks isDestroyed, but leaving the scope alive
        // keeps the activity referenced until the provider answers.
        bridge.dispose()
        webView?.let {
            webContainer.removeView(it)
            it.destroy()
        }
        webView = null
        super.onDestroy()
    }

    /** Dark status- and navigation-bar icons, always: the site has no dark palette. */
    private fun applyBarAppearance() {
        val controller = WindowCompat.getInsetsController(window, window.decorView)
        controller.isAppearanceLightStatusBars = true
        controller.isAppearanceLightNavigationBars = true
    }

    // =========================================================================================
    // WebView creation / replacement
    // =========================================================================================

    private fun attachFreshWebView(): WebView {
        val fresh = SiteWebView.create(this)
        webContainer.addView(
            fresh,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT,
            ),
        )
        // Before the first loadUrl: addWebMessageListener only covers navigations that start
        // after it, so a bridge installed later would be missing from the first document.
        bridge.install(fresh, appOrigins, bridgeLabels())
        webView = fresh
        state = PageState.NONE
        errorUrl = null
        // A WebView is born "resumed"; the activity may not be. One created while the
        // activity is stopped (a renderer crash in the background) would otherwise run its
        // timers behind a stopped activity — and resumeTimers/pauseTimers are process-global,
        // so it would also be out of step with the next onResume.
        if (lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) {
            fresh.onResume()
            fresh.resumeTimers()
        } else {
            fresh.onPause()
            fresh.pauseTimers()
        }
        refreshBack()
        return fresh
    }

    /**
     * The `window.Native` façade's user-visible labels (docs/PLAN.md §5.4).
     *
     * Deliberately empty. [Bridge] resolves every key it is not given from its own string
     * resources — which is where the strings belong, beside the code that decides what they
     * mean — and this parameter exists only so a caller with a reason to override one can.
     * Passing labels from here would put the shell in the business of the bridge's copy.
     */
    private fun bridgeLabels(): Map<String, String> = emptyMap()

    private fun replaceWebView(): WebView {
        val old = webView
        webView = null
        if (old != null) {
            webContainer.removeView(old)
            old.destroy()
        }
        val fresh = attachFreshWebView()
        CookieManager.getInstance().setAcceptThirdPartyCookies(fresh, true)
        return fresh
    }

    /**
     * onRenderProcessGone: the crashed view is unusable; rebuild and reload where we were.
     *
     * The rebuild happens whatever the activity's state (a crashed WebView must go), but the
     * reload only when the activity is at least STARTED: Chromium kills a background
     * renderer precisely because the app is not visible, and loading straight into a fresh
     * WebView behind a stopped activity would spend the network on a page nobody sees.
     * Parked in pendingUrl instead, which onResume turns into the load.
     */
    internal fun onRendererGone(view: WebView) {
        if (view !== webView) return // a view already replaced; nothing left to do for it
        val fresh = replaceWebView()
        val target = pendingUrl ?: lastUrl ?: START_URL
        if (!lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) {
            pendingUrl = target
            return
        }
        hidePanel()
        Toast.makeText(this, R.string.shell_renderer_crashed, Toast.LENGTH_SHORT).show()
        load(fresh, target)
    }

    // =========================================================================================
    // Intents -> pendingUrl -> cold / warm / hot (docs/PLAN.md §5.6)
    // =========================================================================================

    /**
     * @param stale true when the intent's extras were already consumed by a previous
     *   incarnation (recreated with saved state). DeepLinks applies the same rule to a
     *   relaunch from Recents, which re-sends the task's root intent.
     */
    private fun handleIntent(intent: Intent?, stale: Boolean) {
        // A relaunch from Recents re-delivers the task's root Intent verbatim. DeepLinks
        // applies that rule to the URL; the diagnostics extras need it just as much, or
        // every return to the app from Recents would reopen the dialog.
        val fromHistory = stale ||
            ((intent?.flags ?: 0) and Intent.FLAG_ACTIVITY_LAUNCHED_FROM_HISTORY) != 0
        if (!fromHistory) {
            if (intent?.getBooleanExtra(EXTRA_DIAGNOSTICS, false) == true) pendingDiagnostics = true
            if (intent?.getBooleanExtra(EXTRA_DIAGNOSTICS_LOG, false) == true) pendingDiagnosticsLog = true
        }
        when (val target = DeepLinks.targetOf(intent, fromHistory, appOrigins)) {
            is DeepLinks.Target.None -> Unit
            is DeepLinks.Target.Load -> {
                pendingUrl = target.url
                pendingShareId = null
            }
            is DeepLinks.Target.Share -> {
                pendingUrl = target.url
                pendingShareId = target.id
            }
            // Not ours (an off-site URL sent straight to the component): hand it over and
            // keep showing whatever page is up.
            is DeepLinks.Target.Leave -> Links.leave(this, target.url, prefs.linkPolicy, target.nav)
        }
        if (pendingUrl == null && state == PageState.NONE) {
            // Cold start with no target: where we were last time, else the site's front
            // page — from which the page restores its own view (STANDARDS §3.4).
            pendingUrl = restoredUrl?.takeIf { Routes.isAppOrigin(it, appOrigins) } ?: START_URL
        }
        restoredUrl = null
        applyPending()
    }

    private fun applyPending() {
        val current = webView ?: return
        val target = pendingUrl
        if (target == null) {
            maybeRunDiagnostics()
            return
        }
        when (state) {
            PageState.READY -> {
                pendingUrl = null
                pushTarget(current, target)
                maybeRunDiagnostics()
            }
            // Warm: a load is in flight; onPageFinished applies this target when it lands.
            PageState.LOADING -> Unit
            PageState.NONE, PageState.ERROR -> load(current, target)
        }
    }

    /** Cold path. `pendingUrl` stays set until the load for it finishes. */
    private fun load(target: WebView, url: String) {
        hidePanel()
        errorUrl = null
        committedUrl = null
        // Whatever `?r=` the URL carries is the page's to consume on boot (map.py M-032);
        // the hot path is only for a page that is already up.
        pendingShareId = null
        state = PageState.LOADING
        loadingUrl = url
        navigationStarted = false
        mainHandler.removeCallbacks(loadWatchdog)
        mainHandler.postDelayed(loadWatchdog, LOAD_WATCHDOG_MS)
        target.loadUrl(url)
    }

    /**
     * Hot path: the page is up, so hand it the target instead of reloading it.
     *
     * A share target goes through the page's own hook, which opens the card in place and
     * keeps the map where it is; the hook answering anything but `true` (an old page from
     * the service-worker cache, an id that is not on the map) falls back to a plain load.
     * A target with no share id is a real navigation — `?lang=` is one — and the page's
     * semantics for it are exactly "load this URL".
     */
    private fun pushTarget(current: WebView, target: String) {
        val id = pendingShareId
        pendingShareId = null
        if (id != null) {
            current.evaluateJavascript(DeepLinks.hotShareJs(id)) { raw ->
                if (current !== webView) return@evaluateJavascript
                if (raw?.trim() != "true") load(current, target)
            }
            return
        }
        if (current.url != target) load(current, target)
    }

    /**
     * Where to go when a load() has to be abandoned: the last page, unless that IS the
     * abandoned URL, else the front page — and null when even that is the abandoned URL, so
     * the caller shows the panel instead of looping.
     */
    private fun recoveryUrl(abandoned: String?): String? =
        lastUrl?.takeIf { it != abandoned } ?: START_URL.takeIf { it != abandoned }

    private fun maybeRunDiagnostics() {
        if (state != PageState.READY) return
        if (pendingDiagnostics) {
            pendingDiagnostics = false
            runDiagnostics()
        }
        if (pendingDiagnosticsLog) {
            pendingDiagnosticsLog = false
            logDiagnostics()
        }
    }

    // =========================================================================================
    // WebViewClient callbacks (via SiteWebView)
    // =========================================================================================

    internal fun onPageStarted(view: WebView, url: String?) {
        if (view !== webView) return
        state = PageState.LOADING
        // The navigation exists: from here on the WebViewClient says how it ends.
        navigationStarted = true
        pageLoads++
        mainHandler.removeCallbacks(loadWatchdog)
        if (Routes.isAppOrigin(url, appOrigins)) lastUrl = url
        bridge.onPageStarted(view, url)
    }

    /** First paint of the new document: the splash may go. */
    internal fun onPageCommitVisible(view: WebView, url: String?) {
        if (view !== webView) return
        keepSplash = false
        committedUrl = url
        // The new document has its own documentElement, so whatever was written into the old
        // one is gone. First paint rather than onPageFinished so the top bar is never drawn
        // once without the padding and then again with it.
        pushAppInsetTop(force = true)
        Diagnostics.Startup.onFirstPaint(SystemClock.uptimeMillis())
    }

    internal fun onPageFinished(view: WebView, url: String?) {
        if (view !== webView) return
        mainHandler.removeCallbacks(loadWatchdog)
        val wasLoading = loadingUrl
        loadingUrl = null
        refreshBack()
        // A main-frame error and a page that came out of the service-worker cache finish
        // under the SAME url, so the url alone cannot tell them apart — see
        // [resolveRescuedPage]. Assume the worse of the two and let it correct itself.
        if (errorUrl != null && url == errorUrl) {
            state = PageState.ERROR
            resolveRescuedPage(view, url, wasLoading)
            return
        }
        finishReady(view, wasLoading)
    }

    /**
     * The tail of a load that ended with a usable page. Split out of [onPageFinished]
     * because [resolveRescuedPage] reaches it one round trip later.
     */
    private fun finishReady(view: WebView, wasLoading: String?) {
        state = PageState.READY
        keepSplash = false
        hidePanel()
        // Belt and braces for the first-paint push: a document that replaced its own
        // documentElement, or one that committed before this activity had insets, still ends
        // up with the variable set.
        pushAppInsetTop(force = true)
        // The first READY of the process is the end of the cold start, and the one moment
        // reportFullyDrawn() means anything: it tells the framework (and `am start -W`) that
        // the app is not merely drawn but usable.
        if (Diagnostics.Startup.onReady(SystemClock.uptimeMillis())) reportFullyDrawn()

        val target = pendingUrl
        if (target != null) {
            pendingUrl = null
            if (wasLoading == target) {
                // This finish IS the target's own load; the page has the URL, `?r=` and all.
                pendingShareId = null
            } else {
                // A target that arrived while something else was loading (warm): push it
                // into the page now that there is one.
                pushTarget(view, target)
            }
        }
        maybeRunDiagnostics()
    }

    /**
     * Withdraw the error panel when the site's service worker rescued the navigation.
     *
     * STANDARDS §9.1 is "once the site has been visited online, an offline cold start still
     * shows the map". That path goes: Chromium sees no network and reports
     * ERR_INTERNET_DISCONNECTED for the main frame (it only knows there is no network
     * because the app holds ACCESS_NETWORK_STATE — §10.3a), the shell raises its panel, and
     * THEN the service worker answers the same navigation out of `tabelog-shell-<build>` and
     * a perfectly good page commits underneath. Chromium's own error page finishes under the
     * failed URL too, so `url == errorUrl` describes both cases; the two have to be
     * separated by asking what is actually on screen:
     *
     *  - the navigation must have COMMITTED a document (`committedUrl`), which rules out a
     *    load that died before committing anything — a cancelled SSL handshake leaves the
     *    PREVIOUS page up, and that page must not be mistaken for a rescue; and
     *  - that document must be under the site's service worker. `navigator.serviceWorker
     *    .controller` is non-null only for a document the worker itself delivered, which is
     *    exactly the offline-cache case. Chromium's error page is an internal document with
     *    no controller, so it stays behind the panel where §9.2 wants it.
     *
     * `evaluateJavascript` answers on the next main-loop turn, so everything the answer
     * depends on is re-checked inside the callback: a newer load, a replaced WebView or a
     * different error in the meantime all mean this answer is stale and must be dropped.
     */
    private fun resolveRescuedPage(view: WebView, url: String?, wasLoading: String?) {
        if (url == null || url != committedUrl) return
        if (!Routes.isAppOrigin(url, appOrigins)) return
        view.evaluateJavascript(PAGE_RESCUED_JS) { raw ->
            if (view !== webView) return@evaluateJavascript
            if (raw?.trim() != "true") return@evaluateJavascript
            if (loadingUrl != null || state != PageState.ERROR || errorUrl != url) {
                return@evaluateJavascript
            }
            errorUrl = null
            lastUrl = url
            finishReady(view, wasLoading)
        }
    }

    /**
     * Fires for same-document navigations, which on this site means every overlay open and
     * close: this is what keeps the back callback in step with the page's own history stack.
     */
    internal fun onHistoryChanged(view: WebView, url: String?) {
        if (view !== webView) return
        refreshBack()
        if (Routes.isAppOrigin(url, appOrigins)) lastUrl = url
    }

    private fun refreshBack() {
        backCallback.isEnabled = webView?.canGoBack() == true
    }

    // =========================================================================================
    // Error panel (STANDARDS §1.7)
    // =========================================================================================

    /**
     * Overlay over the (kept) WebView. The first message for a document wins: a cancelled
     * SSL handshake is followed by a generic onReceivedError for the same URL, and "not
     * secure" is the one worth reading.
     */
    internal fun showError(text: CharSequence, failedUrl: String?) {
        val showing = errorPanel.visibility == View.VISIBLE || panelPending
        if (showing && errorUrl != null && errorUrl == failedUrl) return
        // The load ended (badly); the watchdog for it has nothing left to catch.
        mainHandler.removeCallbacks(loadWatchdog)
        loadingUrl = null
        errorUrl = failedUrl
        state = PageState.ERROR
        keepSplash = false
        errorTitle.setText(R.string.shell_error_title)
        errorText.text = text
        // Armed, not shown. A navigation the service worker is about to rescue reports its
        // main-frame error BEFORE the cached page commits (§9.1, [resolveRescuedPage]), and
        // a panel that flashes "页面打不开" for a moment over a page that then loads fine is
        // worse than a beige pause. Everything that produces a page — hidePanel() from
        // finishReady(), load() — cancels the reveal, so the panel is still seen in every
        // case where nothing arrives (§9.2), just a beat later.
        mainHandler.removeCallbacks(revealErrorPanel)
        panelPending = true
        mainHandler.postDelayed(revealErrorPanel, ERROR_PANEL_GRACE_MS)
    }

    private fun hidePanel() {
        mainHandler.removeCallbacks(revealErrorPanel)
        panelPending = false
        errorPanel.visibility = View.GONE
    }

    private fun retry() {
        val current = webView ?: return
        hidePanel()
        errorUrl = null
        load(current, pendingUrl ?: lastUrl ?: START_URL)
    }

    // =========================================================================================
    // Insets (STANDARDS §1.2, §2.5)
    // =========================================================================================

    private fun installInsetsListener() {
        ViewCompat.setOnApplyWindowInsetsListener(webContainer) { v, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout(),
            )
            val ime = insets.getInsets(WindowInsetsCompat.Type.ime())
            lastBars = bars
            lastIme = ime
            // A-1: hand the page the same top inset it should be reading out of
            // env(safe-area-inset-top). This listener is the one place that hears about a
            // fold, a rotation and a multi-window resize, so re-pushing from here covers all
            // three without a second observer.
            pushAppInsetTop()
            when (imeMode) {
                // Chromium shrinks its own visual viewport for the keyboard: pass everything
                // through untouched, bars and IME alike. The page turns the bar insets into
                // env(safe-area-inset-*) and pads itself.
                Insets.ImeMode.WEBVIEW -> {
                    v.setPadding(0, 0, 0, 0)
                    insets
                }
                // Older WebView: pad the container by the part of the keyboard that sticks
                // out above the nav bar, and hide the IME inset from the WebView so it does
                // not also try. Never CONSUMED — bars and cutout must still reach the page.
                Insets.ImeMode.NATIVE -> {
                    v.setPadding(0, 0, 0, max(0, ime.bottom - bars.bottom))
                    WindowInsetsCompat.Builder(insets)
                        .setInsets(WindowInsetsCompat.Type.ime(), GraphicsInsets.NONE)
                        .build()
                }
            }
        }
    }

    /**
     * Write the top inset into the page as `--app-inset-top` (A-1).
     *
     * The insets are NOT consumed by this — [installInsetsListener] still passes them
     * through, the page still gets `env(safe-area-inset-top)`, and the page takes the max of
     * the two, so a WebView that reports env() correctly (every one measured so far) is
     * padded exactly once. This exists for the opposite case: a build that hands the page 0
     * while the shell can see a 40dp status bar, which would leave the site's fixed top bar
     * underneath the clock.
     *
     * [lastBars] is systemBars | displayCutout — the same union env() is computed from — and
     * its top edge is the status bar on every geometry this app runs in.
     *
     * @param force write even when the value has not changed, for a document that cannot
     *   have the old value any more (a fresh navigation).
     */
    private fun pushAppInsetTop(force: Boolean = false) {
        val css = Insets.cssPxFromPx(lastBars.top, resources.displayMetrics.density)
        if (!force && css == appInsetTopCss) return
        val current = webView ?: return
        appInsetTopCss = css
        // No URL, no value, no tag: this runs on every fold and every load, and STANDARDS
        // §0.4 keeps navigation out of the log entirely.
        current.evaluateJavascript(Insets.appInsetTopJs(css), null)
    }

    // =========================================================================================
    // Location (STANDARDS §8.2)
    // =========================================================================================

    /**
     * The site asked for a position through the WebView's geolocation prompt. Answered only
     * for the site's own origin, and only after the Android runtime permission is actually
     * held — which is asked for here, on the tap, and never at launch.
     */
    internal fun onGeolocationPrompt(origin: String?, callback: GeolocationPermissions.Callback?) {
        if (callback == null) return
        // Chromium reports an origin with a trailing slash ("https://jpfoodmap.com/").
        val normalised = origin?.trimEnd('/')
        if (normalised == null || normalised !in appOrigins) {
            callback.invoke(origin ?: "", false, false)
            return
        }
        requestLocation { granted -> callback.invoke(origin, granted, false) }
    }

    /**
     * Runtime location permission, asked ONLY when the page's locate control asks for a
     * position. Calls back true if it is already granted, and never shows a dialog of its
     * own — a permanently denied permission comes straight back as false, which the page
     * shows as its own "could not locate you" state.
     */
    internal fun requestLocation(onResult: (Boolean) -> Unit) {
        if (hasLocationPermission()) {
            onResult(true)
            return
        }
        val alreadyAsking = locationWaiters.isNotEmpty()
        locationWaiters += onResult
        if (alreadyAsking) return
        locationLauncher.launch(
            arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION),
        )
    }

    private fun hasLocationPermission(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED ||
            ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_COARSE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    // =========================================================================================
    // Metrics / diagnostics
    // =========================================================================================

    /**
     * `Native.haptic()`: one confirmation tick. `View.performHapticFeedback` and nothing
     * else — it routes through the window session and needs no VIBRATE permission, which is
     * why this app still asks for exactly four.
     */
    internal fun hapticTick() {
        webView?.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
    }

    /** The bridge's `metrics` reply — keys and types exactly as docs/PLAN.md §5.4 pins them. */
    internal fun metricsJson(): JSONObject {
        val pkg = WebViewCompat.getCurrentWebViewPackage(this)
        val cfg = resources.configuration
        return JSONObject()
            .put("t", "metrics")
            .put("webview", pkg?.versionName ?: JSONObject.NULL)
            .put("webviewPackage", pkg?.packageName ?: JSONObject.NULL)
            .put("package", packageName)
            .put("version", BuildConfig.VERSION_NAME)
            .put(
                "insets",
                JSONObject()
                    .put("top", lastBars.top)
                    .put("bottom", lastBars.bottom)
                    .put("left", lastBars.left)
                    .put("right", lastBars.right),
            )
            .put("ime", lastIme.bottom)
            .put("imeMode", imeMode.name)
            .put("fontScale", cfg.fontScale.toDouble())
            .put(
                "textZoom",
                webView?.settings?.textZoom ?: ShellPrefs.effectiveTextZoom(prefs.textZoom, cfg.fontScale),
            )
            .put("density", resources.displayMetrics.density.toDouble())
            .put("widthDp", cfg.screenWidthDp)
            .put("heightDp", cfg.screenHeightDp)
            // The two numbers the fold acceptance turns on: neither may move when the phone
            // is opened or closed (STANDARDS §3.1).
            .put("pageLoads", pageLoads)
            .put("activityCreates", activityCreates)
    }

    /** The shell half of the diagnostics (docs/PLAN.md §5.8): metrics plus what only it knows. */
    internal fun diagnosticsNativeJson(): JSONObject {
        val cfg = resources.configuration
        return metricsJson()
            .put("pageState", state.name)
            // The sibling dashboard shell writes --safe-* into the page when env() comes
            // back zero; since 2.1.0 this one has the same kind of fallback for the top edge
            // alone (--app-inset-top, bug A-1), so the field finally says something: true
            // once the shell has pushed a value into the current document. appInsetTop is
            // that value in CSS px, or -1 before the first push — compare it with page.env.t,
            // which is what the page uses when the two disagree in env()'s favour.
            .put("safeVar", appInsetTopCss >= 0)
            .put("appInsetTop", appInsetTopCss)
            // Everything the back key has to work with (STANDARDS §4.1), because the
            // acceptance has to be able to go red on a RELEASE APK and there is no DevTools
            // probe there to read the page's own history with. `enabled` is the whole
            // answer: false means the next BACK leaves the app. `index`/`size` are the
            // WebView's own list, and `canGoBack` is what Chromium says AFTER skipping every
            // entry its history-manipulation intervention marked `skip_on_back_forward_ui` —
            // an entry a document created with pushState and no user activation. So
            // `index > 0` with `canGoBack` false is not a shell bug: it is a card the page
            // opened for a `?r=` deep link rather than for a finger, and Chrome does the
            // same thing with that URL in a fresh tab. Measured 2026-09-07: nothing on
            // WebView walks past such an entry — canGoBackOrForward(-1) is false as well and
            // goBackOrForward(-1) is a silent no-op — so the callback follows canGoBack.
            .put(
                "back",
                JSONObject()
                    .put("enabled", backCallback.isEnabled)
                    .put("canGoBack", webView?.canGoBack() == true)
                    .put("index", webView?.copyBackForwardList()?.currentIndex ?: -1)
                    .put("size", webView?.copyBackForwardList()?.size ?: 0),
            )
            .put(
                "webViewFeatures",
                JSONObject()
                    .put(
                        "WEB_MESSAGE_LISTENER",
                        WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER),
                    )
                    .put(
                        "DOCUMENT_START_SCRIPT",
                        WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT),
                    )
                    .put(
                        "ALGORITHMIC_DARKENING",
                        WebViewFeature.isFeatureSupported(WebViewFeature.ALGORITHMIC_DARKENING),
                    ),
            )
            .put("startup", Diagnostics.Startup.json())
            .put("exits", Diagnostics.exitsJson(this))
            .put(
                "config",
                JSONObject().put(
                    "orientation",
                    if (cfg.orientation == Configuration.ORIENTATION_LANDSCAPE) "landscape" else "portrait",
                ),
            )
    }

    /** Ask the page for its half, then hand both to [onResult]. */
    private fun collectDiagnostics(onResult: (String, JSONObject) -> Unit) {
        val native = diagnosticsNativeJson()
        val current = webView
        val js = Diagnostics.JS_METRICS
        // No page, or no page probe: the shell half on its own is still worth having — it
        // carries the insets, the WebView version and the two fold counters.
        if (current == null || js.isBlank()) {
            onResult("null", native)
            return
        }
        // Diagnostics.unquote, not a second copy here: evaluateJavascript's JSON encoding is
        // one rule, and the copy that used to live in this file lacked the leading-quote
        // guard — JSONTokener is lenient about bare words, so a page probe that failed and
        // returned `garbage{` came back truncated to `garbage` in the diagnostics.
        current.evaluateJavascript(js) { raw -> onResult(Diagnostics.unquote(raw), native) }
    }

    /** Page half + shell half, in a dialog. Diagnostics owns the presentation. */
    internal fun runDiagnostics() {
        collectDiagnostics { pageJson, native ->
            if (isFinishing || isDestroyed) return@collectDiagnostics
            showDiagnostics(Diagnostics.show(this, pageJson, native))
        }
    }

    /**
     * The same content as one line on logcat, for `emu.sh diag` (STANDARDS §12.2). Release
     * builds included: there is nothing secret in it — no cookie, no token, and the URL has
     * its query stripped.
     */
    private fun logDiagnostics() {
        collectDiagnostics { pageJson, native ->
            Log.i(Diagnostics.LOG_TAG, Diagnostics.logLine(pageJson, native))
        }
    }

    private fun showDiagnostics(dialog: AlertDialog) {
        diagnosticsDialog?.takeIf { it !== dialog }?.dismiss()
        diagnosticsDialog = dialog
        dialog.setOnDismissListener { if (diagnosticsDialog === it) diagnosticsDialog = null }
    }

    companion object {
        const val START_URL = Routes.BASE_URL

        /** Boolean extra: show the diagnostics dialog once the page is READY. */
        const val EXTRA_DIAGNOSTICS = "com.fredhli.jpfoodmap.EXTRA_DIAGNOSTICS"

        /**
         * Boolean extra: print one diagnostics line to logcat under tag JpfmDiag and do not
         * show a dialog. This is how tools/emu.sh reads geometry off a running app without a
         * human looking at a screen. Short and unqualified because it is typed by hand:
         * `am start --ez diagnostics_log true`.
         */
        const val EXTRA_DIAGNOSTICS_LOG = "diagnostics_log"

        fun diagnosticsIntent(context: Context): Intent =
            Intent(context, MainActivity::class.java).putExtra(EXTRA_DIAGNOSTICS, true)

        /** The splash never outlives this, page or no page (STANDARDS §1.5). */
        private const val SPLASH_MAX_MS = 3000L

        /**
         * A load() whose navigation has not even STARTED after this long is treated as one
         * that never will. Generous on purpose: a slow network still fires onPageStarted
         * within a second or two, because that callback marks the start of the request, not
         * the arrival of the response.
         */
        private const val LOAD_WATCHDOG_MS = 10_000L

        /**
         * How long [showError] waits before the panel is actually on screen. Long enough for
         * the service worker to answer a navigation Chromium already gave up on (measured at
         * a few hundred ms on the emulator, offline, cold), short enough that a real dead end
         * still explains itself immediately. It delays only the panel's VISIBILITY: the page
         * state is ERROR from the moment the error arrives.
         */
        private const val ERROR_PANEL_GRACE_MS = 1200L

        /**
         * "Is the document on screen one the site's service worker served?" — asked only
         * after a main-frame error, by [resolveRescuedPage], to tell an offline page out of
         * the site's cache from Chromium's built-in error page, which commits under the same
         * URL. `navigator.serviceWorker.controller` is non-null only for a document the
         * worker itself delivered; the error page is internal and has none. Wrapped in a
         * try/catch because `navigator.serviceWorker` is not exposed in every document.
         */
        private const val PAGE_RESCUED_JS =
            "(function(){try{" +
                "return !!(navigator.serviceWorker&&navigator.serviceWorker.controller);" +
                "}catch(e){return false}})()"

        /** Saved-state key: the last site URL, query stripped, restored after process death. */
        private const val STATE_LAST_URL = "lastUrl"

        /**
         * Process-scoped, not per-activity, and that is the point: they are read across a
         * fold to prove the activity was NOT recreated and the document was NOT reloaded
         * (STANDARDS §3.1). A per-activity counter would reset in exactly the case it is
         * meant to detect. Volatile because the diagnostics can be collected from a WebView
         * callback thread.
         */
        @Volatile private var activityCreates: Int = 0

        @Volatile private var pageLoads: Int = 0
    }
}
