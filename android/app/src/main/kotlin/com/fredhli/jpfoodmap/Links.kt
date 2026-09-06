package com.fredhli.jpfoodmap

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.ComponentName
import android.content.Context
import android.content.ContextWrapper
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.widget.Toast
import androidx.browser.customtabs.CustomTabsClient
import androidx.browser.customtabs.CustomTabsIntent
import java.net.URI
import java.net.URISyntaxException
import java.net.URLDecoder

/**
 * Where a URL that is not the site goes. Tabelog and Google Maps links are the whole
 * reason this exists: the user taps one, looks at it, and comes back — so the default is a
 * Custom Tab, whose back arrow returns to the app in one tap (docs/PLAN.md D7).
 *
 * Every way a URL can try to leave the page comes through here — a tap the WebView reports
 * in `shouldOverrideUrlLoading`, a `target="_blank"` caught by [PopupCatcher], the bridge's
 * `open` message, an App Link intent that turns out not to be ours — so this file, not the
 * page, is what guarantees the main WebView never loads an off-site document.
 *
 * Two halves, kept apart on purpose:
 *
 *  - The pure half ([classify], [isIntentUri], [intentFallbackUrl]) imports nothing from
 *    `android.*`, so `LinksTest` can pin the whole decision matrix on a plain JVM. Local
 *    unit tests run against the stub `android.jar` whose every method throws, which is why
 *    the classification never touches `android.net.Uri` and parses the URL by hand where
 *    `java.net.URI` is stricter than Chromium.
 *  - The Android half ([leave], [openExternal], [openInBrowser], [openIntentUri],
 *    [openOtherScheme], [share]) only dispatches a decision that has already been made.
 *
 * Logging: nothing in this file logs a URL, puts one in an exception message or shows one
 * in a toast (STANDARDS §0.4 — the rule is flat even though this site's URLs carry no
 * secret). Toasts are the fixed strings in `strings_links.xml` and nothing else.
 */
object Links {

    /** Chrome's package. Named explicitly because Samsung Internet is the Fold's default. */
    const val CHROME_PACKAGE = "com.android.chrome"

    /** What kind of destination a URL is, decided before anything is started. */
    enum class Nav {
        /** Same origin as the page: the WebView keeps it. */
        IN_APP,

        /** http(s) elsewhere: the external ladder below. */
        EXTERNAL,

        /** `intent://…#Intent;…` — resolve, or fall back to S.browser_fallback_url. */
        INTENT_URI,

        /** Some other scheme (`geo:`, `tel:`, `mailto:`): hand to the system. */
        OTHER_SCHEME,

        /** Nothing is started. */
        BLOCKED,
    }

    /**
     * The schemes a phone has a stock handler for, and an allow-list rather than "anything
     * that is not blocked": an unknown scheme handed to ACTION_VIEW is an unknown app being
     * launched with attacker-chosen data, and this site's own content never needs more than
     * these (STANDARDS §5.1). `geo:` is here because a Japanese page may well carry one.
     */
    private val OTHER_SCHEMES = setOf("mailto", "tel", "sms", "smsto", "geo", "market")

    /** RFC 3986 scheme, anchored at the start. A URL without one is unparsable here. */
    private val SCHEME = Regex("^([A-Za-z][A-Za-z0-9+.-]*):")

