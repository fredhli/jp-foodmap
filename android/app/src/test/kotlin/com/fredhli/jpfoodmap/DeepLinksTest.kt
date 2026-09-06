package com.fredhli.jpfoodmap

import android.content.Intent
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Intent → [DeepLinks.Target] (docs/STANDARDS.md §6.2–6.4, docs/PLAN.md §5.6), plus the
 * exact text of the hot-path snippet the shell evaluates in the page.
 *
 * Everything here goes through the pure overload of [DeepLinks.targetOf]: `Intent.action`
 * and `Intent.data` cannot be read in a plain-JVM test (the mockable android.jar throws),
 * while `Intent.ACTION_VIEW` and `Intent.FLAG_ACTIVITY_LAUNCHED_FROM_HISTORY` are
 * compile-time constants and inline, so the values under test are the real ones.
 */
class DeepLinksTest {

    private val origins = Routes.appOrigins()

    /** Stands in for T5's Links.classify, which is not this task's to depend on. */
    private val classify: (String, Set<String>) -> Links.Nav = { url, _ ->
        when {
            url.startsWith("intent:") -> Links.Nav.INTENT_URI
            url.startsWith("http://") || url.startsWith("https://") -> Links.Nav.EXTERNAL
            else -> Links.Nav.OTHER_SCHEME
        }
    }

    private fun target(
        url: String?,
        action: String? = Intent.ACTION_VIEW,
        flags: Int = 0,
        stale: Boolean = false,
    ) = DeepLinks.targetOf(action, url, flags, stale, origins, classify)

    // ---- Share: the one URL this site shares ---------------------------------------------

    @Test
    fun `a share link becomes Share, id and fallback url both kept`() {
        val t = target("https://jpfoodmap.com/?r=2s7z1")
        assertEquals(DeepLinks.Target.Share("2s7z1", "https://jpfoodmap.com/?r=2s7z1"), t)
    }

    @Test
    fun `a share link with other parameters is still Share`() {
        assertEquals(
            DeepLinks.Target.Share("2s7z1", "https://jpfoodmap.com/?lang=en&r=2s7z1"),
            target("https://jpfoodmap.com/?lang=en&r=2s7z1"),
        )
    }

    @Test
    fun `surrounding whitespace is trimmed off the url`() {
        assertEquals(
            DeepLinks.Target.Share("2s7z1", "https://jpfoodmap.com/?r=2s7z1"),
            target("  https://jpfoodmap.com/?r=2s7z1  "),
        )
    }

    // ---- Load: ours, but not a card -------------------------------------------------------

    @Test
    fun `the bare site is a Load`() {
        assertEquals(DeepLinks.Target.Load("https://jpfoodmap.com/"), target("https://jpfoodmap.com/"))
    }

    @Test
    fun `a language link is a Load, because switching language is a navigation`() {
        // STANDARDS §6.4: the page's own language switch is a full page load, so the shell
        // does not try to be cleverer than a loadUrl here.
        assertEquals(
            DeepLinks.Target.Load("https://jpfoodmap.com/?lang=en"),
            target("https://jpfoodmap.com/?lang=en"),
        )
    }

    @Test
    fun `an id the page could not have minted degrades to Load, never to Share`() {
        // A stale or hand-edited link still lands the visitor on a working map — the page
        // ignores an unknown ?r= in silence (map.py M-032) — but it must not reach the hot
        // path, where the id would be interpolated into script.
        for (bad in listOf("?r=", "?r=2S7Z1", "?r=abcdefg", "?r=%3Cscript%3E", "?r=a%22b")) {
            val url = "https://jpfoodmap.com/$bad"
            assertEquals(DeepLinks.Target.Load(url), target(url))
        }
    }

    @Test
    fun `other pages on the site are Loads too`() {
        assertEquals(
            DeepLinks.Target.Load("https://jpfoodmap.com/privacy.html"),
            target("https://jpfoodmap.com/privacy.html"),
        )
    }

    // ---- Leave: not ours ------------------------------------------------------------------

