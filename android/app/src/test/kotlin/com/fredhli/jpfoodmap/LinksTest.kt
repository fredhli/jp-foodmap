package com.fredhli.jpfoodmap

import com.fredhli.jpfoodmap.Links.Nav
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The navigation gate's decision matrix, on a plain JVM. Only the pure half of Links is
 * exercised (classify / isIntentUri / intentFallbackUrl); the Android half dispatches
 * Intents and cannot run against the stub android.jar, whose every method throws.
 *
 * What this protects: the WebView must never end up on an off-site document while it is
 * carrying the site's session cookie, and the two links every restaurant card carries
 * (Tabelog, Google Maps) must be classified as the external links they are.
 */
class LinksTest {

    /** What Routes.appOrigins() produces: one host, no www (docs/PLAN.md D8). */
    private val origins = setOf("https://jpfoodmap.com")

    // ---------------------------------------------------------------- the site itself

    @Test
    fun `the site stays in the webview`() {
        assertEquals(Nav.IN_APP, Links.classify("https://jpfoodmap.com/", origins))
        assertEquals(Nav.IN_APP, Links.classify("https://jpfoodmap.com", origins))
        // The deep link the share button produces, and the language switch's whole-page nav.
        assertEquals(Nav.IN_APP, Links.classify("https://jpfoodmap.com/?r=1a2b3c", origins))
        assertEquals(Nav.IN_APP, Links.classify("https://jpfoodmap.com/?lang=en", origins))
        // The two hand-written standalone pages. STANDARDS §5.1 names them: there is no
        // APP_DOCUMENT rung here, every path on the host is a page the WebView renders.
        assertEquals(Nav.IN_APP, Links.classify("https://jpfoodmap.com/privacy.html", origins))
        assertEquals(Nav.IN_APP, Links.classify("https://jpfoodmap.com/404.html", origins))
        // Build outputs the page fetches at run time are the same origin too.
        assertEquals(Nav.IN_APP, Links.classify("https://jpfoodmap.com/data/restaurants.json?v=9", origins))
    }

    @Test
    fun `scheme and host are case-insensitive`() {
        assertEquals(Nav.IN_APP, Links.classify("HTTPS://JPFOODMAP.COM/", origins))
        assertEquals(Nav.IN_APP, Links.classify("Https://JpFoodMap.Com/?r=x", origins))
        assertEquals(Nav.INTENT_URI, Links.classify("INTENT://scan/#Intent;scheme=zxing;end", origins))
        assertEquals(Nav.OTHER_SCHEME, Links.classify("MAILTO:someone@example.com", origins))
    }

    @Test
    fun `an explicit default port is the same origin and any other port is not`() {
        assertEquals(Nav.IN_APP, Links.classify("https://jpfoodmap.com:443/", origins))
        assertEquals(Nav.EXTERNAL, Links.classify("https://jpfoodmap.com:8443/", origins))
        // http on the same host is a different origin: the site is https-only.
        assertEquals(Nav.EXTERNAL, Links.classify("http://jpfoodmap.com/", origins))
    }

    // ---------------------------------------------------------------- the real outbound links

    @Test
    fun `the two links on every restaurant card are external`() {
        // BOTTOM_SHEET_HTML's "Tabelog ↗" anchor (map.py ~9445).
        assertEquals(
            Nav.EXTERNAL,
            Links.classify("https://tabelog.com/kyoto/A2601/A260302/26000305/", origins),
        )
        // The Google Maps quick-jump, both shapes (map.py ~9305): a plain search, and the
        // calibrated one that carries a place id.
        assertEquals(
            Nav.EXTERNAL,
            Links.classify("https://www.google.com/maps/search/?api=1&query=%E5%AF%BF%E5%8F%B8", origins),
        )
        assertEquals(
            Nav.EXTERNAL,
            Links.classify(
                "https://www.google.com/maps/search/?api=1&query=x&query_place_id=ChIJ_abc-123",
                origins,
            ),
        )
        // The photo host the sheet links out to, and the Nominatim search the box calls.
        assertEquals(Nav.EXTERNAL, Links.classify("https://tblg.k-img.com/restaurant/images/x.jpg", origins))
        assertEquals(Nav.EXTERNAL, Links.classify("https://nominatim.openstreetmap.org/search?q=x", origins))
    }

