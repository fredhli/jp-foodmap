package com.fredhli.jpfoodmap

import android.content.Intent
import android.net.Uri
import android.util.Log
import android.webkit.WebView
import android.widget.Toast
import androidx.webkit.JavaScriptReplyProxy
import androidx.webkit.ScriptHandler
import androidx.webkit.WebMessageCompat
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import org.json.JSONException
import org.json.JSONObject

/**
 * The only channel between the page and the shell.
 *
 * WebViewCompat.addWebMessageListener, scoped to `https://jpfoodmap.com` — NOT
 * addJavascriptInterface, which is global, reflective and reachable from any frame the page
 * ever embeds. The façade below is injected with addDocumentStartJavaScript over the same
 * origin set, so `window.Native` exists before the page's own scripts run and the page can
 * feature-detect it instead of sniffing the user agent.
 *
 * Every message is one of a closed set (docs/PLAN.md §5.4). The page can ask for a Google
 * credential, a share sheet, an external link, the settings screen, a haptic tick, or the
 * shell's half of the diagnostics, or a one-shot JSON file picker. Favourites and bookmarks
 * stay in the page's storage; the shell only transfers the user-selected backup.
 *
 * Nothing here ever stores, logs or forwards an ID token: it goes from Credential Manager
 * straight into one replyProxy message and is never referenced again (STANDARDS §7.7).
 */
class Bridge(private val host: MainActivity) {

    sealed class Msg {
        data class SignIn(val req: String, val silent: Boolean) : Msg()
        object SignOut : Msg()
        data class Share(val title: String, val url: String) : Msg()
        data class Open(val url: String) : Msg()
        object Settings : Msg()
        object Haptic : Msg()
        object Metrics : Msg()
        data class PrepareJsonImport(val req: String) : Msg()
        data class ExportJson(val req: String, val filename: String, val text: String) : Msg()
    }

    /** Origins the listener accepts — the set [install] actually took, lowercase, unslashed. */
    private var allowedOrigins: Set<String> = emptySet()

    /** True once addWebMessageListener succeeded; without it the façade has nothing to talk to. */
    private var listenerInstalled = false

    /** True when the façade rides on addDocumentStartJavaScript; else [onPageStarted] evaluates it. */
    private var facadeAtDocumentStart = false

    private var facadeHandler: ScriptHandler? = null
    private var facadeRules: Set<String> = emptySet()
    private var labels: Map<String, String> = emptyMap()