    /**
     * Pure. Case-insensitive scheme and host. `appOrigins` entries look like
     * `https://jpfoodmap.com` (Routes.appOrigins — lowercase, no trailing slash, an
     * explicit port only when it is not the scheme's default).
     *
     * The scheme is taken by regex first and only http(s) URLs are parsed further: an
     * `intent://` URL carries `;`-delimited fields and a `mailto:` has no authority, and
     * neither needs anything but its scheme to be classified. Null, blank or no scheme →
     * BLOCKED, which is the safe answer for a URL nobody can explain.
     *
     * Unlike the dashboard shell this classifier has no APP_DOCUMENT rung: every path on
     * jpfoodmap.com is a page the WebView can render (`privacy.html`, `404.html` included)
     * and nothing on the site serves a download.
     */
    fun classify(url: String?, appOrigins: Set<String>): Nav {
        if (url.isNullOrBlank()) return Nav.BLOCKED
        val s = url.trim()
        val scheme = SCHEME.find(s)?.groupValues?.get(1)?.lowercase() ?: return Nav.BLOCKED
        return when (scheme) {
            "http", "https" -> {
                val origin = httpOrigin(s, scheme) ?: return Nav.BLOCKED
                if (origin in appOrigins) Nav.IN_APP else Nav.EXTERNAL
            }
            "intent" -> Nav.INTENT_URI
            in OTHER_SCHEMES -> Nav.OTHER_SCHEME
            else -> Nav.BLOCKED
        }
    }

    /** Pure. True when url starts with "intent:" (case-insensitive, leading space ignored). */
    fun isIntentUri(url: String?): Boolean =
        url?.trimStart()?.startsWith("intent:", ignoreCase = true) == true

    /**
     * Pure. The `S.browser_fallback_url=…` value inside `#Intent;…;end`, percent-decoded,
     * and only when it decodes to http(s); else null.
     *
     * Mirrors what `Intent.parseUri` does with the same field (it looks for the LAST
     * `#Intent;` and reads `;`-separated `key=value` pairs up to `end`), so the fallback we
     * open is the one Chrome would have opened. Decoding is percent-only: `Uri.decode`
     * leaves '+' alone, and a fallback URL with a literal '+' in its query has to survive
     * the trip. A fallback that decodes to anything but http(s) — a `javascript:`, a second
     * `intent:` — is dropped: the point of the fallback is "a web page instead", not "any
     * URL the link's author likes".
     */
    fun intentFallbackUrl(url: String): String? {
        val start = url.lastIndexOf("#Intent;")
        if (start < 0) return null
        val body = url.substring(start + "#Intent;".length)
        val end = body.indexOf(";end")
        val fields = (if (end < 0) body else body.substring(0, end)).split(';')
        val raw = fields.firstOrNull { it.startsWith("S.browser_fallback_url=") }
            ?.substringAfter('=')
            ?.takeIf { it.isNotBlank() } ?: return null
        val decoded = percentDecode(raw) ?: return null
        val scheme = SCHEME.find(decoded)?.groupValues?.get(1)?.lowercase()
        return if (scheme == "http" || scheme == "https") decoded else null
    }

    // ---------------------------------------------------------------- pure helpers

    /**
     * `scheme://host[:port]` for an http(s) URL, or null when even a hand parse cannot find
     * a host.
     *
     * `java.net.URI` is the first attempt because it gets IPv6 literals and percent-escapes
     * right; but it is stricter than Chromium (a raw `|` or `^` in a query is a
     * URISyntaxException there and an ordinary link here), and for some hosts it parses and
     * still reports `host == null` (an underscore in a label). Either way the URL is a real
     * navigation that has to be classified, so the fallback takes the authority by hand:
     * everything between `://` and the first of `/?#\`, userinfo dropped at the LAST '@' —
     * which is exactly the trick `https://jpfoodmap.com@evil.com/` relies on, and why the
     * split is at the last one and not the first.
     *
     * The backslash is in that terminator set on purpose. Chromium parses http(s) URLs per
     * the WHATWG URL Standard, where `\` in a special-scheme URL is a path separator: it
     * navigates `https://evil.com\@jpfoodmap.com/` to evil.com. `java.net.URI` rejects the
     * backslash (which is how such a URL reaches this branch at all), and a hand parse that
     * stopped only at `/?#` would take `evil.com\` for userinfo and call the URL IN_APP —
     * after which the WebView, carrying the site's session cookie, would be on evil.com.
     */
    private fun httpOrigin(url: String, scheme: String): String? {
        try {
            val u = URI(url)
            val host = u.host
            if (!host.isNullOrEmpty()) return origin(scheme, host.lowercase(), u.port)
        } catch (_: URISyntaxException) {
            // fall through to the manual parse
        }
        val afterScheme = url.substring(scheme.length + 1)
        if (!afterScheme.startsWith("//")) return null
        val rest = afterScheme.substring(2)
        val authorityEnd = rest.indexOfAny(charArrayOf('/', '?', '#', '\\'))
            .let { if (it < 0) rest.length else it }
        val authority = rest.substring(0, authorityEnd).substringAfterLast('@')
        if (authority.isEmpty()) return null
        val host: String
        val portText: String
        if (authority.startsWith("[")) {
            val close = authority.indexOf(']')
            if (close < 0) return null
            host = authority.substring(0, close + 1)
            portText = authority.substring(close + 1).removePrefix(":")
        } else {
            host = authority.substringBefore(':')
            portText = authority.substringAfter(':', "")
        }
        if (host.isEmpty()) return null
        val port = if (portText.isEmpty()) -1 else (portText.toIntOrNull() ?: return null)
        return origin(scheme, host.lowercase(), port)
    }

