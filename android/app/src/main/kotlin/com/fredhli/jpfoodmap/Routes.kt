package com.fredhli.jpfoodmap

/**
 * Pure string work on the site's URLs. No Android types on purpose: every rule here is
 * testable off-device, and the deep-link and link-policy decisions that depend on them are
 * the two places a mistake sends the user somewhere they did not ask to go.
 *
 * java.net.URI was considered and rejected: it throws on URLs a WebView reports happily
 * (a space in a fragment, an underscore in a host), and `originOf(webView.url)` runs on
 * every navigation — it must never throw.
 */
object Routes {

    /** The one host the shell claims (docs/PLAN.md D8: no `www`, it does not resolve). */
    val APP_HOSTS: Set<String> = setOf("jpfoodmap.com")

    const val BASE_URL: String = "https://jpfoodmap.com/"

    /**
     * `scheme://authority` — the part of a URL an origin is made of.
     *
     * The authority ends at the first of `/ ? #` — and `\`: the WHATWG URL parser Chromium
     * uses treats a backslash in a special-scheme (http/https) URL as a path separator, so
     * `https://evil.com\@jpfoodmap.com/` navigates to evil.com with a path of
     * `/@jpfoodmap.com/`. RFC 3986 knows no such rule, which is why a parser that stops
     * only at `/ ? #` reads `evil.com\` as userinfo and answers jpfoodmap.com. This is a
     * security boundary — [isAppOrigin] gates the bridge's origin set and the App Link
     * route — so the origin here has to be the one Chromium actually navigates to.
     */
    private val AUTHORITY = Regex("^([A-Za-z][A-Za-z0-9+.-]*)://([^/?#\\\\]*)")

    /**
     * The `?r=` value the page mints: the trailing numeric segment of a Tabelog detail_url
     * in base36 (map.py `shareIdOf`). All 9,807 rows produce one, all distinct, at most 5
     * characters; 6 is the headroom for a corpus that grows past 36^5. Anything else —
     * a percent escape, an upper-case letter, a longer string — is not an id this site
     * ever produced, and the shell refuses it rather than interpolating it into JS.
     */
    private val SHARE_ID = Regex("^[0-9a-z]{1,6}$")

    /**
     * `https://host[:port]` — lowercased scheme+host, explicit port kept only when it is
     * not the scheme's default; null when unparsable or without a host (`about:blank`,
     * `javascript:`, `mailto:`). Only http and https have origins this shell cares about:
     * an `intent://scan` URL parses to something host-shaped but must never compare equal
     * to anything.
     */
    fun originOf(url: String?): String? {
        val m = AUTHORITY.find(url?.trim() ?: return null) ?: return null
        val scheme = m.groupValues[1].lowercase()
        if (scheme != "http" && scheme != "https") return null
        // userinfo@ is legal in a URL and irrelevant to the origin.
        val authority = m.groupValues[2].substringAfterLast('@')
        if (authority.isEmpty()) return null
        val host: String
        val portText: String?
        if (authority.startsWith("[")) {
            // IPv6 literal: the colons inside the brackets are not a port separator.
            val close = authority.indexOf(']')
            if (close < 0) return null
            host = authority.substring(0, close + 1)
            val rest = authority.substring(close + 1)
            portText = when {
                rest.isEmpty() -> null
                rest.startsWith(":") -> rest.substring(1)
                else -> return null
            }
        } else {
            val colon = authority.lastIndexOf(':')
            if (colon >= 0) {
                host = authority.substring(0, colon)
                portText = authority.substring(colon + 1)
            } else {
                host = authority
                portText = null
            }
        }
        if (host.isEmpty()) return null
        val port = if (portText.isNullOrEmpty()) -1
        else portText.toIntOrNull()?.takeIf { it in 1..65535 } ?: return null
        val default = if (scheme == "https") 443 else 80
        val h = host.lowercase()
        return if (port == -1 || port == default) "$scheme://$h" else "$scheme://$h:$port"
    }