    @Test
    fun `an off-site url is handed over, not loaded in the shell`() {
        assertEquals(
            DeepLinks.Target.Leave("https://tabelog.com/tokyo/A1301/", Links.Nav.EXTERNAL),
            target("https://tabelog.com/tokyo/A1301/"),
        )
        // The look-alike host is the one that matters: it must never be Load.
        assertEquals(
            DeepLinks.Target.Leave("https://jpfoodmap.com.evil.test/?r=2s7z1", Links.Nav.EXTERNAL),
            target("https://jpfoodmap.com.evil.test/?r=2s7z1"),
        )
        assertEquals(
            DeepLinks.Target.Leave("https://evil.com\\@jpfoodmap.com/?r=2s7z1", Links.Nav.EXTERNAL),
            target("https://evil.com\\@jpfoodmap.com/?r=2s7z1"),
        )
        assertEquals(
            DeepLinks.Target.Leave("intent://scan#Intent;scheme=zxing;end", Links.Nav.INTENT_URI),
            target("intent://scan#Intent;scheme=zxing;end"),
        )
        assertEquals(
            DeepLinks.Target.Leave("geo:35.68,139.76", Links.Nav.OTHER_SCHEME),
            target("geo:35.68,139.76"),
        )
    }

    @Test
    fun `the production classifier is what ships, not just the fake above`() {
        // Same three URLs, with the default argument — Links.classify — in place, so a
        // change on either side of that seam shows up here rather than on the phone.
        fun real(url: String) = DeepLinks.targetOf(Intent.ACTION_VIEW, url, 0, false, origins)
        assertEquals(
            DeepLinks.Target.Leave("https://tabelog.com/tokyo/A1301/", Links.Nav.EXTERNAL),
            real("https://tabelog.com/tokyo/A1301/"),
        )
        assertEquals(
            DeepLinks.Target.Leave("geo:35.68,139.76", Links.Nav.OTHER_SCHEME),
            real("geo:35.68,139.76"),
        )
        // …and an app-origin URL never reaches the classifier at all.
        assertEquals(DeepLinks.Target.Share("2s7z1", "https://jpfoodmap.com/?r=2s7z1"),
            real("https://jpfoodmap.com/?r=2s7z1"))
    }

    // ---- None: nothing to act on ----------------------------------------------------------

    @Test
    fun `a relaunch from recents does nothing`() {
        // Recents re-delivers the root Intent verbatim; acting on it would reopen a card
        // the user closed. Both the flag and the caller's own stale bit say so.
        assertEquals(
            DeepLinks.Target.None,
            target("https://jpfoodmap.com/?r=2s7z1", flags = Intent.FLAG_ACTIVITY_LAUNCHED_FROM_HISTORY),
        )
        assertEquals(DeepLinks.Target.None, target("https://jpfoodmap.com/?r=2s7z1", stale = true))
        // …and the flag is checked as a bit, not compared to the whole field.
        assertEquals(
            DeepLinks.Target.None,
            target(
                "https://jpfoodmap.com/?r=2s7z1",
                flags = Intent.FLAG_ACTIVITY_LAUNCHED_FROM_HISTORY or Intent.FLAG_ACTIVITY_NEW_TASK,
            ),
        )
    }

    @Test
    fun `a launcher tap or an extras-only intent does nothing`() {
        assertEquals(DeepLinks.Target.None, target(null, action = Intent.ACTION_MAIN))
        assertEquals(DeepLinks.Target.None, target("https://jpfoodmap.com/?r=2s7z1", action = Intent.ACTION_MAIN))
        assertEquals(DeepLinks.Target.None, target(null))
        assertEquals(DeepLinks.Target.None, target(""))
        assertEquals(DeepLinks.Target.None, target("   "))
        assertEquals(DeepLinks.Target.None, target(null, action = null))
    }

    @Test
    fun `a null intent does nothing`() {
        assertEquals(DeepLinks.Target.None, DeepLinks.targetOf(null, false, origins))
    }

    // ---- hotShareJs -------------------------------------------------------------------------

    @Test
    fun `hotShareJs is the snippet the contract names, verbatim`() {
        assertEquals(
            "(function(id){try{return !!(window.__jpfmOpenShare&&window.__jpfmOpenShare(id));}" +
                "catch(e){return false;}})(\"2s7z1\")",
            DeepLinks.hotShareJs("2s7z1"),
        )
    }

    @Test
    fun `hotShareJs escapes its argument even though the id was already validated`() {
        assertTrue(DeepLinks.hotShareJs("a\"b").endsWith("(\"a\\\"b\")"))
        assertTrue(DeepLinks.hotShareJs("</script>").endsWith("(\"\\u003C/script>\")"))
        // Single line, always: evaluateJavascript takes one expression.
        assertEquals(1, DeepLinks.hotShareJs("a\nb").lines().size)
    }
}
