package com.fredhli.jpfoodmap

import android.annotation.SuppressLint
import android.app.Activity
import android.graphics.Bitmap
import android.net.Uri
import android.net.http.SslError
import android.os.Message
import android.view.View
import android.view.ViewGroup
import android.webkit.GeolocationPermissions
import android.webkit.RenderProcessGoneDetail
import android.webkit.SslErrorHandler
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.ValueCallback
import android.webkit.WebViewClient
import androidx.core.content.ContextCompat
import androidx.webkit.WebSettingsCompat
import androidx.webkit.WebViewFeature
import java.io.ByteArrayInputStream

/**
 * The one WebView, and every setting on it. Kept out of MainActivity because the settings
 * are a security surface as much as a behaviour one — file access off, content access off,
 * mixed content never, windows only through PopupCatcher, algorithmic darkening off — and
 * they are easier to audit in one short file than scattered through a lifecycle.
 *
 * A factory rather than a subclass, because the view has to be thrown away and rebuilt from
 * scratch after a renderer crash: a crashed WebView must never be reused, so everything
 * that makes "our" WebView ours lives here and is applied again to the replacement. The
 * activity owns the view, the bridge and the page state machine; this file knows only how
 * to configure a WebView and how to turn its callbacks into calls on [MainActivity].
 *
 * Two settings differ from the sibling dashboard shell this is modelled on, and both are
 * deliberate:
 *  - `setGeolocationEnabled(true)`: the site's "locate me" FAB is the whole reason the app
 *    asks for a location permission at all. The gate is [ShellChromeClient]'s
 *    onGeolocationPermissionsShowPrompt, which answers only for the site's own origin and
 *    only after the runtime permission has actually been granted (STANDARDS §8.2).
 *  - [shouldInterceptRequest] answers `accounts.google.com/gsi/client` with an empty
 *    script: Google refuses GIS inside a WebView (`disallowed_useragent`), so the ~100 KB
 *    would be downloaded on every cold start only to fail. The page's own mount retries for
 *    ~3 s and then gives up quietly; sign-in goes through the native bridge instead
 *    (STANDARDS §7.3).
 */
object SiteWebView {

    /** Host + path of the Google Identity Services loader, intercepted in-app. */
    private const val GSI_HOST = "accounts.google.com"
    private const val GSI_PATH = "/gsi/client"

    /**
     * A configured, client-attached WebView with no page loaded. The caller installs the
     * bridge (it needs the allowed origins) and then calls loadUrl — in that order, because
     * addWebMessageListener only affects navigations that start after it.
     */
    @SuppressLint("SetJavaScriptEnabled")
    fun create(activity: MainActivity): WebView {
        // Chrome DevTools over USB for debug builds only: a release APK must never hand the
        // page (and its session cookie) to whatever is plugged into the phone.
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)

