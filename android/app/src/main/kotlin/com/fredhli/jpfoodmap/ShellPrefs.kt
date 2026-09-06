package com.fredhli.jpfoodmap

import android.content.Context
import androidx.core.content.edit
import kotlin.math.roundToInt

/**
 * The shell's three preferences. SharedPreferences, not DataStore (docs/PLAN.md D14): one
 * writer, three keys, no coroutines needed for it.
 *
 * NOTE FOR EVERY LATER TASK: the shell stores NOTHING ELSE. Favourites, blacklist,
 * bookmarks, language, filters and map position are the page's, in its own localStorage and
 * in the Worker's KV, and the shell must never read or write them (STANDARDS §0.1, and the
 * repo's CLAUDE.md red lines).
 *
 * Everything except [ShellPrefs.load] / [ShellPrefs.save] is deliberately free of
 * `android.*`: local unit tests run against the stub `android.jar` whose every method
 * throws, so any rule that has to be *tested* — and these are the settings whose wrong
 * value silently ruins the page — must be expressible in plain Kotlin. The Android side
 * (the settings screen, the WebView's textZoom) only ever calls in here; it never
 * re-implements a rule.
 *
 * The `storageValue` strings are the on-disk format: SharedPreferences keeps whatever an
 * older build wrote, so they must never change once shipped, and [LinkPolicy.fromStorage]
 * is the single gate that turns a stored string back into an enum. It is deliberately
 * forgiving (trimmed, case-insensitive, unknown → the default) because the alternative —
 * throwing on an unexpected value — turns a hand-edited or downgraded preferences file into
 * an app that cannot open its own settings screen.
 */
enum class LinkPolicy(val storageValue: String) {
    /** Chrome Custom Tab — the default (docs/PLAN.md D7). */
    CUSTOM_TAB("custom_tab"),

    /** Chrome proper, a separate task. */
    CHROME("chrome"),

    /** Whatever the system default browser is. */
    SYSTEM("system");

    companion object {
        val DEFAULT = CUSTOM_TAB

        fun fromStorage(value: String?): LinkPolicy =
            entries.firstOrNull { it.storageValue == value?.trim()?.lowercase() } ?: DEFAULT
    }
}

data class ShellPrefs(
    val linkPolicy: LinkPolicy = LinkPolicy.DEFAULT,
    val textZoom: Int = TEXT_ZOOM_SYSTEM,
    val notifyEnabled: Boolean = NOTIFY_DEFAULT,
) {
    companion object {
        const val PREFS_NAME = "jpfm_shell"
        const val KEY_LINK_POLICY = "link_policy"
        const val KEY_TEXT_ZOOM = "text_zoom"
        const val KEY_NOTIFY_ENABLED = "notify_enabled"

        /** 0 = follow the system font scale, which is the default. */
        const val TEXT_ZOOM_SYSTEM = 0

        /** Off: there is no push source yet, so nothing would ever arrive (D10). */
        const val NOTIFY_DEFAULT = false

        /**
         * What the settings screen offers, in the order it draws them. Six rungs and not a
         * slider: `textZoom` reflows the page, so the useful move is "a notch bigger than
         * the system" and not a continuous dial nobody can place twice. Copied from the
         * dashboard shell, where these six were settled on a real Fold 8.
         */
        val TEXT_ZOOM_CHOICES = listOf(TEXT_ZOOM_SYSTEM, 90, 95, 100, 115, 130)

        /**
         * The band a percentage is pinned into. WebView accepts far wilder numbers and
         * turns the page into either unreadable glyphs or one word per line, and the map's
         * own controls stop fitting their buttons well before either end.
         */
        private const val ZOOM_MIN = 50
        private const val ZOOM_MAX = 200

        /**
         * Read the three values. A missing file, a missing key or a value of the wrong type
         * all end up at the default rather than throwing: this is called on the WebView's
         * setup path, and a shell that cannot start because a preference is malformed is
         * strictly worse than one that quietly uses its defaults.
         *
         * `getInt` on a key some other build stored as a String throws ClassCastException,
         * which is the one realistic corruption here (a hand-edited XML), hence the catch.
         */
        fun load(context: Context): ShellPrefs {
            val sp = context.applicationContext
                .getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            val policy = LinkPolicy.fromStorage(
                runCatching { sp.getString(KEY_LINK_POLICY, null) }.getOrNull(),
            )
            val zoom = clampZoom(
                runCatching { sp.getInt(KEY_TEXT_ZOOM, TEXT_ZOOM_SYSTEM) }.getOrDefault(TEXT_ZOOM_SYSTEM),
            )
            val notify = runCatching { sp.getBoolean(KEY_NOTIFY_ENABLED, NOTIFY_DEFAULT) }
                .getOrDefault(NOTIFY_DEFAULT)
            return ShellPrefs(linkPolicy = policy, textZoom = zoom, notifyEnabled = notify)
        }

        /**
         * Write all three as one edit. `apply()` rather than `commit()`: the settings screen
         * saves on every tap, the values are re-read from the in-memory map immediately
         * either way, and the disk write must not block the UI thread on a screen the user
         * is still touching. The three keys are always written together, so a caller that
         * builds a [ShellPrefs] from only part of the screen would silently reset the rest —
         * which is why AppSettingsActivity re-reads every control before it saves.
         */
        fun save(context: Context, prefs: ShellPrefs) {
            context.applicationContext
                .getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .edit {
                    putString(KEY_LINK_POLICY, prefs.linkPolicy.storageValue)
                    putInt(KEY_TEXT_ZOOM, clampZoom(prefs.textZoom))
                    putBoolean(KEY_NOTIFY_ENABLED, prefs.notifyEnabled)
                }
        }

        /**
         * Pure. 0 stays 0 (the "follow the system" sentinel); anything else is pinned into
         * [ZOOM_MIN]..[ZOOM_MAX].
         */
        fun clampZoom(v: Int): Int = if (v == TEXT_ZOOM_SYSTEM) TEXT_ZOOM_SYSTEM else v.coerceIn(ZOOM_MIN, ZOOM_MAX)

        /**
         * WebView does not follow the system font scale on its own; the shell multiplies it
         * in. Returns the textZoom percentage to set (STANDARDS §2.4).
         *
         * `textZoom` defaults to 100 whatever the phone's font-size setting says, so without
         * this the map would be the one surface on a Fold running a non-default font size
         * that ignores it. Zero is a distinct stored state rather than "100" precisely so it
         * can be resolved late, here, against the live `Configuration.fontScale` — which is
         * what makes the setting keep following the slider after it has moved.
         */
        fun effectiveTextZoom(pref: Int, fontScale: Float): Int =
            if (pref == TEXT_ZOOM_SYSTEM) (fontScale * 100).roundToInt().coerceIn(ZOOM_MIN, ZOOM_MAX)
            else clampZoom(pref)
    }
}