    /** Same shape as Routes.originOf: the scheme's default port is dropped, any other kept. */
    private fun origin(scheme: String, host: String, port: Int): String {
        val default = if (scheme == "https") 443 else 80
        return if (port <= 0 || port == default) "$scheme://$host" else "$scheme://$host:$port"
    }

    /** Percent-decoding that leaves '+' alone (Uri.decode semantics, not form semantics). */
    private fun percentDecode(s: String): String? = try {
        URLDecoder.decode(s.replace("+", "%2B"), "UTF-8")
    } catch (_: IllegalArgumentException) {
        null
    }

    // ---------------------------------------------------------------- android half

    /**
     * Dispatch on nav. IN_APP is the caller's business (the WebView keeps the navigation)
     * and returns false here. Every other branch returns true — something was started, or
     * the URL was dropped or toasted — because a `true` out of `shouldOverrideUrlLoading` is
     * what keeps the main WebView on its own page.
     *
     * ActivityNotFoundException / SecurityException from any branch → the fixed
     * `links_no_handler` toast. A SecurityException is what a target that is
     * `exported=false`, or that demands a permission this app does not hold, throws instead
     * of "not found".
     */
    fun leave(context: Context, url: String, policy: LinkPolicy, nav: Nav): Boolean {
        try {
            when (nav) {
                Nav.IN_APP -> return false
                Nav.EXTERNAL -> if (!openExternal(context, url, policy)) noHandler(context)
                Nav.INTENT_URI -> if (!openIntentUri(context, url, policy)) noHandler(context)
                Nav.OTHER_SCHEME -> openOtherScheme(context, url) // toasts on its own
                Nav.BLOCKED -> Unit // dropped silently: nothing a user could act on
            }
        } catch (_: ActivityNotFoundException) {
            noHandler(context)
        } catch (_: SecurityException) {
            noHandler(context)
        }
        return true
    }