        val webView = WebView(activity)
        webView.layoutParams = ViewGroup.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT,
        )
        // The page runs edge-to-edge and pads itself; Android's overscroll glow on top of a
        // map that is already being dragged around reads as a rendering fault. The page's
        // own scroll containers keep their behaviour.
        webView.overScrollMode = View.OVER_SCROLL_NEVER
        // The site's own background_color, so the frame between "window shown" and "first
        // paint" is beige like the splash and the map, never a white flash (STANDARDS §1.4).
        webView.setBackgroundColor(ContextCompat.getColor(activity, R.color.shell_background))

        val s = webView.settings
        // The site is a JS app, and its favourites / filters / language live in DOM storage.
        s.javaScriptEnabled = true
        s.domStorageEnabled = true
        // window.open()/target=_blank must reach onCreateWindow (PopupCatcher) instead of
        // being loaded in place or dropped — but a page may not open windows unprompted.
        s.setSupportMultipleWindows(true)
        s.javaScriptCanOpenWindowsAutomatically = false
        // Page-level zoom is off entirely as of 3.2.3. The WebView must never scale the
        // document: a pinch that got past Leaflet used to blow up the whole layout — the
        // sidebar and the chrome along with the map — and there is no gesture that puts it
        // back reliably. Zooming the map is Leaflet's job, inside the page, and the site
        // now refuses page zoom on its side too (meta viewport + touch-action). The
        // accessibility path for "everything is too small" is the app's own text-size
        // setting, which drives textZoom and is untouched by this. displayZoomControls
        // stays false so the +/- overlay can never appear over the site's FAB stack.
        s.setSupportZoom(false)
        s.builtInZoomControls = false
        s.displayZoomControls = false
        // Respect the page's <meta viewport> (width=device-width, user-scalable=no,
        // viewport-fit=cover — the site's own half of the line above) and do
        // not zoom out to "fit": CSS px must equal dp, or the site's 480/700/1100 breakpoints
        // fire at the wrong widths on the Fold (STANDARDS §2.1).
        s.useWideViewPort = true
        s.loadWithOverviewMode = false
        // Lockdown. Everything the site needs is https and same-origin plus a handful of
        // known CDNs; nothing here should ever read a file:// or content:// URI.
        s.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
        s.allowFileAccess = false
        s.allowContentAccess = false
        s.mediaPlaybackRequiresUserGesture = true
        // The map's "locate me" control. Gated per request by the chrome client below.
        s.setGeolocationEnabled(true)
        // The site's service worker is what makes an offline cold start show a map at all
        // (STANDARDS §9.1); LOAD_DEFAULT is what lets the HTTP cache and the SW behave
        // exactly as they do in Chrome.
        s.cacheMode = WebSettings.LOAD_DEFAULT
        s.userAgentString = userAgent(activity)
        applyTextZoom(webView, activity.prefs.textZoom, activity.resources.configuration.fontScale)
        // The site declares `color-scheme: only light` and ships no dark palette; letting
        // Chromium invert it would produce a half-dark map with light labels (STANDARDS §1.3).
        if (WebViewFeature.isFeatureSupported(WebViewFeature.ALGORITHMIC_DARKENING)) {
            WebSettingsCompat.setAlgorithmicDarkeningAllowed(s, false)
        }

        webView.webViewClient = ShellWebViewClient(activity)
        webView.webChromeClient = ShellChromeClient(activity)
        return webView
    }

    /** default UA + " JpFoodMapApp/<versionName>" (docs/PLAN.md §5.4, D12). */
    fun userAgent(activity: Activity): String =
        WebSettings.getDefaultUserAgent(activity) + Bridge.UA_SUFFIX + BuildConfig.VERSION_NAME

    /**
     * WebSettings.textZoom from the stored preference and the live font scale.
     *
     * A WebView does NOT follow the system font scale on its own the way every other view
     * does — the shell has to multiply it in, or "large text" in Android settings changes
     * everything on the phone except this app (STANDARDS §2.4).
     */
    fun applyTextZoom(webView: WebView, prefTextZoom: Int, fontScale: Float) {
        webView.settings.textZoom = ShellPrefs.effectiveTextZoom(prefTextZoom, fontScale)
    }

    /**
     * Navigation policy and page-state plumbing. Every callback is forwarded to the
     * activity, which owns the state machine; the client itself decides only two things:
     * whether a navigation may happen inside the WebView at all, and which requests are
     * answered locally.
     */
    private class ShellWebViewClient(private val activity: MainActivity) : WebViewClient() {

        /**
         * IN_APP navigates; everything else is handed to Links.leave. The return is `true`
         * for every non-IN_APP case regardless of what `leave` reports: the WebView must
         * never carry the site's origin to an off-site page, and a URL `leave` could not
         * open is dropped rather than loaded (STANDARDS §5.3).
         */
        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            val url = request.url.toString()
            return when (val nav = Links.classify(url, activity.appOrigins)) {
                Links.Nav.IN_APP -> false
                else -> {
                    Links.leave(activity, url, activity.prefs.linkPolicy, nav)
                    true
                }
            }
        }

        /**
         * The one request the shell answers itself: the Google Identity Services loader.
         * See the class note. An empty 200 rather than a block, because the page's
         * `<script>` tag is in the shipped HTML and an error would only add a console line;
         * the page already tolerates `window.google` never appearing.
         *
         * Runs on a Chromium worker thread, which is why it touches nothing but its
         * argument.
         */
        override fun shouldInterceptRequest(
            view: WebView,
            request: WebResourceRequest,
        ): WebResourceResponse? {
            val u: Uri = request.url
            if (u.host == GSI_HOST && u.path == GSI_PATH) {
                return WebResourceResponse(
                    "application/javascript",
                    "utf-8",
                    ByteArrayInputStream(ByteArray(0)),
                )
            }
            return null
        }

        override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) {
            super.onPageStarted(view, url, favicon)
            activity.onPageStarted(view, url)
        }

        /**
         * First paint of the new document: the moment the splash may go. The URL is passed
         * on because "did THIS navigation commit a document at all" is what separates a
         * page the service worker rescued offline from a navigation that died before it
         * committed anything (MainActivity.onPageCommitVisible).
         */
        override fun onPageCommitVisible(view: WebView, url: String?) {
            super.onPageCommitVisible(view, url)
            activity.onPageCommitVisible(view, url)
        }

        override fun onPageFinished(view: WebView, url: String?) {
            super.onPageFinished(view, url)
            activity.onPageFinished(view, url)
        }

        /**
         * Fires for same-document navigations too — which on this site means every overlay:
         * the page pushes one state-only history entry per open card, filter sheet or
         * modal (map.py M-015), and this callback is how the back button learns there is
         * something to pop (STANDARDS §4.1).
         */
        override fun doUpdateVisitedHistory(view: WebView, url: String?, isReload: Boolean) {
            super.doUpdateVisitedHistory(view, url, isReload)
            activity.onHistoryChanged(view, url)
        }

        /** Main frame only: a failed tile or emoji PNG is the page's problem, not the shell's. */
        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            super.onReceivedError(view, request, error)
            if (!request.isForMainFrame) return
            // Chromium's own description is English and often an error code; the panel says
            // it in Chinese instead, and never shows the URL (STANDARDS §1.7).
            activity.showError(activity.getString(R.string.shell_error_offline), request.url.toString())
        }

        /**
         * 5xx on the main document only. 4xx is left to the site (its 404.html is a real
         * page), and a subresource status is never the shell's business.
         */
        override fun onReceivedHttpError(
            view: WebView,
            request: WebResourceRequest,
            errorResponse: WebResourceResponse,
        ) {
            super.onReceivedHttpError(view, request, errorResponse)
            if (!request.isForMainFrame) return
            val code = errorResponse.statusCode
            if (code < 500) return
            activity.showError(activity.getString(R.string.shell_error_http, code), request.url.toString())
        }

        /**
         * Always cancel. The Worker session cookie must never be sent over a connection
         * whose certificate does not verify, and there is deliberately no "proceed anyway".
         * The panel shows only for the site's own origin — an off-origin subresource with a
         * bad certificate is simply dropped.
         */
        override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
            handler.cancel()
            val url = error.url
            if (Routes.isAppOrigin(url, activity.appOrigins)) {
                activity.showError(activity.getString(R.string.shell_error_ssl), url)
            }
        }

        /**
         * The renderer died (crash or OOM kill). Returning false would kill the app; true
         * means "handled" — but the WebView is now unusable and must be removed from the
         * hierarchy and destroyed, which the activity does before creating a fresh one.
         */
        override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
            activity.onRendererGone(view)
            return true
        }
    }

    /** Popups, one authorized JSON picker, and the location prompt. */
    private class ShellChromeClient(private val activity: MainActivity) : WebChromeClient() {

        override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean =
            activity.jsonFiles.choose(view, callback, params)

        override fun onCreateWindow(
            view: WebView,
            isDialog: Boolean,
            isUserGesture: Boolean,
            resultMsg: Message?,
        ): Boolean = activity.popupCatcher.onCreateWindow(view, resultMsg)

        override fun onCloseWindow(window: WebView?) {
            activity.popupCatcher.onCloseWindow(window)
        }

        /**
         * The site asked for a position. Two gates, in this order: the origin must be the
         * site's own (a third-party frame must never inherit the grant), and the Android
         * runtime permission must be held — which is asked for HERE, at the moment the user
         * taps the locate FAB, and never at launch (STANDARDS §8.2).
         *
         * `retain = false`: the answer applies to this request only. Chromium would
         * otherwise remember it in its own per-origin store, which would then disagree with
         * the Android permission the user can revoke in system settings.
         */
        override fun onGeolocationPermissionsShowPrompt(
            origin: String?,
            callback: GeolocationPermissions.Callback?,
        ) {
            activity.onGeolocationPrompt(origin, callback)
        }
    }
}