    /**
     * Credential Manager's API is suspend-only and its UI can sit on screen for as long as
     * the user takes to pick an account, so the sign-in messages are the one asynchronous
     * thing in here. Main.immediate because every continuation ends in a replyProxy or a
     * Toast, both of which are main-thread-only.
     */
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    /**
     * Register the listener and the façade on a fresh WebView, BEFORE its first loadUrl:
     * addWebMessageListener only applies to navigations that start after the call. Both
     * calls throw IllegalArgumentException on an origin rule Chromium cannot parse, and a
     * bad rule must not take the one shipped host down with it — hence [tryRules].
     *
     * @param labels the shell's own strings for the page to render, keyed [LABEL_SIGN_IN] /
     *   [LABEL_SETTINGS] / [LABEL_BROWSER]. They are baked into the façade because a
     *   document-start script is a fixed string and cannot read shell state later. A key the
     *   caller leaves out is resolved from this app's own resources — MainActivity passes an
     *   empty map on purpose, so the strings stay with the code that defines their meaning.
     */
    fun install(webView: WebView, allowedOrigins: Set<String>, labels: Map<String, String>) {
        // A fresh WebView (the first one, or the replacement after a renderer crash) has
        // nothing registered whatever the previous one carried.
        listenerInstalled = false
        facadeAtDocumentStart = false
        facadeHandler = null
        this.labels = effectiveLabels(labels)
        this.allowedOrigins = allowedOrigins.map { it.lowercase().trimEnd('/') }.toSet()
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) return
        val rules = tryRules(allowedOrigins) {
            WebViewCompat.addWebMessageListener(webView, OBJECT_NAME, it, listener)
        } ?: return
        listenerInstalled = true
        this.allowedOrigins = rules.map { it.lowercase().trimEnd('/') }.toSet()
        facadeRules = rules
        if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
            facadeAtDocumentStart = addFacade(webView) != null
        }
    }

    /**
     * The labels actually baked into the façade. The two the page renders (the sign-in
     * button and the extra menu row) follow the SYSTEM language, not the site's language
     * switch: they are shell strings, and the page's i18n tables are a build input for the
     * website that must not grow entries only the app can see (docs/PLAN.md D13).
     */
    private fun effectiveLabels(given: Map<String, String>): Map<String, String> = mapOf(
        LABEL_SIGN_IN to (given[LABEL_SIGN_IN] ?: host.getString(R.string.signin_label)),
        LABEL_SETTINGS to (given[LABEL_SETTINGS] ?: host.getString(R.string.settings_label)),
        LABEL_BROWSER to (given[LABEL_BROWSER] ?: host.getString(R.string.open_in_browser)),
    )

    /** Register [facadeJs] at document start; the handler, or null if no rule set was taken. */
    private fun addFacade(webView: WebView): ScriptHandler? {
        var handler: ScriptHandler? = null
        val taken = tryRules(facadeRules) {
            handler = WebViewCompat.addDocumentStartJavaScript(webView, facadeJs(labels), it)
        }
        facadeHandler = if (taken == null) null else handler
        return facadeHandler
    }

    /** Run [op] with the full rule set, then with the shipped https host alone; what worked, or null. */
    private inline fun tryRules(rules: Set<String>, op: (Set<String>) -> Unit): Set<String>? {
        val https = Routes.APP_HOSTS.map { "https://$it" }.toSet()
        for (candidate in listOf(rules, https)) {
            try {
                op(candidate)
                return candidate
            } catch (_: IllegalArgumentException) {
                // fall through to the narrower set
            }
        }
        return null
    }

    /**
     * Called from WebViewClient.onPageStarted. When the WebView lacks DOCUMENT_START_SCRIPT
     * the façade is evaluated here instead — later than document start, so an inline script
     * at the very top of the page could miss it, but the page only touches window.Native
     * from initMap and from event handlers. Off-origin pages get nothing (they have no
     * NativeBridge either).
     */
    fun onPageStarted(webView: WebView, url: String?) {
        if (facadeAtDocumentStart || !listenerInstalled) return
        if (!isAllowedOrigin(url)) return
        webView.evaluateJavascript(facadeJs(labels), null)
    }

    /** Drop any in-flight sign-in continuation. Safe to call more than once. */
    fun dispose() {
        scope.cancel()
    }

    private val listener = object : WebViewCompat.WebMessageListener {
        override fun onPostMessage(
            view: WebView,
            message: WebMessageCompat,
            sourceOrigin: Uri,
            isMainFrame: Boolean,
            replyProxy: JavaScriptReplyProxy,
        ) {
            // Belt and braces over the origin rules: an iframe on an allowed origin, or an
            // origin string that does not match byte for byte, is dropped here.
            if (!isMainFrame) return
            if (sourceOrigin.toString().trimEnd('/').lowercase() !in allowedOrigins) return
            val msg = parse(message.data ?: return) ?: return
            try {
                handle(msg, replyProxy)
            } catch (t: Throwable) {
                // A message from the page must never be able to kill the shell. The type
                // is enough to find it in a bug report; the payload is not logged, because
                // some of it is a URL (STANDARDS §0.4).
                Log.w(TAG, "dropped a ${msg.javaClass.simpleName} message: ${t.javaClass.simpleName}")
            }
        }
    }

    private fun handle(msg: Msg, reply: JavaScriptReplyProxy) {
        when (msg) {
            is Msg.SignIn -> signIn(msg, reply)
            Msg.SignOut -> scope.launch { GoogleSignIn.signOut(host) }
            is Msg.Share ->
                // Only the site's own URLs. The page shares a ?r= deep link and nothing
                // else, and a share sheet is the one place a URL leaves this app without
                // the user reading it first. [isOwnUrl] is the bridge's own gate (see
                // below); Links.share re-derives the same rule from `classify` and owns the
                // chooser, so the ACTION_SEND intent exists once in the tree and the
                // "no share target at all" case reaches the same toast as the link ladder.
                if (isOwnUrl(msg.url)) Links.share(host, msg.title, msg.url, host.appOrigins)
            is Msg.Open -> {
                val url = msg.url
                if (url.startsWith("http://", true) || url.startsWith("https://", true)) {
                    // The page asked for the browser explicitly — even for its own origin,
                    // where the default ladder would resolve straight back to this app
                    // (it is the verified App Links handler) and the tap would do nothing.
                    Links.openExternal(
                        host, url, host.prefs.linkPolicy,
                        allowSelf = !Routes.isAppOrigin(url, host.appOrigins),
                    )
                } else {
                    Links.leave(host, url, host.prefs.linkPolicy, Links.classify(url, host.appOrigins))
                }
            }
            Msg.Settings ->
                // Started from the Activity with NO flags, so it stacks on this app's own
                // task and Back returns to the page.
                host.startActivity(Intent(host, AppSettingsActivity::class.java))
            Msg.Haptic -> host.hapticTick()
            Msg.Metrics -> reply.postMessage(host.metricsJson().toString())
            is Msg.PrepareJsonImport -> reply.postMessage(JSONObject().apply {
                put("t", "prepareJsonImportResult")
                put("req", msg.req)
                put("allowed", host.jsonFiles.prepareImport())
            }.toString())
            is Msg.ExportJson -> host.jsonFiles.export(msg.filename, msg.text) { status, error ->
                reply.postMessage(JSONObject().apply {
                    put("t", "exportJsonResult")
                    put("req", msg.req)
                    put("status", status)
                    if (error != null) put("error", error)
                }.toString())
            }
        }
    }

    /**
     * The one asynchronous handler. Exactly one reply goes back per request — the page keys
     * its pending callbacks by `req` and would otherwise wait out its own timeout — and the
     * token inside it is never touched again.
     */
    private fun signIn(msg: Msg.SignIn, reply: JavaScriptReplyProxy) {
        scope.launch {
            val result = GoogleSignIn.request(host, msg.silent)
            if (host.isDestroyed) return@launch
            when (result) {
                is GoogleSignIn.Result.Token -> reply.postMessage(credentialJson(msg.req, result.idToken, null))
                is GoogleSignIn.Result.Failed -> {
                    reply.postMessage(credentialJson(msg.req, null, result.error.wire))
                    // A silent attempt is the 90-day cookie renewal; it must stay invisible
                    // whether or not it worked (STANDARDS §7.5).
                    if (!msg.silent) explainSignInFailure(result.error)
                }
            }
        }
    }

    /**
     * The degraded path (STANDARDS §7.4). The page has already put its own "sign-in
     * handling failed" line in the menu; this adds the part only the shell knows — that
     * signing in on the phone needs an Android OAuth client Fred has to register, and that
     * the browser can do it today. Cancelling is a decision, not a failure, so it is quiet.
     */
    private fun explainSignInFailure(error: GoogleSignIn.Error) {
        if (error == GoogleSignIn.Error.CANCELLED) return
        Toast.makeText(host, R.string.signin_failed_hint, Toast.LENGTH_LONG).show()
        // NO_CREDENTIAL as well as NOT_CONFIGURED: "there is no Google account on this
        // device" and "this build is not registered yet" are the same dead end for the
        // user, and STANDARDS §7.4 puts both on the "sign in from the browser" path.
        if (error != GoogleSignIn.Error.NOT_CONFIGURED && error != GoogleSignIn.Error.NO_CREDENTIAL) return
        if (host.isFinishing || host.isDestroyed) return
        android.app.AlertDialog.Builder(host)
            .setTitle(R.string.signin_dialog_title)
            .setMessage(R.string.signin_not_configured)
            .setPositiveButton(R.string.open_in_browser) { _, _ -> openSiteInBrowser() }
            .setNegativeButton(android.R.string.cancel, null)
            .show()
    }

    /**
     * The escape hatch behind that dialog. [Links.openInBrowser] owns the whole ladder —
     * default browser, then any browser, then a chooser with this app struck off it — and
     * comes back false only when the device can open no URL at all. Deliberately NOT the
     * plain ladder: this app is the verified App Links handler for jpfoodmap.com, so
     * `allowSelf` would resolve straight back to the WebView the user is trying to escape.
     */
    private fun openSiteInBrowser() {
        if (Links.openInBrowser(host, Routes.BASE_URL, host.prefs.linkPolicy)) return
        Toast.makeText(host, R.string.signin_failed_hint, Toast.LENGTH_SHORT).show()
    }

    /**
     * The bridge's own origin gate, deliberately not [Routes.isAppOrigin]: this is the test
     * that decides whether a message is allowed to act at all, and it must not depend on a
     * file the shell could be built without. Origins in [allowedOrigins] are already
     * lowercase and unslashed, so a prefix test against `origin` + `/` (or the bare origin)
     * is exact — `https://jpfoodmap.com.evil.test/` does not start with either form.
     */
    private fun isAllowedOrigin(url: String?): Boolean = matchesOrigin(url, allowedOrigins)

    private fun isOwnUrl(url: String?): Boolean = matchesOrigin(url, host.appOrigins)

    private fun matchesOrigin(url: String?, origins: Set<String>): Boolean {
        val u = url?.trim()?.lowercase() ?: return false
        return origins.any { raw ->
            val o = raw.lowercase().trimEnd('/')
            o.isNotEmpty() && (u == o || u.startsWith("$o/") || u.startsWith("$o?") || u.startsWith("$o#"))
        }
    }

    companion object {
        private const val TAG = "JpfmBridge"

        const val OBJECT_NAME = "NativeBridge"

        /** Appended to the default UA so the page can recognise the shell (D12). */
        const val UA_SUFFIX = " JpFoodMapApp/"

        const val SIGN_IN_LABEL_SLOT = "__SIGN_IN_LABEL__"
        const val SETTINGS_LABEL_SLOT = "__SETTINGS_LABEL__"
        const val BROWSER_LABEL_SLOT = "__BROWSER_LABEL__"

        /** Keys of the [install] labels map; they are the façade's slots, in the same order. */
        const val LABEL_SIGN_IN = "signIn"
        const val LABEL_SETTINGS = "settings"
        const val LABEL_BROWSER = "browser"

        /** `<default UA> JpFoodMapApp/2.0.0`. Pure, so the format is unit-testable. */
        fun userAgent(defaultUa: String, versionName: String): String =
            defaultUa.trimEnd() + UA_SUFFIX + versionName

        /**
         * The `window.Native` façade, verbatim from docs/PLAN.md §5.4.
         *
         * Note what it does NOT have: a version gate. The page feature-detects every
         * function (`typeof Native.share === 'function'`), because a site deployed today
         * routinely runs inside an APK sideloaded months ago and the other way round.
         *
         * No `$` anywhere in here — this is a Kotlin raw string.
         */
        val FACADE_JS: String = """
            (function () {
              if (window.Native || !window.NativeBridge) return;
              var B = window.NativeBridge, waiting = [], files = {}, seq = 0;
              function fileRequest(t, data, timeout) {
                return new Promise(function (resolve) {
                  if (document.hidden || (navigator.userActivation && !navigator.userActivation.isActive)) {
                    resolve(t === "prepareJsonImport" ? false : {status:"error",error:"gesture"}); return;
                  }
                  var req = "file-" + (++seq);
                  data.t = t; data.req = req;
                  var timer = setTimeout(function () {
                    delete files[req];
                    resolve(t === "prepareJsonImport" ? false : {status:"error",error:"timeout"});
                  }, timeout);
                  files[req] = function (m) {
                    clearTimeout(timer);
                    resolve(t === "prepareJsonImport" ? m.allowed === true : {status:m.status,error:m.error});
                  };
                  send(data);
                });
              }
              function send(o) { try { B.postMessage(JSON.stringify(o)); } catch (e) {} }
              B.onmessage = function (ev) {
                var m; try { m = JSON.parse(ev.data); } catch (e) { return; }
                if (!m) return;
                if (m.t === "exportJsonResult" || m.t === "prepareJsonImportResult") {
                  var done = files[m.req]; delete files[m.req]; if (done) done(m); return;
                }
                if (m.t === "metrics") { var r = waiting.shift(); if (r) r(m); return; }
                if (m.t === "credential" && typeof window.__jpfmNativeCredential === "function") {
                  try { window.__jpfmNativeCredential(String(m.req || ""), m.idToken || null, m.error || null); } catch (e) {}
                }
              };
              window.Native = {
                version: "3.1.0",
                app: "jpfoodmap",
                labels: { signIn: "__SIGN_IN_LABEL__", settings: "__SETTINGS_LABEL__", browser: "__BROWSER_LABEL__" },
                signIn: function (req, silent) { send({ t: "signin", req: String(req || ""), silent: !!silent }); },
                signOut: function () { send({ t: "signout" }); },
                share: function (title, url) { send({ t: "share", title: String(title || ""), url: String(url || "") }); },
                openExternal: function (url) { send({ t: "open", url: String(url || "") }); },
                openSettings: function () { send({ t: "settings" }); },
                haptic: function () { send({ t: "haptic" }); },
                prepareJsonImport: function () { return fileRequest("prepareJsonImport", {}, 5000); },
                exportJson: function (filename, text) { return fileRequest("exportJson", {filename:filename,text:text}, 600000); },
                metrics: function () { return new Promise(function (res) { waiting.push(res); send({ t: "metrics" }); }); }
              };
            })();
        """.trimIndent()

        /**
         * The façade with the shell's strings baked in. Substituted BEFORE injection because
         * a document-start script is a fixed string that cannot read shell state later. A
         * missing key becomes an empty string, which the page reads as falsy and replaces
         * with its own fallback — that is why the slots are not sensible defaults.
         */
        fun facadeJs(labels: Map<String, String>): String =
            FACADE_JS
                .replace(SIGN_IN_LABEL_SLOT, jsEscape(labels[LABEL_SIGN_IN]))
                .replace(SETTINGS_LABEL_SLOT, jsEscape(labels[LABEL_SETTINGS]))
                .replace(BROWSER_LABEL_SLOT, jsEscape(labels[LABEL_BROWSER]))

        /**
         * Escape for the inside of a double-quoted JS string. The values are this app's own
         * string resources, so nothing hostile is expected — but a translator's apostrophe
         * or a stray backslash would otherwise be a syntax error in a script that runs
         * before the page does, i.e. a blank app.
         */
        internal fun jsEscape(raw: String?): String {
            val s = raw ?: return ""
            val out = StringBuilder(s.length + 8)
            for (c in s) {
                when (c) {
                    '\\' -> out.append("\\\\")
                    '"' -> out.append("\\\"")
                    '\n' -> out.append("\\n")
                    '\r' -> out.append("\\r")
                    '\t' -> out.append("\\t")
                    '<' -> out.append("\\u003C")   // never let a label close a <script>
                    '\u2028' -> out.append("\\u2028")   // JS line terminators, Kotlin's are not
                    '\u2029' -> out.append("\\u2029")
                    else -> out.append(c)
                }
            }
            return out.toString()
        }

        /**
         * The shell's answer to a `signin` message. JSONObject rather than string
         * concatenation: the token is opaque and a hand-built literal is one unescaped
         * character away from a syntax error the page would read as "no credential".
         * Exactly one of idToken / error is present.
         */
        fun credentialJson(req: String, idToken: String?, error: String?): String {
            val o = JSONObject()
            o.put("t", "credential")
            o.put("req", req)
            if (idToken != null) o.put("idToken", idToken) else o.put("error", error ?: "error")
            return o.toString()
        }

        /**
         * Pure (org.json is on the test classpath). Unknown "t", non-object, non-JSON, or a
         * message missing a required field → null. `signin`'s `silent` defaults to false;
         * `share`'s `title` may be empty, because the façade sends String(title || "").
         */
        fun parse(json: String): Msg? {
            if (json.length > JsonFiles.MAX_BYTES * 6 + 2048) return null
            val o = try {
                JSONObject(json)
            } catch (_: JSONException) {
                return null
            }
            return when (o.optString("t")) {
                "signin" -> Msg.SignIn(requiredString(o, "req") ?: return null, o.optBoolean("silent", false))
                "signout" -> Msg.SignOut
                "share" -> {
                    val url = requiredString(o, "url") ?: return null
                    Msg.Share(if (o.isNull("title")) "" else o.optString("title"), url)
                }
                "open" -> Msg.Open(requiredString(o, "url") ?: return null)
                "settings" -> Msg.Settings
                "haptic" -> Msg.Haptic
                "metrics" -> Msg.Metrics
                "prepareJsonImport" -> Msg.PrepareJsonImport(fileRequestId(o) ?: return null)
                "exportJson" -> Msg.ExportJson(
                    fileRequestId(o) ?: return null,
                    requiredString(o, "filename") ?: return null,
                    requiredString(o, "text") ?: return null,
                )
                else -> null
            }
        }

        private fun fileRequestId(o: JSONObject): String? = requiredString(o, "req")?.takeIf {
            it.length <= 80 && Regex("[A-Za-z0-9_-]+").matches(it)
        }

        private fun requiredString(o: JSONObject, key: String): String? {
            if (o.isNull(key)) return null
            val v = o.opt(key) as? String ?: return null
            return v.ifBlank { null }
        }
    }
}