    /**
     * The external ladder (STANDARDS §5.2). http(s) only. In order:
     *
     *  1. ACTION_VIEW + FLAG_ACTIVITY_REQUIRE_NON_BROWSER — a verified native app for that
     *     link wins, which is the rung that makes a Google Maps link open the Maps app and
     *     a Tabelog link open Tabelog's app when it is installed. The flag makes Android
     *     throw ActivityNotFoundException rather than fall back to a browser, and that
     *     throw is the signal that there is no such app. Skipped when [allowSelf] is false,
     *     see below.
     *  2. The user's policy — CUSTOM_TAB (the default): a CustomTabsIntent pinned to Chrome
     *     when Chrome is installed and enabled, else to whatever CustomTabsClient names.
     *     CHROME: ACTION_VIEW aimed at [CHROME_PACKAGE]. SYSTEM: nothing, step 3 is the
     *     whole policy.
     *  3. A plain ACTION_VIEW — or, when [allowSelf] is false, [openBrowserOnly].
     *
     * [allowSelf]: this app is the verified App Links handler for jpfoodmap.com, so for a
     * URL on the site step 1 resolves to… MainActivity, and so does the plain ACTION_VIEW
     * in step 3. That is right for the WebView's own off-site taps (the caller only asks
     * about URLs that are not ours). It is wrong for the callers whose entire point is
     * "leave this app": the settings screen's "open in browser" row and the sign-in failure
     * hint (STANDARDS §7.4), both of which exist to get the user to the site in a real
     * browser where Google's OAuth is allowed. Those pass `allowSelf = false` and must
     * never re-enter the shell — a `singleTask` MainActivity would otherwise merely receive
     * an onNewIntent, which from the user's side is a tap that did nothing.
     *
     * Flags: every intent started from a non-Activity context carries FLAG_ACTIVITY_NEW_TASK,
     * because without it the framework throws AndroidRuntimeException (which [leave] does
     * not catch). The Custom Tab is the one case where the flag is deliberately NOT added
     * from an Activity — see the branch. Returns false only when nothing could open it.
     */
    fun openExternal(
        context: Context,
        url: String,
        policy: LinkPolicy,
        allowSelf: Boolean = true,
    ): Boolean {
        val uri = Uri.parse(url)
        if (allowSelf) {
            val nonBrowser = Intent(Intent.ACTION_VIEW, uri)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REQUIRE_NON_BROWSER)
            if (tryStart(context, nonBrowser)) return true
        }

        when (policy) {
            LinkPolicy.CUSTOM_TAB -> {
                val provider = if (isPackageEnabled(context, CHROME_PACKAGE)) CHROME_PACKAGE
                else runCatching { CustomTabsClient.getPackageName(context, null) }.getOrNull()
                if (provider != null) {
                    val tab = CustomTabsIntent.Builder().build()
                    tab.intent.setPackage(provider)
                    // A Custom Tab is meant to live in OUR task: Chrome's CustomTabActivity
                    // declares taskAffinity="" precisely so it stacks on the caller and the
                    // back arrow returns to the page with no second card in Recents — which
                    // is the whole reason it is the default here (D7). Adding NEW_TASK from
                    // an Activity would make Android spawn a separate task for it: a second
                    // "Japan Foodmap" card in Recents, and a tab that outlives the app being
                    // swiped away. The flag is added only when the caller is not an Activity
                    // at all (a WebView callback wrapped in an application context), where
                    // it is mandatory or startActivity throws AndroidRuntimeException.
                    if (activityOf(context) == null) tab.intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    try {
                        tab.launchUrl(context, uri)
                        return true
                    } catch (_: ActivityNotFoundException) {
                        // named but gone or disabled between the query and now
                    } catch (_: SecurityException) {
                        // a provider that refuses us; the plain browser below will not
                    }
                }
            }
            LinkPolicy.CHROME -> {
                val chrome = Intent(Intent.ACTION_VIEW, uri)
                    .setPackage(CHROME_PACKAGE)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                if (tryStart(context, chrome)) return true
            }
            LinkPolicy.SYSTEM -> Unit // step 3 is the whole policy
        }

        if (!allowSelf) return openBrowserOnly(context, uri)
        val plain = Intent(Intent.ACTION_VIEW, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return tryStart(context, plain)
    }

    /**
     * The "open in browser" escape hatch: the settings row, and the hint the shell shows
     * when Credential Manager cannot sign in because the Android OAuth client has not been
     * created yet (STANDARDS §7.4). [openExternal] with `allowSelf = false` — the ladder
     * without the two rungs that can resolve back to MainActivity.
     */
    fun openInBrowser(context: Context, url: String, policy: LinkPolicy): Boolean =
        openExternal(context, url, policy, allowSelf = false)

