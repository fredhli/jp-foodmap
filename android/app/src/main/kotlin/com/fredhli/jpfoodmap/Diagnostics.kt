package com.fredhli.jpfoodmap

import android.app.ActivityManager
import android.app.AlertDialog
import android.app.ApplicationExitInfo
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.graphics.Typeface
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import org.json.JSONTokener

/**
 * "Is the page seeing the geometry I think it is?" answered on the device, with no cable.
 * Half the answer comes from the page ([JS_METRICS] evaluated in the WebView: viewport,
 * safe-area insets, dpr, boot id) and half from the shell (window insets, text zoom,
 * WebView version, how many times the page has loaded, why the process died last time).
 *
 * The boot id is the load-bearing field: it changes only when the document reloads, so
 * comparing two diagnostics across a fold proves the page survived rather than reloaded —
 * which is the whole point of the fold work (STANDARDS §3).
 *
 * The URL is reported WITHOUT its query (STANDARDS §0.4): `?r=` is harmless but the rule is
 * flat, and a diagnostics screen is a thing people screenshot. It is stripped twice, in
 * [JS_METRICS] where the URL is read and again in [showText] where it is rendered — the
 * page half may come back as raw text when the script fails, and the second cut is what
 * covers that case.
 *
 * Two ways in, one body of text (STANDARDS §12): the settings screen's "Diagnostics…" row
 * opens [show], and `am start --ez diagnostics_log true` makes MainActivity print [logLine]
 * to logcat under [LOG_TAG] for `tools/emu.sh diag` to read back. Release builds print it
 * too: there is nothing in it to keep.
 */
object Diagnostics {

    /**
     * Evaluated in the page; returns the JSON described in docs/PLAN.md §5.8 as a *string*,
     * so `evaluateJavascript` hands it back JSON-encoded — see [unquote].
     *
     * Plain ES5 and not one `$` anywhere: this is a Kotlin raw string, where `$` would be a
     * template expression. Everything is wrapped in one try/catch, because a diagnostics
     * probe that throws inside the page returns `null` to the shell and tells us nothing at
     * all about the state we were trying to read.
     *
     * `env` comes from a hidden fixed-position div padded with `env(safe-area-inset-*)` and
     * then measured: there is no other way to read those values, and they are the numbers
     * STANDARDS §1.2 checks the shell's insets pass-through against.
     *
     * `lang` is `document.documentElement.lang`, which the page assigns on every language
     * switch, and NOT `localStorage['tabelog.lang']` — docs/PLAN.md §5.8 offers either, and
     * the DOM one is the live value while the storage one would put a read of a page-owned
     * key into the shell, which is exactly what STANDARDS §0.1 forbids.
     *
     * `fontPx` is beyond §5.8 and is here because it is the page-side half of the text-size
     * check: the shell reports `textZoom`, this reports what the page actually rendered at,
     * and STANDARDS §2.4 is only really verified when the two agree.
     */
    const val JS_METRICS: String = """
        (function () {
          try {
            var d = document.documentElement, cs = getComputedStyle(d);
            function px(v) { var n = parseFloat(v); return isNaN(n) ? 0 : Math.round(n); }
            var host = document.body || d;
            var probe = document.createElement('div');
            probe.style.cssText = 'position:fixed;top:0;left:0;width:0;height:0;visibility:hidden;pointer-events:none;'
              + 'padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px);'
              + 'padding-left:env(safe-area-inset-left,0px);padding-right:env(safe-area-inset-right,0px)';
            host.appendChild(probe);
            var pc = getComputedStyle(probe);
            var env = { t: px(pc.paddingTop), b: px(pc.paddingBottom), l: px(pc.paddingLeft), r: px(pc.paddingRight) };
            host.removeChild(probe);
            var vv = window.visualViewport;
            var sw = false;
            try { sw = !!(navigator.serviceWorker && navigator.serviceWorker.controller); } catch (e2) {}
            var o = {
              innerWidth: window.innerWidth, innerHeight: window.innerHeight,
              outerWidth: window.outerWidth, outerHeight: window.outerHeight,
              dpr: window.devicePixelRatio,
              screen: { w: screen.width, h: screen.height, aw: screen.availWidth, ah: screen.availHeight },
              vv: vv ? { w: Math.round(vv.width), h: Math.round(vv.height), ot: Math.round(vv.offsetTop), s: vv.scale } : null,
              env: env,
              fontPx: px(cs.fontSize),
              bootId: window.__jpfmBootId || null,
              lang: d.getAttribute('lang') || null,
              url: location.origin + location.pathname,
              ua: navigator.userAgent,
              native: !!window.Native,
              sw: sw,
              online: navigator.onLine
            };
            return JSON.stringify(o);
          } catch (e) {
            return JSON.stringify({ error: String(e) });
          }
        })()
    """

