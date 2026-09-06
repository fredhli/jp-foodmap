package com.fredhli.jpfoodmap

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.os.Handler
import android.os.Looper
import android.os.Message
import android.webkit.RenderProcessGoneDetail
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient

/**
 * `target="_blank"` links have no URL at the moment WebChromeClient.onCreateWindow is
 * called — the URL only arrives when the new window navigates. So a throwaway WebView is
 * handed the transport, its first navigation is intercepted, the URL goes to [onUrl], and
 * the throwaway is destroyed. Without this, every Tabelog link opened from the page's cards
 * would silently do nothing.
 *
 * Why the main WebView has `setSupportMultipleWindows(true)` at all: without it Chromium
 * either loads a `_blank` navigation in place — which would put the session-cookie origin
 * on tabelog.co.jp — or drops it. With it, every popup arrives here and is classified like
 * any other outbound link (STANDARDS §5.3).
 *
 * Two callbacks capture, because Chromium is not consistent about which one fires first
 * for a popup's initial load: `shouldOverrideUrlLoading` (script-driven `location = …` in
 * an empty popup, and most `_blank` anchors) and `onPageStarted` (a popup opened straight
 * onto its URL on some WebView versions skips the override). A per-child `captured` flag
 * makes the pair deliver exactly one URL. `about:blank` is what an empty `window.open()`
 * starts on and is never a destination.
 *
 * Destroying the child happens on the main looper via `post { }`, never inside the callback
 * that delivered the URL: Chromium is still inside that child's navigation stack when the
 * callback runs, and `destroy()` from within it is a native crash. A 30 s watchdog releases
 * a child that never navigates (a popup blocked by the page's own script, a `window.open()`
 * followed by nothing), so a stuck child cannot outlive a session.
 */
class PopupCatcher(
    private val context: Context,
    private val onUrl: (String) -> Unit,
) {

    private val main = Handler(Looper.getMainLooper())

    /** Children handed to Chromium and not yet destroyed. UI thread only. */
    private val children = mutableListOf<WebView>()

    /**
     * Create the child, attach the capturing client, hand it over through the transport
     * and return true. `resultMsg == null` -> false (nothing to hand the child to, so the
     * popup is refused, which is what returning false means to Chromium).
     */
    @SuppressLint("SetJavaScriptEnabled")
    fun onCreateWindow(parent: WebView, resultMsg: Message?): Boolean {
        if (resultMsg == null) return false
        val transport = resultMsg.obj as? WebView.WebViewTransport ?: return false

        val child = WebView(context)
        // JavaScript on, everything else default. Off, a popup that navigates via script
        // (`var w = window.open(); w.location = url`) never navigates and is never caught.
        // The child loads at most one navigation START before it is torn down, and its
        // script runs on the popup's own origin with no bridge attached.
        child.settings.javaScriptEnabled = true
        child.webViewClient = CapturingClient(child)

        children += child
        // Watchdog: a child that never reports a URL is released anyway.
        main.postDelayed({ release(child) }, WATCHDOG_MS)

        transport.webView = child
        resultMsg.sendToTarget()
        return true
    }

    /**
     * `WebChromeClient.onCloseWindow`: the popup called `window.close()`. Only our own
     * children are touched — Chromium may report a window we never created. Deferred to
     * the looper for the same reason capture is: the callback is inside the child's stack.
     */
    fun onCloseWindow(window: WebView?) {
        if (window != null && window in children) main.post { release(window) }
    }

    /** Activity teardown: destroy every child still alive and drop pending watchdogs. */
    fun destroy() {
        main.removeCallbacksAndMessages(null)
        for (child in children.toList()) release(child)
    }

    /**
     * Tear one child down. Idempotent: the capture, the watchdog and [destroy] may all ask
     * for the same child, and only the first one still finds it in [children].
     */
    private fun release(child: WebView) {
        if (!children.remove(child)) return
        // An inert client first, so nothing Chromium fires during stopLoading/destroy
        // re-enters CapturingClient on a child that is going away.
        child.webViewClient = InertClient
        child.stopLoading()
        child.destroy()
    }

    /**
     * The client a child wears while it is torn down. Not a bare `WebViewClient()`: every
     * WebView in the process shares one renderer, and when it dies Chromium calls
     * `onRenderProcessGone` on EVERY live WebView's client — the base implementation
     * returns false, and a single false anywhere kills the whole app, whatever the main
     * WebView's client answered. A child between `release()` and the end of `destroy()`
     * must therefore answer true too. There is nothing to clean up here: the child is
     * already being destroyed.
     */
    private object InertClient : WebViewClient() {
        override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean = true
    }

    /** One URL per child, from whichever callback Chromium fires first. */
    private inner class CapturingClient(private val child: WebView) : WebViewClient() {

        private var captured = false

        /**
         * The shared renderer died under this child (see [InertClient] for why a false here
         * would take the process down with it). The child is useless now — a crashed
         * WebView must be destroyed, never reused — so it is released on the spot; the main
         * WebView's own client rebuilds the page. `release` is safe from inside this
         * callback: Chromium has already torn the renderer down, so there is no navigation
         * stack to be inside of.
         */
        override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
            release(child)
            return true
        }

        private fun capture(url: String?) {
            if (captured) return
            if (url.isNullOrEmpty() || url == "about:blank") return
            captured = true
            // The URL is routed right here (loadUrl on the main WebView, or an Intent —
            // both are fine from inside a child's callback); only the destroy is deferred,
            // because that one is not.
            main.post { release(child) }
            onUrl(url)
        }

        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            capture(request.url?.toString())
            // Always true: the child never loads anything itself. Returning false for a
            // second navigation would let the child render the popup's page in a WebView
            // nobody can see, with its own cookies and network.
            return true
        }

        override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) {
            // The load that has started is stopped by release() a moment later, on the
            // looper; nothing here touches the child while Chromium is inside it.
            capture(url)
        }
    }

    companion object {
        /** A popup that never navigates is destroyed anyway; nothing may leak a WebView. */
        const val WATCHDOG_MS = 30_000L
    }
}