    /**
     * ACTION_VIEW on [uri] aimed at a browser package, resolved WITHOUT the URL's host so
     * that App Links verification — which is what makes this app the handler for
     * jpfoodmap.com — cannot take part. The probe is `ACTION_VIEW https:` + BROWSABLE, a
     * bare scheme with no host: browsers declare `<data android:scheme="https"/>` with no
     * host and match it; this app's own filter names its host and does not. In order:
     *
     *  1. The user's default browser (`resolveActivity` + MATCH_DEFAULT_ONLY). With no
     *     default the framework answers with its own resolver activity (package "android"),
     *     which is not a browser and is skipped.
     *  2. Every browser that answers the probe, Chrome first when it is among them.
     *  3. A system chooser with MainActivity struck off it (EXTRA_EXCLUDE_COMPONENTS).
     *
     * Visibility: the manifest's `<queries>` already declares ACTION_VIEW + BROWSABLE +
     * https, which is what lets `queryIntentActivities` see browsers at all on API 30+.
     * CATEGORY_BROWSABLE on the real intent would not do on its own — this app's own filter
     * carries BROWSABLE too, so it would still be a candidate. The package has to be pinned.
     */
    private fun openBrowserOnly(context: Context, uri: Uri): Boolean {
        val pm = context.packageManager
        val probe = Intent(Intent.ACTION_VIEW, Uri.fromParts(uri.scheme ?: "https", "", null))
            .addCategory(Intent.CATEGORY_BROWSABLE)
        val self = context.packageName
        val candidates = LinkedHashSet<String>()
        runCatching { pm.resolveActivity(probe, PackageManager.MATCH_DEFAULT_ONLY) }.getOrNull()
            ?.activityInfo?.packageName
            ?.takeIf { it != "android" && it != self }
            ?.let { candidates.add(it) }
        val all = runCatching { pm.queryIntentActivities(probe, PackageManager.MATCH_DEFAULT_ONLY) }
            .getOrNull().orEmpty()
            .mapNotNull { it.activityInfo?.packageName }
            .filter { it != self }
        if (CHROME_PACKAGE in all) candidates.add(CHROME_PACKAGE)
        candidates.addAll(all)

        for (pkg in candidates) {
            val pinned = Intent(Intent.ACTION_VIEW, uri)
                .setPackage(pkg)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            if (tryStart(context, pinned)) return true
        }

        val chooser = Intent.createChooser(Intent(Intent.ACTION_VIEW, uri), null)
            .putExtra(
                Intent.EXTRA_EXCLUDE_COMPONENTS,
                arrayOf(ComponentName(context, MainActivity::class.java)),
            )
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return tryStart(context, chooser)
    }

    /**
     * `Intent.parseUri(url, URI_INTENT_SCHEME)`, hardened before it is started: BROWSABLE
     * added (only activities that opted into being launched from web content), explicit
     * component and selector cleared (an intent URI may name any activity in any package —
     * the URL's `package=` hint survives as `setPackage`, which is the intended routing),
     * flags reduced to FLAG_ACTIVITY_NEW_TASK (a `launchFlags=` field could otherwise ask
     * for CLEAR_TASK or for grant-URI flags), and the `browser_fallback_url` extra removed
     * so the target does not receive it as data.
     *
     * The start is attempted rather than pre-checked with `resolveActivity`: under package
     * visibility (API 30+) a custom-scheme target the manifest's `<queries>` does not name
     * resolves to null even when the app is installed, while `startActivity` is not subject
     * to visibility filtering and throws ActivityNotFoundException when nothing matches —
     * the same signal without the false negative. On failure the URL's
     * `S.browser_fallback_url` (http(s) only) goes through [openExternal] per policy, which
     * is how an "open in the app, else the website" link behaves in Chrome.
     */
    fun openIntentUri(context: Context, url: String, policy: LinkPolicy): Boolean {
        val intent = try {
            Intent.parseUri(url, Intent.URI_INTENT_SCHEME)
        } catch (_: URISyntaxException) {
            null
        }
        if (intent != null) {
            intent.addCategory(Intent.CATEGORY_BROWSABLE)
            intent.component = null
            intent.selector = null
            intent.flags = Intent.FLAG_ACTIVITY_NEW_TASK
            intent.removeExtra("browser_fallback_url")
            if (tryStart(context, intent)) return true
        }
        return intentFallbackUrl(url)?.let { openExternal(context, it, policy) } ?: false
    }