    @Test
    fun `the api subdomain is not the page origin`() {
        // The sync Worker. The page fetches it with credentials:'include'; it is never a
        // navigation, and if one ever arrived it must not be loaded as a document here.
        assertEquals(Nav.EXTERNAL, Links.classify("https://api.jpfoodmap.com/api/state", origins))
        // The R2 asset host the transit overlay comes from.
        assertEquals(Nav.EXTERNAL, Links.classify("https://assets.jpfoodmap.com/japan-low.abc.geojson", origins))
    }

    // ---------------------------------------------------------------- spoofs

    @Test
    fun `host spoofs do not reach the webview`() {
        // Userinfo: the host is evil.com, whatever is written before the '@'.
        assertEquals(Nav.EXTERNAL, Links.classify("https://jpfoodmap.com@evil.com/", origins))
        // Backslash: Chromium ends the authority at '\' (WHATWG), so this navigates to
        // evil.com with "/@jpfoodmap.com/" as its path. java.net.URI rejects the backslash,
        // which is how the URL reaches the hand parse at all.
        assertEquals(Nav.EXTERNAL, Links.classify("https://evil.com\\@jpfoodmap.com/", origins))
        // A subdomain and a suffix lookalike are both a different origin.
        assertEquals(Nav.EXTERNAL, Links.classify("https://x.jpfoodmap.com/", origins))
        assertEquals(Nav.EXTERNAL, Links.classify("https://jpfoodmap.com.evil.com/", origins))
        assertEquals(Nav.EXTERNAL, Links.classify("https://jpfoodmap.co/", origins))
    }

    @Test
    fun `characters java net URI rejects still classify`() {
        // A raw '|' in a query is a URISyntaxException for java.net.URI and an ordinary
        // link for Chromium; falling through to the hand parse is what keeps them apart.
        assertEquals(Nav.IN_APP, Links.classify("https://jpfoodmap.com/?q=a|b", origins))
        assertEquals(Nav.EXTERNAL, Links.classify("https://tabelog.com/?q=a|b", origins))
        assertEquals(Nav.EXTERNAL, Links.classify("https://evil.com\\@jpfoodmap.com/?q=a|b", origins))
    }

    // ---------------------------------------------------------------- the other schemes

    @Test
    fun `intent uris`() {
        assertEquals(
            Nav.INTENT_URI,
            Links.classify("intent://scan/#Intent;scheme=zxing;package=com.google.zxing.client.android;end", origins),
        )
        assertEquals(Nav.INTENT_URI, Links.classify("intent:#Intent;action=android.intent.action.VIEW;end", origins))
    }

    @Test
    fun `schemes with a stock handler`() {
        assertEquals(Nav.OTHER_SCHEME, Links.classify("mailto:someone@example.com?subject=hi", origins))
        assertEquals(Nav.OTHER_SCHEME, Links.classify("tel:+81312345678", origins))
        assertEquals(Nav.OTHER_SCHEME, Links.classify("sms:+81312345678", origins))
        assertEquals(Nav.OTHER_SCHEME, Links.classify("smsto:+81312345678", origins))
        // A Japanese restaurant page linking its own coordinates.
        assertEquals(Nav.OTHER_SCHEME, Links.classify("geo:35.68,139.76?q=Tokyo", origins))
        assertEquals(Nav.OTHER_SCHEME, Links.classify("market://details?id=com.android.chrome", origins))
    }

