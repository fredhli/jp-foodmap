package com.fredhli.jpfoodmap

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The shell's three settings, read back out of storage. Plain JVM: everything tested here
 * is free of `android.*`, which is the point — the stub android.jar in a local unit test
 * throws from every method, so a rule that has to be tested cannot live on the Android side.
 *
 * What these protect: a preferences file written by an older build, or edited by hand, must
 * never make the app unusable; and "follow the system font scale" must be the multiplication
 * WebView does not do for us (STANDARDS §2.4).
 */
class ShellPrefsTest {

    // ---------------------------------------------------------------- link policy

    @Test
    fun `link policy round trips every stored value`() {
        assertEquals(LinkPolicy.CUSTOM_TAB, LinkPolicy.fromStorage("custom_tab"))
        assertEquals(LinkPolicy.CHROME, LinkPolicy.fromStorage("chrome"))
        assertEquals(LinkPolicy.SYSTEM, LinkPolicy.fromStorage("system"))
        // Every enum's own storageValue must map back to it — the check that catches a
        // renamed constant whose stored string was not renamed with it.
        for (policy in LinkPolicy.entries) {
            assertEquals(policy, LinkPolicy.fromStorage(policy.storageValue))
        }
    }

    @Test
    fun `the stored strings are the on-disk format and are pinned here`() {
        // These three strings are what lands in jpfm_shell.xml. Changing one silently
        // resets every install that had chosen it, which is why they are asserted literally
        // and not derived from the enum names.
        assertEquals("custom_tab", LinkPolicy.CUSTOM_TAB.storageValue)
        assertEquals("chrome", LinkPolicy.CHROME.storageValue)
        assertEquals("system", LinkPolicy.SYSTEM.storageValue)
        assertEquals("jpfm_shell", ShellPrefs.PREFS_NAME)
        assertEquals("link_policy", ShellPrefs.KEY_LINK_POLICY)
        assertEquals("text_zoom", ShellPrefs.KEY_TEXT_ZOOM)
        assertEquals("notify_enabled", ShellPrefs.KEY_NOTIFY_ENABLED)
    }

    @Test
    fun `an unknown or missing link policy is the custom tab default`() {
        // docs/PLAN.md D7: looking at a Tabelog page is "step out and come back", and the
        // Custom Tab's back arrow is the one-tap way back.
        assertEquals(LinkPolicy.CUSTOM_TAB, LinkPolicy.DEFAULT)
        assertEquals(LinkPolicy.CUSTOM_TAB, LinkPolicy.fromStorage(null))
        assertEquals(LinkPolicy.CUSTOM_TAB, LinkPolicy.fromStorage(""))
        assertEquals(LinkPolicy.CUSTOM_TAB, LinkPolicy.fromStorage("firefox"))
        assertEquals(LinkPolicy.CUSTOM_TAB, LinkPolicy.fromStorage("chrome browser"))
        // The dashboard shell's third value; a preferences file copied between the two apps
        // must degrade to the default rather than throw.
        assertEquals(LinkPolicy.CUSTOM_TAB, LinkPolicy.fromStorage("default_browser"))
        // Trimmed and case-folded, so these resolve rather than falling back.
        assertEquals(LinkPolicy.CHROME, LinkPolicy.fromStorage("  CHROME "))
        assertEquals(LinkPolicy.CUSTOM_TAB, LinkPolicy.fromStorage("  Custom_Tab  "))
    }

    // ---------------------------------------------------------------- text zoom

    @Test
    fun `clamp zoom keeps the system sentinel and pins the rest`() {
        assertEquals(0, ShellPrefs.clampZoom(0)) // 0 is "follow the system", not a percent
        assertEquals(50, ShellPrefs.clampZoom(49))
        assertEquals(50, ShellPrefs.clampZoom(50))
        assertEquals(200, ShellPrefs.clampZoom(200))
        assertEquals(200, ShellPrefs.clampZoom(201))
        assertEquals(115, ShellPrefs.clampZoom(115))
        assertEquals(50, ShellPrefs.clampZoom(-300))
    }

