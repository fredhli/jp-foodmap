package com.fredhli.jpfoodmap

import android.app.Activity
import android.os.Build
import android.content.Intent
import android.provider.Settings
import android.os.Bundle
import android.view.MenuItem
import android.view.View
import android.widget.Button
import android.widget.CompoundButton
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast
import androidx.core.net.toUri
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.webkit.WebViewCompat

/**
 * The shell's settings: outbound-link policy, text size, the notification switch and its
 * test button, "open in browser", diagnostics, and an About line with the version. Reached
 * from the page's menu through the bridge, from the launcher shortcut, and from the system's
 * app-info screen (ACTION_APPLICATION_PREFERENCES).
 *
 * Nothing here touches anything the page owns — no favourites, no language, no filters
 * (STANDARDS §0.1). The three values it writes are the three in [ShellPrefs] and nothing
 * else, and every one of them is a per-device preference whose loss costs a cosmetic reset.
 *
 * **This screen is the only place the app ever asks for POST_NOTIFICATIONS** (STANDARDS
 * §11.2). Not at launch, not from the page, not from anything in the background: the grant
 * is asked for at the moment the switch is turned on, which is the one moment the request
 * has an answerable reason attached to it.
 *
 * A plain `android.app.Activity` with a framework layout. There is no PreferenceFragment and
 * no AppCompat here: the shell owns three settings and three buttons, and a preferences
 * library to draw them would cost more APK than the shell itself. The activity keeps the
 * platform action bar its theme gives it (Theme.Jpfm.Settings), which is where the screen
 * title and the up arrow come from.
 *
 * Every tap saves immediately — there is no Save button, so there is no state that backing
 * out can lose and no half-applied screen. MainActivity re-reads [ShellPrefs] when it comes
 * back to the foreground, which is what makes a text-size change visible without a reload.
 */
class AppSettingsActivity : Activity() {

