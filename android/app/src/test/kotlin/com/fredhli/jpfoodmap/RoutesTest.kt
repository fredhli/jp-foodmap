package com.fredhli.jpfoodmap

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The shell's "is this URL ours?" and "does this URL name a restaurant?" rules
 * (docs/STANDARDS.md §6, docs/PLAN.md §5.6). Plain JVM, no Android, no emulator.
 */
class RoutesTest {

    private val origins = Routes.appOrigins()

    // ---- originOf -----------------------------------------------------------------------

    @Test
    fun `https default port dropped`() {
        assertEquals("https://jpfoodmap.com", Routes.originOf("https://jpfoodmap.com/"))
        assertEquals("https://jpfoodmap.com", Routes.originOf("https://jpfoodmap.com:443/?r=abc"))
        assertEquals("http://127.0.0.1", Routes.originOf("http://127.0.0.1:80/"))
    }

    @Test
    fun `explicit non-default port kept`() {
        assertEquals("https://jpfoodmap.com:8443", Routes.originOf("https://jpfoodmap.com:8443/"))
        assertEquals("http://127.0.0.1:8000", Routes.originOf("http://127.0.0.1:8000/index.html"))
    }

    @Test
    fun `uppercase scheme and host lowercased`() {
        assertEquals("https://jpfoodmap.com", Routes.originOf("HTTPS://JPFoodMap.COM/"))
    }

    @Test
    fun `no authority or non-http scheme is null`() {
        assertNull(Routes.originOf("about:blank"))
        assertNull(Routes.originOf("javascript:void(0)"))
        assertNull(Routes.originOf("mailto:someone@example.com"))
        assertNull(Routes.originOf("geo:35.68,139.76"))
        assertNull(Routes.originOf("intent://scan#Intent;scheme=zxing;end"))
        assertNull(Routes.originOf("https://"))
        assertNull(Routes.originOf("https://:8080/"))
    }

    @Test
    fun `garbage is null`() {
        assertNull(Routes.originOf(null))
        assertNull(Routes.originOf(""))
        assertNull(Routes.originOf("not a url"))
        assertNull(Routes.originOf("https://host:notaport/"))
        assertNull(Routes.originOf("https://host:70000/"))
    }

    @Test
    fun `userinfo dropped and ipv6 kept`() {
        assertEquals("https://jpfoodmap.com", Routes.originOf("https://user:pw@jpfoodmap.com/"))
        assertEquals("http://[::1]:8000", Routes.originOf("http://[::1]:8000/"))
        assertEquals("http://[fd7a::1]", Routes.originOf("http://[FD7A::1]/"))
    }

    @Test
    fun `backslash ends the authority like chromium`() {
        // WHATWG: '\' in an http(s) URL is a path separator, so Chromium navigates this to
        // evil.com — the origin has to say so, or "evil.com\" would pass for userinfo and a
        // hostile link would be treated as ours.
        assertEquals("https://evil.com", Routes.originOf("https://evil.com\\@jpfoodmap.com/?r=abc"))
        assertFalse(Routes.isAppOrigin("https://evil.com\\@jpfoodmap.com/?r=abc", origins))
        // The other way round the host is still ours; the backslash just starts the path.
        assertEquals("https://jpfoodmap.com", Routes.originOf("https://jpfoodmap.com\\evil.com/"))
        assertNull(Routes.originOf("https://\\evil.com/"))
    }

    // ---- appOrigins / isAppOrigin -------------------------------------------------------

    @Test
    fun `the shipped origin set is exactly the one host`() {
        assertEquals(setOf("https://jpfoodmap.com"), Routes.appOrigins())
        assertEquals(Routes.appOrigins(), Routes.appOrigins(Routes.BASE_URL))
        assertEquals(1, Routes.appOrigins("garbage").size)
    }

    @Test
    fun `a staging base adds its own origin`() {
        val o = Routes.appOrigins("http://127.0.0.1:8000")
        assertEquals(2, o.size)
        assertTrue(Routes.isAppOrigin("http://127.0.0.1:8000/?r=abc", o))
        assertTrue(Routes.isAppOrigin("https://jpfoodmap.com/", o))
    }

    @Test
    fun `isAppOrigin refuses everything else`() {
        assertTrue(Routes.isAppOrigin("https://jpfoodmap.com/?r=2s7z1", origins))
        assertTrue(Routes.isAppOrigin("https://JPFOODMAP.com/privacy.html", origins))
        // The API is a different origin on purpose: the shell never navigates to it.
        assertFalse(Routes.isAppOrigin("https://api.jpfoodmap.com/api/state", origins))
        assertFalse(Routes.isAppOrigin("https://www.jpfoodmap.com/", origins))
        assertFalse(Routes.isAppOrigin("http://jpfoodmap.com/", origins))
        assertFalse(Routes.isAppOrigin("https://jpfoodmap.com.evil.test/", origins))
        assertFalse(Routes.isAppOrigin("https://tabelog.com/tokyo/A1301/", origins))
        assertFalse(Routes.isAppOrigin(null, origins))
    }

