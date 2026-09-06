package com.fredhli.jpfoodmap

import android.app.ApplicationExitInfo
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The diagnostics' pure half: the exit-reason names, the cold-start arithmetic, the URL cut,
 * and the shape of the two rendered forms. `exitsJson` asks the ActivityManager and `show`
 * builds a dialog, so neither can run here.
 *
 * The REASON_* numbers are read reflectively rather than written as literals: they are Java
 * compile-time constants, so a literal comparison would be `4 == 4` and could not fail if a
 * name moved.
 */
class DiagnosticsTest {

    private fun reason(name: String): Int =
        ApplicationExitInfo::class.java.getField(name).getInt(null)

    // ---------------------------------------------------------------- exit reason names

    @Test
    fun `the reasons a phone actually reports`() {
        assertEquals("REASON_UNKNOWN", Diagnostics.exitReasonName(reason("REASON_UNKNOWN")))
        assertEquals("REASON_EXIT_SELF", Diagnostics.exitReasonName(reason("REASON_EXIT_SELF")))
        assertEquals("REASON_SIGNALED", Diagnostics.exitReasonName(reason("REASON_SIGNALED")))
        assertEquals("REASON_LOW_MEMORY", Diagnostics.exitReasonName(reason("REASON_LOW_MEMORY")))
        assertEquals("REASON_CRASH", Diagnostics.exitReasonName(reason("REASON_CRASH")))
        assertEquals("REASON_CRASH_NATIVE", Diagnostics.exitReasonName(reason("REASON_CRASH_NATIVE")))
        assertEquals("REASON_ANR", Diagnostics.exitReasonName(reason("REASON_ANR")))
        assertEquals("REASON_USER_REQUESTED", Diagnostics.exitReasonName(reason("REASON_USER_REQUESTED")))
        assertEquals("REASON_USER_STOPPED", Diagnostics.exitReasonName(reason("REASON_USER_STOPPED")))
        assertEquals("REASON_FREEZER", Diagnostics.exitReasonName(reason("REASON_FREEZER")))
        // The one One UI's background limiter is most likely to hand back, which is why
        // exitsJson keeps `description` beside the reason.
        assertEquals("REASON_OTHER", Diagnostics.exitReasonName(reason("REASON_OTHER")))
    }

    @Test
    fun `the rest of the platform's reasons are named too`() {
        assertEquals(
            "REASON_INITIALIZATION_FAILURE",
            Diagnostics.exitReasonName(reason("REASON_INITIALIZATION_FAILURE")),
        )
        assertEquals("REASON_PERMISSION_CHANGE", Diagnostics.exitReasonName(reason("REASON_PERMISSION_CHANGE")))
        assertEquals(
            "REASON_EXCESSIVE_RESOURCE_USAGE",
            Diagnostics.exitReasonName(reason("REASON_EXCESSIVE_RESOURCE_USAGE")),
        )
        assertEquals("REASON_DEPENDENCY_DIED", Diagnostics.exitReasonName(reason("REASON_DEPENDENCY_DIED")))
        assertEquals("REASON_PACKAGE_STATE_CHANGE", Diagnostics.exitReasonName(reason("REASON_PACKAGE_STATE_CHANGE")))
        assertEquals("REASON_PACKAGE_UPDATED", Diagnostics.exitReasonName(reason("REASON_PACKAGE_UPDATED")))
    }

    @Test
    fun `an unknown reason keeps its number`() {
        assertEquals("REASON_999", Diagnostics.exitReasonName(999))
        assertEquals("REASON_-1", Diagnostics.exitReasonName(-1))
    }

    @Test
    fun `five exits is the window`() {
        assertEquals(5, Diagnostics.EXITS_MAX)
    }

    // ---------------------------------------------------------------- startup arithmetic

    @Test
    fun `a cold start is four gaps off the process start`() {
        val j = Diagnostics.startupJson(
            processMs = 100,
            createMs = 900,
            paintMs = 1500,
            readyMs = 2400,
            rendererGone = 0,
            activityCreates = 1,
        )
        assertEquals(800L, j.getLong("processToCreateMs"))
        assertEquals(1400L, j.getLong("processToPaintMs"))
        assertEquals(2300L, j.getLong("processToReadyMs"))
        assertEquals(1500L, j.getLong("createToReadyMs"))
        assertFalse(j.getBoolean("warmProcess"))
        assertEquals(1, j.getInt("activityCreates"))
        assertEquals(0, j.getInt("rendererGone"))
    }