    @Test
    fun `effective text zoom follows the font scale only when the pref is system`() {
        assertEquals(100, ShellPrefs.effectiveTextZoom(0, 1f))
        assertEquals(115, ShellPrefs.effectiveTextZoom(0, 1.15f))
        // STANDARDS §2.4's acceptance: `settings put system font_scale 1.3` -> textZoom 130.
        assertEquals(130, ShellPrefs.effectiveTextZoom(0, 1.3f))
        // ...and picking 100 on the settings screen pins it there whatever the system says.
        assertEquals(100, ShellPrefs.effectiveTextZoom(100, 1.3f))
        assertEquals(130, ShellPrefs.effectiveTextZoom(130, 1f))
        assertEquals(90, ShellPrefs.effectiveTextZoom(90, 2f))
        // One UI's largest accessibility scales overshoot; the clamp is the guard.
        assertEquals(200, ShellPrefs.effectiveTextZoom(0, 3f))
        assertEquals(50, ShellPrefs.effectiveTextZoom(0, 0.1f))
        // Rounding, not truncation: 1.149 is 115, not 114.
        assertEquals(115, ShellPrefs.effectiveTextZoom(0, 1.149f))
    }

    @Test
    fun `the offered choices are exactly the ones the settings screen can store`() {
        assertEquals(listOf(0, 90, 95, 100, 115, 130), ShellPrefs.TEXT_ZOOM_CHOICES)
        assertEquals(ShellPrefs.TEXT_ZOOM_SYSTEM, ShellPrefs.TEXT_ZOOM_CHOICES.first())
        for (choice in ShellPrefs.TEXT_ZOOM_CHOICES) {
            // Nothing the screen offers may be moved by the clamp on the way to disk.
            assertEquals(choice, ShellPrefs.clampZoom(choice))
        }
        // The rungs are offered in the order the radio group draws them, and no rung repeats
        // — AppSettingsActivity.ZOOM_ROWS maps position to a view id by hand.
        assertEquals(ShellPrefs.TEXT_ZOOM_CHOICES.sorted(), ShellPrefs.TEXT_ZOOM_CHOICES)
        assertEquals(ShellPrefs.TEXT_ZOOM_CHOICES.distinct(), ShellPrefs.TEXT_ZOOM_CHOICES)
        assertEquals(6, ShellPrefs.TEXT_ZOOM_CHOICES.size)
    }

    @Test
    fun `a pinned rung ignores the system scale entirely`() {
        // That is what pinning means, and it is the half of §2.4 the emulator check proves
        // second: set the system to 1.3, choose 100, and the page must be at 100.
        for (rung in ShellPrefs.TEXT_ZOOM_CHOICES.filter { it != ShellPrefs.TEXT_ZOOM_SYSTEM }) {
            assertEquals(rung, ShellPrefs.effectiveTextZoom(rung, 1f))
            assertEquals(rung, ShellPrefs.effectiveTextZoom(rung, 1.3f))
            assertEquals(rung, ShellPrefs.effectiveTextZoom(rung, 0.85f))
        }
    }

    // ---------------------------------------------------------------- defaults

    @Test
    fun `the defaults are what a fresh install gets`() {
        val defaults = ShellPrefs()
        assertEquals(LinkPolicy.CUSTOM_TAB, defaults.linkPolicy)
        assertEquals(ShellPrefs.TEXT_ZOOM_SYSTEM, defaults.textZoom)
        // Off, because there is no server that pushes anything to this app (D10): a switch
        // defaulted on would ask for a runtime permission for a feature with no source.
        assertFalse(ShellPrefs.NOTIFY_DEFAULT)
        assertFalse(defaults.notifyEnabled)
        // And a stored true is a real setting, not a re-defaulted one.
        assertTrue(ShellPrefs(notifyEnabled = true).notifyEnabled)
    }
}