    // ---- shareIdOf ----------------------------------------------------------------------

    @Test
    fun `shareIdOf reads the id the page mints`() {
        // The exact shape of shareUrlFor() in map.py: origin + pathname + "?r=" + base36.
        assertEquals("2s7z1", Routes.shareIdOf("https://jpfoodmap.com/?r=2s7z1"))
        assertEquals("a", Routes.shareIdOf("https://jpfoodmap.com/?r=a"))
        assertEquals("0", Routes.shareIdOf("https://jpfoodmap.com/?r=0"))
        assertEquals("zzzzzz", Routes.shareIdOf("https://jpfoodmap.com/?r=zzzzzz"))
    }

    @Test
    fun `shareIdOf finds r among other parameters`() {
        assertEquals("2s7z1", Routes.shareIdOf("https://jpfoodmap.com/?lang=en&r=2s7z1"))
        assertEquals("2s7z1", Routes.shareIdOf("https://jpfoodmap.com/?r=2s7z1&lang=en"))
        assertEquals("2s7z1", Routes.shareIdOf("https://jpfoodmap.com/?r=2s7z1#anything"))
    }

    @Test
    fun `shareIdOf is null when there is no id`() {
        assertNull(Routes.shareIdOf(null))
        assertNull(Routes.shareIdOf(""))
        assertNull(Routes.shareIdOf("https://jpfoodmap.com/"))
        assertNull(Routes.shareIdOf("https://jpfoodmap.com/?lang=en"))
        assertNull(Routes.shareIdOf("https://jpfoodmap.com/?r="))
        assertNull(Routes.shareIdOf("https://jpfoodmap.com/?r"))
        // A '?' inside the fragment is not a query at all.
        assertNull(Routes.shareIdOf("https://jpfoodmap.com/#x?r=2s7z1"))
    }

    @Test
    fun `shareIdOf refuses anything the page could not have produced`() {
        // Upper case: URLSearchParams is case-sensitive on the key, and base36 from
        // Number.toString(36) is lower case on the value.
        assertNull(Routes.shareIdOf("https://jpfoodmap.com/?R=2s7z1"))
        assertNull(Routes.shareIdOf("https://jpfoodmap.com/?r=2S7Z1"))
        assertNull(Routes.shareIdOf("https://jpfoodmap.com/?r=2s7z1x9"))     // seven chars
        assertNull(Routes.shareIdOf("https://jpfoodmap.com/?r=a-b"))
        assertNull(Routes.shareIdOf("https://jpfoodmap.com/?r=%22"))
        assertNull(Routes.shareIdOf("https://jpfoodmap.com/?r=a+b"))
        assertNull(Routes.shareIdOf("https://jpfoodmap.com/?rr=2s7z1"))
        // Not a partial match: a bad value is refused, not searched past.
        assertNull(Routes.shareIdOf("https://jpfoodmap.com/?r=BAD&r=2s7z1"))
    }

    // ---- pathOf / stripQuery / stripFragment --------------------------------------------

    @Test
    fun `pathOf strips query and fragment`() {
        assertEquals("/", Routes.pathOf("https://jpfoodmap.com"))
        assertEquals("/", Routes.pathOf("https://jpfoodmap.com/?r=2s7z1"))
        assertEquals("/privacy.html", Routes.pathOf("https://jpfoodmap.com/privacy.html?a=1#b"))
        assertEquals("", Routes.pathOf("about:blank"))
        assertEquals("", Routes.pathOf(null))
    }

    @Test
    fun `stripQuery and stripFragment`() {
        assertEquals("https://jpfoodmap.com/", Routes.stripQuery("https://jpfoodmap.com/?r=2s7z1"))
        assertEquals("https://h/p#f", Routes.stripQuery("https://h/p?k=x#f"))
        assertEquals("https://h/p#a?b", Routes.stripQuery("https://h/p#a?b"))
        assertEquals("https://h/p", Routes.stripQuery("https://h/p"))
        assertEquals("https://h/p?k=x", Routes.stripFragment("https://h/p?k=x#f"))
        assertNull(Routes.stripFragment(null))
    }

    // ---- jsStringLiteral -----------------------------------------------------------------

    @Test
    fun `jsStringLiteral escapes what could break out of a literal`() {
        assertEquals("\"2s7z1\"", Routes.jsStringLiteral("2s7z1"))
        assertEquals("\"a\\\"b\"", Routes.jsStringLiteral("a\"b"))
        assertEquals("\"a\\\\b\"", Routes.jsStringLiteral("a\\b"))
        assertEquals("\"a\\nb\\rc\"", Routes.jsStringLiteral("a\nb\rc"))
        assertEquals("\"\\u003C/script>\"", Routes.jsStringLiteral("</script>"))
        assertEquals("\"\\u2028\\u2029\"", Routes.jsStringLiteral("\u2028\u2029"))
        assertEquals("\"\\u0001\"", Routes.jsStringLiteral("\u0001"))
        assertEquals("\"https://jpfoodmap.com/?r='x'\"", Routes.jsStringLiteral("https://jpfoodmap.com/?r='x'"))
    }
}