    private lateinit var linkGroup: RadioGroup
    private lateinit var zoomGroup: RadioGroup
    private lateinit var notifySwitch: Switch
    private lateinit var notifyHint: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_app_settings)
        actionBar?.setDisplayHomeAsUpEnabled(true)

        // targetSdk 36: every window is edge-to-edge and there is no opt-out, and a plain
        // framework theme pads nothing for it — the last button would sit under the gesture
        // bar. The bars (and the cutout, because the manifest's cutout mode is ALWAYS)
        // become padding on the ScrollView rather than on the column inside it: that keeps
        // the bar regions painted with the window background while the content scrolls
        // between them. The insets are passed through, not consumed — nothing below needs
        // them, but the rule is one rule everywhere in this app (STANDARDS §1.2).
        val root = findViewById<View>(R.id.settings_root)
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout(),
            )
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }

        linkGroup = findViewById(R.id.link_policy_group)
        zoomGroup = findViewById(R.id.text_zoom_group)
        notifySwitch = findViewById(R.id.notify_enabled)
        notifyHint = findViewById(R.id.notify_hint)
        val notifyTest: Button = findViewById(R.id.notify_test)
        val openBrowser: Button = findViewById(R.id.open_browser)
        val openDiagnostics: Button = findViewById(R.id.open_diagnostics)
        val about: TextView = findViewById(R.id.about)

        // The percentage labels are formatted here rather than written into the layout, so
        // the numbers exist once (in ZOOM_ROWS, mirroring ShellPrefs.TEXT_ZOOM_CHOICES) and
        // a locale that formats digits differently gets its own numerals.
        for ((id, percent) in ZOOM_ROWS) {
            if (percent == ShellPrefs.TEXT_ZOOM_SYSTEM) continue // its label is static
            findViewById<RadioButton>(id).text = getString(R.string.settings_zoom_pct, percent)
        }

        // Initial state BEFORE the listeners are attached: RadioGroup.check() fires
        // onCheckedChanged, and a listener already in place would write back the value it
        // just read on every open.
        val prefs = ShellPrefs.load(this)
        linkGroup.check(
            when (prefs.linkPolicy) {
                LinkPolicy.CUSTOM_TAB -> R.id.link_custom_tab
                LinkPolicy.CHROME -> R.id.link_chrome
                LinkPolicy.SYSTEM -> R.id.link_system
            },
        )
        // A stored zoom that is not one of the six rungs is only reachable by editing the
        // preferences file by hand; it shows as "Follow system" and the next tap corrects it.
        zoomGroup.check(ZOOM_ROWS.firstOrNull { it.second == prefs.textZoom }?.first ?: R.id.zoom_system)
        notifySwitch.isChecked = prefs.notifyEnabled

        val save = RadioGroup.OnCheckedChangeListener { _, _ -> saveCurrent() }
        linkGroup.setOnCheckedChangeListener(save)
        zoomGroup.setOnCheckedChangeListener(save)
        notifySwitch.setOnCheckedChangeListener(notifyListener)

        // The proof that the plumbing works, since there is no server that pushes anything
        // (docs/PLAN.md D10). postTest() answers whether the entry really reached the shade
        // and not merely whether the call returned: it runs Notifications.canPost() itself,
        // which is the runtime grant plus the app's system-level notification switch.
        //
        // The app's OWN switch is tested here rather than there, because it is this screen's
        // model of the feature: a phone where Android would allow the post but the user has
        // this switch off must not receive one, and the failure string is already the row
        // that says so. Both halves of the && therefore have a hint above explaining them.
        notifyTest.setOnClickListener {
            val posted = notifySwitch.isChecked && Notifications.postTest(this)
            Toast.makeText(
                this,
                if (posted) R.string.settings_notify_test_sent else notifyFailureString(),
                Toast.LENGTH_SHORT,
            ).show()
            refreshNotifyRow()
        }

        findViewById<Button>(R.id.notify_system_settings).setOnClickListener {
            val channelOff = Notifications.canPost(this) && !Notifications.channelOn(this)
            val settings = Intent(if (channelOff) Settings.ACTION_CHANNEL_NOTIFICATION_SETTINGS else Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                .putExtra(Settings.EXTRA_APP_PACKAGE, packageName)
            if (channelOff) settings.putExtra(Settings.EXTRA_CHANNEL_ID, Notifications.CHANNEL_ID)
            try { startActivity(settings) } catch (_: android.content.ActivityNotFoundException) {
                startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, "package:$packageName".toUri()))
            }
        }

        // Deliberately NOT "load the site in the WebView": this row exists for the things
        // that have to happen outside it. Google refuses OAuth inside a WebView
        // (disallowed_useragent), so signing in on a phone where the native path is not
        // configured yet means signing in on the site in a real browser (STANDARDS §7.4).
        // allowSelf = false is what stops App Links from bouncing the URL straight back
        // into this app.
        openBrowser.setOnClickListener {
            if (!Links.openInBrowser(this, Routes.BASE_URL, currentLinkPolicy())) {
                Toast.makeText(this, R.string.links_no_handler, Toast.LENGTH_SHORT).show()
            }
        }

        // Diagnostics lives in the shell because half of what it reports (the viewport, the
        // safe-area insets, the WebView version, the effective text zoom) only exists once a
        // page is loaded. MainActivity runs it as soon as the page is READY.
        openDiagnostics.setOnClickListener {
            startActivity(MainActivity.diagnosticsIntent(this))
        }

        // App version, then the WebView package version the page actually runs on. The
        // second number is the one worth having: how the shell behaves around insets and the
        // keyboard is decided by it, and WebView updates itself out from under the app
        // through the Play Store. "?" rather than a crash when no provider is resolvable —
        // this screen must open on a broken device too.
        val webViewVersion = WebViewCompat.getCurrentWebViewPackage(this)?.versionName ?: "?"
        about.text = getString(R.string.settings_about, BuildConfig.VERSION_NAME, webViewVersion)

        refreshNotifyRow()
    }

    /**
     * The grant can change while this screen is in the background — it can be revoked in
     * Android settings, or notifications turned off for the app entirely — so the hint is
     * re-derived on every resume rather than only in onCreate.
     */
    override fun onResume() {
        super.onResume()
        refreshNotifyRow()
    }

    /** The action bar's up arrow. Nothing to unwind: this screen is a leaf. */
    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        if (item.itemId == android.R.id.home) {
            finish()
            return true
        }
        return super.onOptionsItemSelected(item)
    }

    /**
     * Turned on: create the channel so it appears in system settings immediately, and ask
     * Android for the grant when this is API 33+ and we do not have it. Turned off: nothing
     * to ask; the preference alone stops every post. Either way the value is saved first —
     * the request is asynchronous and the preference is not waiting on it.
     */
    private val notifyListener = CompoundButton.OnCheckedChangeListener { _, checked ->
        saveCurrent()
        if (checked) {
            Notifications.ensureChannel(this)
            // The SDK test comes first and is not redundant with hasPermission()'s own: it
            // is what says out loud that POST_NOTIFICATIONS does not exist below 33 and must
            // not be requested there, without depending on how Notifications happens to
            // answer for a platform that has no such permission. The "may we ask at all"
            // rule itself lives in Notifications, where NotificationsTest pins it — this
            // screen is the only caller, so a copy here would be an untested second rule.
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
                Notifications.shouldRequestPermission(checked, Notifications.hasPermission(this))
            ) {
                requestPermissions(arrayOf(Notifications.PERMISSION), REQ_NOTIFY)
            }
        }
        refreshNotifyRow()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQ_NOTIFY) return
        // Both rules are Notifications' (pinned by NotificationsTest), not second copies
        // here: an empty grantResults is Android saying the dialog was cancelled and is not
        // a grant, and where the switch lands is `requestedOn && granted` — the request only
        // ever goes out with the switch on, so a denial is the only way this comes back false.
        val granted = Notifications.permissionGranted(grantResults)
        if (!Notifications.switchAfterPermissionResult(requestedOn = true, granted = granted)) {
            // Bounce the switch back, with the listener DETACHED: setting isChecked fires
            // onCheckedChanged, and the listener would re-enter the request path — on
            // Android 13+ a second denial is permanent, so the re-entry would burn the one
            // remaining ask on a dialog the system no longer shows (STANDARDS §11.2).
            notifySwitch.setOnCheckedChangeListener(null)
            notifySwitch.isChecked = false
            notifySwitch.setOnCheckedChangeListener(notifyListener)
            saveCurrent()
        }
        refreshNotifyRow()
    }

    /**
     * The hint follows all four notification gates, including the channel setting.
     */
    private fun refreshNotifyRow() {
        // Notifications.readiness owns the state precedence (pinned by NotificationsTest);
        // this screen only maps its answer onto a string. canPost() is hasPermission() AND
        // the system switch, and readiness only reaches its third argument once the second
        // is true — so passing it as `systemEnabled` is exactly the system switch.
        val readiness = Notifications.readiness(
            switchOn = notifySwitch.isChecked,
            hasPermission = Notifications.hasPermission(this),
            systemEnabled = Notifications.canPost(this),
            channelEnabled = Notifications.channelOn(this),
        )
        notifyHint.setText(
            when (readiness) {
                Notifications.Readiness.OFF -> R.string.settings_notify_hint_off
                Notifications.Readiness.NEEDS_PERMISSION -> R.string.settings_notify_hint_needs_permission
                Notifications.Readiness.BLOCKED -> R.string.settings_notify_hint_blocked
                Notifications.Readiness.CHANNEL_OFF -> R.string.settings_notify_hint_channel_off
                Notifications.Readiness.ON -> R.string.settings_notify_hint_on
            },
        )
    }

    private fun notifyFailureString(): Int = when {
        !notifySwitch.isChecked -> R.string.settings_notify_hint_off
        !Notifications.hasPermission(this) -> R.string.settings_notify_hint_needs_permission
        !Notifications.canPost(this) -> R.string.settings_notify_hint_blocked
        !Notifications.channelOn(this) -> R.string.settings_notify_hint_channel_off
        else -> R.string.settings_notify_test_failed
    }

    /** The policy the radio group currently shows, without a round trip through storage. */
    private fun currentLinkPolicy(): LinkPolicy = when (linkGroup.checkedRadioButtonId) {
        R.id.link_chrome -> LinkPolicy.CHROME
        R.id.link_system -> LinkPolicy.SYSTEM
        else -> LinkPolicy.CUSTOM_TAB
    }

    /**
     * Read every control and write all three values as one edit.
     *
     * Every control on the screen is read here and not just the one that changed, because
     * [ShellPrefs.save] writes the whole record: leaving the switch out would save
     * `notifyEnabled = false` on every tap of a link or text-size row and silently turn the
     * feature off.
     */
    private fun saveCurrent() {
        ShellPrefs.save(
            this,
            ShellPrefs(
                linkPolicy = currentLinkPolicy(),
                // -1 (nothing checked) cannot happen once onCreate has run, but it maps to
                // the system default rather than throwing if it ever does.
                textZoom = ZOOM_ROWS.firstOrNull { it.first == zoomGroup.checkedRadioButtonId }?.second
                    ?: ShellPrefs.TEXT_ZOOM_SYSTEM,
                notifyEnabled = notifySwitch.isChecked,
            ),
        )
    }

    private companion object {
        /** requestPermissions code for POST_NOTIFICATIONS; this screen makes one request. */
        const val REQ_NOTIFY = 1

        /**
         * Radio id → stored percent, in the order the layout draws them. Mirrors
         * `ShellPrefs.TEXT_ZOOM_CHOICES`; it cannot be derived from it, because a view id is
         * a compile-time constant and there is no generating a `@+id` at run time. The two
         * lists move together — adding a rung means a RadioButton, an id, and a line here.
         */
        val ZOOM_ROWS = listOf(
            R.id.zoom_system to ShellPrefs.TEXT_ZOOM_SYSTEM,
            R.id.zoom_90 to 90,
            R.id.zoom_95 to 95,
            R.id.zoom_100 to 100,
            R.id.zoom_115 to 115,
            R.id.zoom_130 to 130,
        )
    }
}