    @Test
    fun `a process that was already up reads as warm`() {
        // This app has no background worker, so warmProcess should never be true on it —
        // which is exactly why it is reported: a true here means something started the
        // process that STANDARDS §10.1 says does not exist.
        val warm = Diagnostics.startupJson(100, 100 + Diagnostics.Startup.WARM_PROCESS_MS, 0, 0, 0, 1)
        assertTrue(warm.getBoolean("warmProcess"))
        // One millisecond under the threshold is still a cold start.
        val cold = Diagnostics.startupJson(100, 100 + Diagnostics.Startup.WARM_PROCESS_MS - 1, 0, 0, 0, 1)
        assertFalse(cold.getBoolean("warmProcess"))
    }

    @Test
    fun `a mark that was never taken is JSON null and not a zero`() {
        val j = Diagnostics.startupJson(100, 900, 1500, 0, 0, 1)
        assertTrue(j.isNull("processToReadyMs"))
        assertTrue(j.isNull("createToReadyMs"))
        assertEquals(1400L, j.getLong("processToPaintMs"))

        // No process mark either (a reading taken before onCreate ran): everything null,
        // warmProcess included, because there is nothing to test.
        val empty = Diagnostics.startupJson(0, 0, 0, 0, 0, 0)
        assertTrue(empty.isNull("processToCreateMs"))
        assertTrue(empty.isNull("processToPaintMs"))
        assertTrue(empty.isNull("processToReadyMs"))
        assertTrue(empty.isNull("createToReadyMs"))
        assertTrue(empty.isNull("warmProcess"))
    }

    @Test
    fun `the process scoped marks are first wins and the creates are counted`() {
        Diagnostics.Startup.resetForTest()
        try {
            Diagnostics.Startup.onActivityCreate(processStartUptimeMs = 100, uptimeMs = 900)
            // A recreation: the clock must not move, only the counter. STANDARDS §3.1 reads
            // activityCreates across a fold, so it has to count and not reset.
            Diagnostics.Startup.onActivityCreate(processStartUptimeMs = 5_000, uptimeMs = 6_000)
            Diagnostics.Startup.onFirstPaint(1500)
            Diagnostics.Startup.onFirstPaint(9_999)
            assertTrue(Diagnostics.Startup.onReady(2400))
            // Only the first READY answers true — reportFullyDrawn() is called exactly once.
            assertFalse(Diagnostics.Startup.onReady(9_999))
            Diagnostics.Startup.onRendererGone()

            val j = Diagnostics.Startup.json()
            assertEquals(800L, j.getLong("processToCreateMs"))
            assertEquals(1400L, j.getLong("processToPaintMs"))
            assertEquals(2300L, j.getLong("processToReadyMs"))
            assertEquals(1500L, j.getLong("createToReadyMs"))
            assertEquals(2, j.getInt("activityCreates"))
            assertEquals(1, j.getInt("rendererGone"))
        } finally {
            Diagnostics.Startup.resetForTest()
        }
    }

    // ---------------------------------------------------------------- the page probe

    @Test
    fun `the metrics script is a kotlin raw string with no template hazard`() {
        // A '$' in a Kotlin raw string is a template expression: it would either fail to
        // compile or, worse, splice a value into the JS. The probe must contain none.
        assertFalse(Diagnostics.JS_METRICS.contains('$'))
        assertTrue(Diagnostics.JS_METRICS.isNotBlank())
    }

    @Test
    fun `the metrics script reports every field the plan asks for`() {
        for (key in listOf(
            "innerWidth", "innerHeight", "outerWidth", "outerHeight", "dpr",
            "screen", "vv", "env", "bootId", "lang", "url", "ua", "native", "sw", "online",
        )) {
            assertTrue("JS_METRICS is missing $key", Diagnostics.JS_METRICS.contains("$key:"))
        }
        // The boot id is the field that proves a fold did not reload the page (§3.1); it
        // comes from the page's own app-bridge block (docs/PLAN.md §5.5).
        assertTrue(Diagnostics.JS_METRICS.contains("window.__jpfmBootId"))
    }

    @Test
    fun `the metrics script builds the url without a query and never reads page storage`() {
        // STANDARDS §0.4: the shell must not carry a query string into a screen people
        // screenshot, so the URL is assembled from origin + pathname and location.href is
        // never read.
        assertTrue(Diagnostics.JS_METRICS.contains("location.origin + location.pathname"))
        assertFalse(Diagnostics.JS_METRICS.contains("location.href"))
        assertFalse(Diagnostics.JS_METRICS.contains("location.search"))
        // STANDARDS §0.1: the shell reads nothing the page owns. `lang` comes off the DOM,
        // which the page assigns on every language switch.
        assertFalse(Diagnostics.JS_METRICS.contains("localStorage"))
        assertFalse(Diagnostics.JS_METRICS.contains("tabelog."))
    }