    /** logcat tag for the one-line machine-readable dump the emulator scripts read. */
    const val LOG_TAG = "JpfmDiag"

    /** How many process exits the dialog carries. Five is a week of an aggressive phone. */
    const val EXITS_MAX = 5

    /**
     * `evaluateJavascript` hands results back JSON-encoded: a script that returns the string
     * `abc` arrives as `"abc"`, one that returns nothing as `null`. This turns a string
     * result back into the string; anything else (null, a number, garbage) comes back as
     * the raw text so the caller can still show it. MainActivity calls this on the result of
     * [JS_METRICS] before handing it to [show] or [logLine].
     *
     * The leading-quote test is not decoration. `JSONTokener` is lenient about unquoted
     * words — it reads `garbage{` as the string `garbage` and stops at the brace — so
     * tokenising unconditionally would silently truncate exactly the input this is meant to
     * pass through untouched: a WebView that answered with something that is not JSON at
     * all. A JSON string literal always starts with a double quote, so that is the only
     * shape worth decoding.
     */
    fun unquote(raw: String?): String {
        if (raw == null) return "null"
        if (!raw.trimStart().startsWith("\"")) return raw
        return try {
            val v = JSONTokener(raw).nextValue()
            if (v is String) v else raw
        } catch (_: JSONException) {
            raw
        }
    }

    /**
     * Cold/warm start timings, filled as the activity progresses.
     *
     * Process-scoped rather than per-activity, which is why it is an `object` and not four
     * fields on MainActivity: an activity recreation would reset a per-activity clock and
     * report a 200 ms "cold start" for a process that has been up for an hour. Every mark is
     * first-wins, and every value is a `SystemClock.uptimeMillis()` /
     * `Process.getStartUptimeMillis()` reading in the same base — uptime since boot, not
     * wall clock, so a clock change mid-start cannot produce a negative gap. The android
     * calls belong to the CALLER (MainActivity reads both clocks and hands the numbers in),
     * which is what lets [startupJson] be pinned on a plain JVM.
     *
     * `activityCreates` is the field STANDARDS §3.1 turns on: folding and unfolding must not
     * recreate the activity, and "it is still 1 after a fold" is how that is proved.
     */
    object Startup {

        /** No reading yet. 0 is safe as the sentinel: an uptime of exactly 0 is boot itself. */
        private const val UNSET = 0L

        /**
         * A process-to-create gap at or past this means the process was already running when
         * the activity was created, so the numbers describe a warm launch and must not be
         * read as a cold one. This app has no background worker to start its own process
         * (STANDARDS §10.1), so it should never trip — which is exactly why it is reported:
         * a `warmProcess: true` here means something started the process that should not
         * have, and that is worth seeing.
         */
        const val WARM_PROCESS_MS = 30_000L

        @Volatile private var processMs = UNSET
        @Volatile private var createMs = UNSET
        @Volatile private var paintMs = UNSET
        @Volatile private var readyMs = UNSET
        @Volatile private var rendererGone = 0
        @Volatile private var activityCreates = 0

        /**
         * The first `onCreate` of the process wins for both marks; later ones only bump the
         * recreation count.
         *
         * @param processStartUptimeMs `Process.getStartUptimeMillis()`
         * @param uptimeMs `SystemClock.uptimeMillis()`
         */
        @Synchronized
        fun onActivityCreate(processStartUptimeMs: Long, uptimeMs: Long) {
            activityCreates++
            if (processMs == UNSET) processMs = processStartUptimeMs
            if (createMs == UNSET) createMs = uptimeMs
        }

        /** First paint of the first document (`onPageCommitVisible`). First wins. */
        @Synchronized
        fun onFirstPaint(uptimeMs: Long) {
            if (paintMs == UNSET) paintMs = uptimeMs
        }

        /**
         * The page reached READY. First wins, and the answer says whether this call was the
         * first — MainActivity calls `reportFullyDrawn()` exactly then and never again.
         */
        @Synchronized
        fun onReady(uptimeMs: Long): Boolean {
            if (readyMs != UNSET) return false
            readyMs = uptimeMs
            return true
        }

        /**
         * `onRenderProcessGone`. Counted here because it never reaches
         * `ApplicationExitInfo`: the renderer lives in the WebView provider's process, so
         * its death is invisible to [exitsJson], and without this the memory question would
         * be answered with half the data.
         */
        @Synchronized
        fun onRendererGone() {
            rendererGone++
        }

        fun json(): JSONObject =
            startupJson(processMs, createMs, paintMs, readyMs, rendererGone, activityCreates)