    /**
     * ACTION_VIEW on the raw scheme URL + NEW_TASK — the dialer, the mail app, Maps, Play.
     * Toasts `links_no_handler` itself when nothing claims the scheme (a tablet with no
     * telephony meeting a `tel:` link) and answers whether something started.
     */
    fun openOtherScheme(context: Context, url: String): Boolean {
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val started = tryStart(context, intent)
        if (!started) noHandler(context)
        return started
    }

    /**
     * The share sheet behind the page's `Native.share(title, url)` (STANDARDS §8.1): a
     * WebView has no `navigator.share`, so the page's own share button routes here and the
     * user gets the system chooser instead of the "copy link" fallback.
     *
     * The URL is checked against [appOrigins] first and a foreign one is dropped. The page
     * only ever shares its own `?r=` deep links, and a bridge that would hand any URL at
     * all to ACTION_SEND is a bridge that can be talked into sharing something else by any
     * script that reaches `window.Native`.
     *
     * Lives here rather than in Bridge/MainActivity because the classification rule it
     * needs is this file's, and because "leave the app with this URL" is what this file is.
     * Returns false when the URL was rejected or nothing could handle ACTION_SEND.
     */
    fun share(context: Context, title: String, url: String, appOrigins: Set<String>): Boolean {
        if (classify(url, appOrigins) != Nav.IN_APP) return false
        val send = Intent(Intent.ACTION_SEND)
            .setType("text/plain")
            .putExtra(Intent.EXTRA_TEXT, url)
        if (title.isNotBlank()) send.putExtra(Intent.EXTRA_SUBJECT, title)
        val chooser = Intent.createChooser(send, null)
        // From an Activity the chooser belongs on our task; from anything else NEW_TASK is
        // mandatory. Same rule, and the same reason, as the Custom Tab branch above.
        if (activityOf(context) == null) chooser.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val started = tryStart(context, chooser)
        if (!started) noHandler(context)
        return started
    }

    /** startActivity that answers "did it start" instead of throwing. */
    private fun tryStart(context: Context, intent: Intent): Boolean = try {
        context.startActivity(intent)
        true
    } catch (_: ActivityNotFoundException) {
        false
    } catch (_: SecurityException) {
        false
    }

    /**
     * Installed AND enabled. A disabled Chrome still answers `getPackageInfo`, but every
     * intent aimed at it throws, so the enabled bit is the one that matters. Relies on the
     * manifest's `<queries><package android:name="com.android.chrome"/>` for visibility.
     */
    private fun isPackageEnabled(context: Context, packageName: String): Boolean = try {
        context.packageManager.getApplicationInfo(packageName, 0).enabled
    } catch (_: PackageManager.NameNotFoundException) {
        false
    }

    /**
     * The Activity behind a Context, unwrapping ContextWrappers (a ContextThemeWrapper, the
     * context a WebView hands its callbacks), or null for an application context.
     * `context is Activity` alone is not enough — a wrapper around an activity is still
     * "from an Activity" as far as startActivity's NEW_TASK requirement is concerned.
     */
    private tailrec fun activityOf(context: Context?): Activity? = when (context) {
        is Activity -> context
        is ContextWrapper -> activityOf(context.baseContext)
        else -> null
    }

    private fun noHandler(context: Context) = toastOnMain(context, R.string.links_no_handler)

    /**
     * A toast from any thread, with a fixed resource string and never a URL. The bridge's
     * callbacks and the WebView's are not guaranteed to be on the UI thread, and
     * `Toast.makeText` off the main looper throws on some OEM builds ("Can't create handler
     * inside thread that has not called Looper.prepare()"). Application context, so a
     * finishing Activity is not what the toast holds on to.
     */
    private fun toastOnMain(context: Context, resId: Int) {
        val app = context.applicationContext ?: context
        val show = Runnable { Toast.makeText(app, resId, Toast.LENGTH_SHORT).show() }
        if (Looper.myLooper() == Looper.getMainLooper()) show.run()
        else Handler(Looper.getMainLooper()).post(show)
    }
}