    /**
     * The origin set the bridge and the link classifier are scoped to:
     * `originOf(baseUrl)` ∪ `{ "https://$h" for h in APP_HOSTS }`. With the shipped
     * [BASE_URL] that is exactly one entry; the parameter exists so a build pointed at a
     * staging origin does not have to special-case anything.
     */
    fun appOrigins(baseUrl: String = BASE_URL): Set<String> {
        val out = LinkedHashSet<String>()
        originOf(baseUrl)?.let { out.add(it) }
        for (h in APP_HOSTS) out.add("https://$h")
        return out
    }

    fun isAppOrigin(url: String?, appOrigins: Set<String>): Boolean {
        val o = originOf(url) ?: return false
        return o in appOrigins
    }

    /**
     * The `?r=` share id of a URL, or null when it carries none, carries an empty one, or
     * carries something that is not a base36 id.
     *
     * Case-sensitive on the key and read out of the query only (never the fragment),
     * because that is what `new URLSearchParams(location.search).get('r')` does in the
     * page (map.py, M-032) — the shell and the page must agree on which URLs name a
     * restaurant or the hot path would open a card the cold path would not.
     */
    fun shareIdOf(url: String?): String? {
        val u = url ?: return null
        val q = u.indexOf('?')
        if (q < 0) return null
        val hash = u.indexOf('#')
        if (hash in 0 until q) return null                       // the '?' is inside the fragment
        val query = if (hash < 0) u.substring(q + 1) else u.substring(q + 1, hash)
        for (pair in query.split('&')) {
            if (pair.length < 2 || pair[0] != 'r') continue
            if (pair[1] != '=') continue
            val value = pair.substring(2)
            return if (SHARE_ID.matches(value)) value else null
        }
        return null
    }

    /** The path of a URL ("/" when empty), query and fragment stripped; "" when not a URL. */
    fun pathOf(url: String?): String {
        val u = url?.trim() ?: return ""
        val m = AUTHORITY.find(u) ?: return ""
        var rest = u.substring(m.range.last + 1)
        val cut = rest.indexOfFirst { it == '?' || it == '#' }
        if (cut >= 0) rest = rest.substring(0, cut)
        return if (rest.isEmpty()) "/" else rest
    }

    /** The URL without its `#fragment`; null stays null. Same-document comparisons use this. */
    fun stripFragment(url: String?): String? = url?.substringBefore('#')

    /**
     * The URL without its `?query`, fragment kept: `https://h/p?r=x#y` → `https://h/p#y`.
     * Used before a URL is shown or saved anywhere. Nothing on this site puts a secret in
     * a query — `?r` and `?lang` are the only two parameters that exist — but the rule is
     * flat (STANDARDS §0.4) so a future parameter cannot leak by being forgotten about.
     */
    fun stripQuery(url: String): String {
        val hash = url.indexOf('#')
        val q = url.indexOf('?')
        if (q < 0 || (hash in 0 until q)) return url
        return if (hash < 0) url.substring(0, q) else url.substring(0, q) + url.substring(hash)
    }

    /**
     * A double-quoted JS string literal: backslash, double quote, `\n`, `\r`, U+2028/U+2029
     * and `<` escaped (so `</script>` can never terminate an inline script). Other control
     * characters go out as `\uXXXX`, so the literal is always a single line.
     */
    fun jsStringLiteral(s: String): String {
        val sb = StringBuilder(s.length + 2).append('"')
        for (c in s) {
            when (c) {
                '\\' -> sb.append("\\\\")
                '"' -> sb.append("\\\"")
                '\n' -> sb.append("\\n")
                '\r' -> sb.append("\\r")
                '\u2028' -> sb.append("\\u2028")
                '\u2029' -> sb.append("\\u2029")
                '<' -> sb.append("\\u003C")
                else -> if (c.code < 0x20) sb.append(String.format("\\u%04X", c.code)) else sb.append(c)
            }
        }
        return sb.append('"').toString()
    }
}