        /** Tests only: the process-scoped marks are otherwise write-once for the process. */
        @Synchronized
        internal fun resetForTest() {
            processMs = UNSET
            createMs = UNSET
            paintMs = UNSET
            readyMs = UNSET
            rendererGone = 0
            activityCreates = 0
        }
    }

    /**
     * Pure. The startup block, from four uptime marks (0 = never taken) and the two
     * counters.
     *
     * Each duration is `mark - processMs` (or `readyMs - createMs`), and is JSON null rather
     * than 0 or -1 when either end is missing: a reading that did not happen must not read
     * as an instant one. Contains no URL and no token by construction — every value is a
     * number or a boolean.
     */
    fun startupJson(
        processMs: Long,
        createMs: Long,
        paintMs: Long,
        readyMs: Long,
        rendererGone: Int,
        activityCreates: Int,
    ): JSONObject {
        fun gap(from: Long, to: Long): Any =
            if (from <= 0L || to <= 0L) JSONObject.NULL else to - from
        val toCreate = gap(processMs, createMs)
        return JSONObject()
            .put("processToCreateMs", toCreate)
            .put("processToPaintMs", gap(processMs, paintMs))
            .put("processToReadyMs", gap(processMs, readyMs))
            .put("createToReadyMs", gap(createMs, readyMs))
            .put(
                "warmProcess",
                if (toCreate is Long) toCreate >= Startup.WARM_PROCESS_MS else JSONObject.NULL,
            )
            .put("activityCreates", activityCreates)
            .put("rendererGone", rendererGone)
    }

    /**
     * Pure. `ApplicationExitInfo.REASON_*` as its framework name, or `"REASON_<n>"` for a
     * value this build does not know — a newer platform may add one, and an unknown number
     * in a pasted diagnostics dump is worse than useless.
     *
     * The constants are Java compile-time `static final int`s, so this `when` inlines to
     * plain integers and runs against the stub android.jar in a local unit test.
     */
    fun exitReasonName(reason: Int): String = when (reason) {
        ApplicationExitInfo.REASON_UNKNOWN -> "REASON_UNKNOWN"
        ApplicationExitInfo.REASON_EXIT_SELF -> "REASON_EXIT_SELF"
        ApplicationExitInfo.REASON_SIGNALED -> "REASON_SIGNALED"
        ApplicationExitInfo.REASON_LOW_MEMORY -> "REASON_LOW_MEMORY"
        ApplicationExitInfo.REASON_CRASH -> "REASON_CRASH"
        ApplicationExitInfo.REASON_CRASH_NATIVE -> "REASON_CRASH_NATIVE"
        ApplicationExitInfo.REASON_ANR -> "REASON_ANR"
        ApplicationExitInfo.REASON_INITIALIZATION_FAILURE -> "REASON_INITIALIZATION_FAILURE"
        ApplicationExitInfo.REASON_PERMISSION_CHANGE -> "REASON_PERMISSION_CHANGE"
        ApplicationExitInfo.REASON_EXCESSIVE_RESOURCE_USAGE -> "REASON_EXCESSIVE_RESOURCE_USAGE"
        ApplicationExitInfo.REASON_USER_REQUESTED -> "REASON_USER_REQUESTED"
        ApplicationExitInfo.REASON_USER_STOPPED -> "REASON_USER_STOPPED"
        ApplicationExitInfo.REASON_DEPENDENCY_DIED -> "REASON_DEPENDENCY_DIED"
        ApplicationExitInfo.REASON_OTHER -> "REASON_OTHER"
        ApplicationExitInfo.REASON_FREEZER -> "REASON_FREEZER"
        ApplicationExitInfo.REASON_PACKAGE_STATE_CHANGE -> "REASON_PACKAGE_STATE_CHANGE"
        ApplicationExitInfo.REASON_PACKAGE_UPDATED -> "REASON_PACKAGE_UPDATED"
        else -> "REASON_$reason"
    }