    @Test
    fun `blocked schemes and garbage`() {
        assertEquals(Nav.BLOCKED, Links.classify("javascript:alert(1)", origins))
        assertEquals(Nav.BLOCKED, Links.classify("JavaScript:alert(1)", origins))
        assertEquals(Nav.BLOCKED, Links.classify("file:///etc/hosts", origins))
        assertEquals(Nav.BLOCKED, Links.classify("content://com.android.contacts/contacts", origins))
        assertEquals(Nav.BLOCKED, Links.classify("data:text/html,<script>1</script>", origins))
        assertEquals(Nav.BLOCKED, Links.classify("blob:https://jpfoodmap.com/uuid", origins))
        assertEquals(Nav.BLOCKED, Links.classify("about:blank", origins))
        assertEquals(Nav.BLOCKED, Links.classify("ftp://example.com/x", origins))
        assertEquals(Nav.BLOCKED, Links.classify(null, origins))
        assertEquals(Nav.BLOCKED, Links.classify("", origins))
        assertEquals(Nav.BLOCKED, Links.classify("   ", origins))
        assertEquals(Nav.BLOCKED, Links.classify("/relative/path", origins))
        assertEquals(Nav.BLOCKED, Links.classify("not a url", origins))
        assertEquals(Nav.BLOCKED, Links.classify("https:///nohost", origins))
    }

    @Test
    fun `an empty origin set makes even the site external, never in-app`() {
        // Routes.appOrigins() cannot return empty, but a caller that got it wrong must fail
        // towards "hand it to the browser" and never towards "load it in the WebView".
        assertEquals(Nav.EXTERNAL, Links.classify("https://jpfoodmap.com/", emptySet()))
    }

    // ---------------------------------------------------------------- intent:// details

    @Test
    fun `isIntentUri`() {
        assertTrue(Links.isIntentUri("intent://scan/#Intent;scheme=zxing;end"))
        assertTrue(Links.isIntentUri("INTENT:#Intent;end"))
        assertTrue(Links.isIntentUri("  intent:x"))
        assertFalse(Links.isIntentUri("https://jpfoodmap.com/intent:"))
        assertFalse(Links.isIntentUri("intents://x"))
        assertFalse(Links.isIntentUri(""))
        assertFalse(Links.isIntentUri(null))
    }

    @Test
    fun `intent fallback url present`() {
        val url = "intent://scan/#Intent;scheme=zxing;package=com.google.zxing.client.android;" +
            "S.browser_fallback_url=https%3A%2F%2Ftabelog.com%2Fget%3Fa%3D1%26b%3Dx%2By;end"
        // The '+' survives: decoding is percent-only, not form-decoding.
        assertEquals("https://tabelog.com/get?a=1&b=x+y", Links.intentFallbackUrl(url))
    }

    @Test
    fun `intent fallback url may be unencoded and http`() {
        val url = "intent://x/#Intent;scheme=foo;S.browser_fallback_url=http://example.com/a;end"
        assertEquals("http://example.com/a", Links.intentFallbackUrl(url))
    }

    @Test
    fun `intent fallback url absent`() {
        assertNull(Links.intentFallbackUrl("intent://scan/#Intent;scheme=zxing;end"))
        assertNull(Links.intentFallbackUrl("intent://scan/#Intent;S.browser_fallback_url=;end"))
        assertNull(Links.intentFallbackUrl("https://jpfoodmap.com/no-intent-fragment"))
        assertNull(Links.intentFallbackUrl(""))
    }

    @Test
    fun `intent fallback url that is not http is dropped`() {
        assertNull(Links.intentFallbackUrl("intent://x/#Intent;S.browser_fallback_url=javascript%3Aalert(1);end"))
        assertNull(Links.intentFallbackUrl("intent://x/#Intent;S.browser_fallback_url=intent%3A%23Intent%3Bend;end"))
        assertNull(Links.intentFallbackUrl("intent://x/#Intent;S.browser_fallback_url=file%3A%2F%2F%2Fetc;end"))
        // A malformed percent-escape is not decodable, so it is not a URL we open.
        assertNull(Links.intentFallbackUrl("intent://x/#Intent;S.browser_fallback_url=https%ZZ;end"))
    }

    // ---------------------------------------------------------------- the enum itself

    @Test
    fun `the nav set is the five rungs the shell dispatches on`() {
        // T4's DeepLinks.Target.Leave carries a Nav, and MainActivity switches on it: a
        // rung added or renamed here is a compile break there, which is the intent.
        assertEquals(
            listOf(Nav.IN_APP, Nav.EXTERNAL, Nav.INTENT_URI, Nav.OTHER_SCHEME, Nav.BLOCKED),
            Nav.entries.toList(),
        )
    }
}