    @Test
    fun `unquote turns an evaluateJavascript result back into its string`() {
        assertEquals("{\"a\":1}", Diagnostics.unquote("\"{\\\"a\\\":1}\""))
        assertEquals("null", Diagnostics.unquote(null))
        // Not a JSON string: handed back raw so the caller can still show whatever it is.
        assertEquals("null", Diagnostics.unquote("null"))
        assertEquals("{\"a\":1}", Diagnostics.unquote("{\"a\":1}"))
        // JSONTokener is lenient about unquoted words and would read this as "garbage",
        // dropping the brace. Anything that does not start with a quote is passed through.
        assertEquals("garbage{", Diagnostics.unquote("garbage{"))
        assertEquals("Uncaught TypeError: x is not a function", Diagnostics.unquote("Uncaught TypeError: x is not a function"))
    }

    // ---------------------------------------------------------------- the rendered forms

    @Test
    fun `strip query cuts the query and the fragment`() {
        assertEquals("https://jpfoodmap.com/", Diagnostics.stripQuery("https://jpfoodmap.com/?r=1a2b"))
        assertEquals("https://jpfoodmap.com/", Diagnostics.stripQuery("https://jpfoodmap.com/#x"))
        assertEquals("https://jpfoodmap.com/", Diagnostics.stripQuery("https://jpfoodmap.com/?r=1#x"))
        assertEquals("https://jpfoodmap.com/", Diagnostics.stripQuery("https://jpfoodmap.com/"))
        assertEquals("", Diagnostics.stripQuery(""))
    }

    @Test
    fun `the dialog text carries both halves and no query`() {
        val page = """{"innerWidth":475,"url":"https://jpfoodmap.com/?r=1a2b3c","bootId":"k7f2x"}"""
        val native = JSONObject().put("textZoom", 130).put("pageLoads", 1)
        val text = Diagnostics.showText(page, native)
        assertTrue(text.startsWith("Page"))
        assertTrue(text.contains("Native"))
        assertTrue(text.contains("\"innerWidth\": 475"))
        assertTrue(text.contains("\"textZoom\": 130"))
        // The whole point of the cut: the id the user opened is not in the screenshot.
        assertTrue(text.contains("https://jpfoodmap.com/"))
        assertFalse(text.contains("?r="))
        assertFalse(text.contains("1a2b3c"))
    }

    @Test
    fun `the log line is one line of valid json with both halves`() {
        val page = """{"innerWidth":932,"url":"https://jpfoodmap.com/?r=zz","bootId":"abc"}"""
        val native = JSONObject().put("activityCreates", 1).put("pageLoads", 1)
        val line = Diagnostics.logLine(page, native)
        // logcat splits on newlines and half a JSON document is not parseable.
        assertFalse(line.contains("\n"))
        assertFalse(line.contains("\r"))
        val parsed = JSONObject(line)
        assertEquals(932, parsed.getJSONObject("page").getInt("innerWidth"))
        assertEquals("abc", parsed.getJSONObject("page").getString("bootId"))
        assertEquals("https://jpfoodmap.com/", parsed.getJSONObject("page").getString("url"))
        assertEquals(1, parsed.getJSONObject("native").getInt("activityCreates"))
        assertFalse(line.contains("?r="))
    }

    @Test
    fun `an unparsable page half still produces one parseable line`() {
        // The probe threw, or the WebView returned something unexpected. The failure text is
        // the interesting part, so it is kept — flattened, so the line stays a line.
        val line = Diagnostics.logLine("Uncaught TypeError\n  at x", JSONObject().put("pageLoads", 0))
        assertFalse(line.contains("\n"))
        val parsed = JSONObject(line)
        assertTrue(parsed.getJSONObject("page").getString("raw").contains("Uncaught TypeError"))
    }

    @Test
    fun `nothing in the log tag or the rendered text leaks a token`() {
        assertEquals("JpfmDiag", Diagnostics.LOG_TAG)
        val text = Diagnostics.showText(
            """{"url":"https://jpfoodmap.com/?r=1&k=secret","ua":"Mozilla/5.0 JpFoodMapApp/2.0.0"}""",
            JSONObject().put("webview", "145.0.0.0"),
        )
        assertFalse(text.contains("secret"))
        assertFalse(text.contains("k="))
    }
}