    /**
     * The last [EXITS_MAX] exits of THIS package, newest first:
     * `ActivityManager.getHistoricalProcessExitReasons(packageName, 0, 5)`.
     *
     * Unprivileged for the caller's own package — no permission, no dependency, no new
     * component — which is the whole reason this is the answer to "the app disappeared while
     * I was looking at a restaurant" rather than a crash reporter (STANDARDS §10.1 forbids
     * the components a crash reporter would add).
     *
     * `description`, `status` and `importance` are kept beside the reason because One UI's
     * background limiter reports plenty of kills as `REASON_OTHER`, where the description is
     * the only field that says which limiter did it. Everything here is the system's own
     * text about our own process: no URL, no page content, nothing out of the WebView.
     */
    fun exitsJson(context: Context): JSONArray {
        val out = JSONArray()
        val am = context.getSystemService(ActivityManager::class.java) ?: return out
        val exits = try {
            am.getHistoricalProcessExitReasons(context.packageName, 0, EXITS_MAX)
        } catch (_: SecurityException) {
            // Asking about another package throws; asking about our own should not — but a
            // diagnostics dialog must never be the thing that crashes the app.
            return out
        } catch (_: IllegalArgumentException) {
            return out
        }
        val stamp = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US)
        for (info in exits) {
            out.put(
                JSONObject()
                    .put("reason", exitReasonName(info.reason))
                    .put("at", stamp.format(Date(info.timestamp)))
                    .put("pssKb", info.pss)
                    .put("rssKb", info.rss)
                    .put("status", info.status)
                    .put("importance", info.importance)
                    .put("description", info.description ?: JSONObject.NULL),
            )
        }
        return out
    }

    /**
     * Pure. A URL with its query and fragment removed, for display. [JS_METRICS] already
     * builds `url` out of `origin + pathname`, so this only matters for the paths that do
     * not go through it — a page half that came back as raw text, or a future caller.
     *
     * Routes.stripQuery (T4) is the same rule; this one is deliberately local so that the
     * diagnostics screen does not depend on another file being finished to open at all.
     * If T8 unifies them, this is the copy to delete.
     */
    fun stripQuery(url: String): String = url.substringBefore('?').substringBefore('#')

    /**
     * Pure. The text both [show] and the clipboard carry: the page half and the shell half,
     * each pretty-printed. An unparsable page half is wrapped as `{"raw": …}` rather than
     * dropped — when the probe failed, its failure text is the interesting part.
     */
    fun showText(pageJson: String, native: JSONObject): String =
        "Page\n" + pageObject(pageJson).toString(2) + "\n\nNative\n" + native.toString(2)

    /**
     * One line, tag [LOG_TAG], for `tools/emu.sh diag` to pull out of logcat and hand to
     * `jq`. One JSON object with both halves, and never more than one line: logcat splits on
     * newlines, and half a JSON document is not parseable. `JSONObject.toString()` (no
     * indent) has no newlines of its own and escapes any that appear inside a value.
     */
    fun logLine(pageJson: String, native: JSONObject): String =
        JSONObject()
            .put("page", pageObject(pageJson))
            .put("native", native)
            .toString()

    /**
     * The page half as an object, with `url` cut back to origin + path whatever arrived.
     * Raw text that is not JSON becomes `{"raw": …}` with its newlines flattened, so
     * [logLine] stays on one line even when the probe returned a stack trace.
     */
    private fun pageObject(pageJson: String): JSONObject {
        val page = try {
            JSONObject(pageJson)
        } catch (_: JSONException) {
            return JSONObject().put("raw", pageJson.replace('\n', ' ').replace('\r', ' '))
        }
        if (page.has("url")) page.put("url", stripQuery(page.optString("url")))
        return page
    }

    /**
     * The dialog the settings screen and the diagnostics Intent both open: the two halves in
     * a monospace, selectable, scrolling TextView, with Copy and "Run again" beside Close.
     * Run again matters because the interesting reading is rarely the first one — the insets
     * and the viewport change with the keyboard and with the fold, and the point of the
     * screen is to compare two of them.
     *
     * Returns the shown dialog because the caller owns it: an AlertDialog is a window
     * attached to the activity, and an activity that finishes while one is up leaks it
     * ("Activity has leaked window … that was originally added here"), so MainActivity
     * dismisses it from onDestroy.
     */
    fun show(activity: MainActivity, pageJson: String, native: JSONObject): AlertDialog {
        val text = showText(pageJson, native)
        val pad = (16 * activity.resources.displayMetrics.density).toInt()
        val body = TextView(activity).apply {
            typeface = Typeface.MONOSPACE
            textSize = 12f
            setTextIsSelectable(true)
            setPadding(pad, pad, pad, pad)
            this.text = text
        }
        val scroll = ScrollView(activity).apply { addView(body) }

        val dialog = AlertDialog.Builder(activity)
            .setTitle(R.string.diag_title)
            .setView(scroll)
            .setPositiveButton(R.string.diag_close, null)
            .setNeutralButton(R.string.diag_copy) { _, _ ->
                val cm = activity.getSystemService(ClipboardManager::class.java)
                cm?.setPrimaryClip(ClipData.newPlainText("jpfoodmap diagnostics", text))
                Toast.makeText(activity, R.string.diag_copied, Toast.LENGTH_SHORT).show()
            }
            .setNegativeButton(R.string.diag_again) { _, _ -> activity.runDiagnostics() }
            .create()
        dialog.show()
        return dialog
    }
}
