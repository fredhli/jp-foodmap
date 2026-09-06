package com.fredhli.jpfoodmap

import android.content.pm.PackageManager
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The notification rules, on the JVM, with no Android runtime.
 *
 * These are the cases an emulator run is bad at: a grant revoked between the check and the
 * post, a channel the user muted in system settings, a permission dialog that was dismissed
 * rather than answered, and a second denial (which on Android 13+ never shows a dialog
 * again, so getting the switch's rebound wrong costs the user the feature permanently).
 * Each one is a branch in a pure function in Notifications.kt, and each is pinned here.
 *
 * `Notifications` is an `object`, but nothing in its initialisation touches Android:
 * `PERMISSION` and `PackageManager.PERMISSION_GRANTED` are compile-time constants that the
 * compiler inlines, so no stubbed android.jar method is ever called from this file.
 */
class NotificationsTest {

    // ------------------------------------------------------------------ identity

    /**
     * **The channel id is permanent.** A renamed channel is a NEW channel to Android: every
     * setting the user changed on the old one is stranded on an id nothing posts to, and the
     * new one arrives at its defaults. Same class of mistake as renaming a localStorage key
     * on the site, and this test is here to make it fail loudly rather than ship quietly.
     */
    @Test
    fun channelIdIsTheAgreedOne() {
        assertEquals("jpfoodmap_general", Notifications.CHANNEL_ID)
    }

    /** Fixed id: a second test notification REPLACES the first rather than stacking. */
    @Test
    fun testNotificationIdIsFixed() {
        assertEquals(1, Notifications.TEST_NOTIFICATION_ID)
    }

    @Test
    fun permissionNameIsPostNotifications() {
        assertEquals("android.permission.POST_NOTIFICATIONS", Notifications.PERMISSION)
    }

    // ------------------------------------------------------------------ delivery

    @Test
    fun deliveryPostedWhenChannelOnAndNothingThrown() {
        assertEquals(Notifications.Delivery.POSTED, Notifications.delivery(true, null))
    }

    @Test
    fun deliveryChannelOffWhenTheUserMutedIt() {
        assertEquals(Notifications.Delivery.CHANNEL_OFF, Notifications.delivery(false, null))
    }

    @Test
    fun deliveryNoPermissionOnSecurityException() {
        assertEquals(
            Notifications.Delivery.NO_PERMISSION,
            Notifications.delivery(true, SecurityException("revoked")),
        )
    }

    /**
     * The ordering case, and the reason `delivery` tests the throw first: the channel flag
     * is a reading taken a few instructions earlier, the exception is what actually
     * happened. A muted channel that ALSO threw SecurityException lost the grant, and
     * "CHANNEL_OFF" would send the settings screen to tell the user to unmute a channel
     * that is not the problem.
     */
    @Test
    fun aThrowOutranksAStaleChannelReading() {
        assertEquals(
            Notifications.Delivery.NO_PERMISSION,
            Notifications.delivery(false, SecurityException("revoked")),
        )
        assertEquals(
            Notifications.Delivery.ERROR,
            Notifications.delivery(false, IllegalStateException("boom")),
        )
    }

    @Test
    fun deliveryErrorOnAnythingElse() {
        assertEquals(
            Notifications.Delivery.ERROR,
            Notifications.delivery(true, RuntimeException("boom")),
        )
    }

    // ------------------------------------------------------------------ readiness

    /** The whole 2×2×2 table, so the precedence is stated rather than assumed. */
    @Test
    fun readinessCoversEveryCombination() {
        // The user's own switch outranks everything: with it off we ask for nothing and
        // report nothing, whatever the system thinks.
        assertEquals(Notifications.Readiness.OFF, Notifications.readiness(false, false, false))
        assertEquals(Notifications.Readiness.OFF, Notifications.readiness(false, false, true))
        assertEquals(Notifications.Readiness.OFF, Notifications.readiness(false, true, false))
        assertEquals(Notifications.Readiness.OFF, Notifications.readiness(false, true, true))

        // Switch on, grant missing — the row can offer to ask, so that is what it says even
        // when the app is ALSO blocked at the system level.
        assertEquals(
            Notifications.Readiness.NEEDS_PERMISSION,
            Notifications.readiness(true, false, false),
        )
        assertEquals(
            Notifications.Readiness.NEEDS_PERMISSION,
            Notifications.readiness(true, false, true),
        )

        // Granted but switched off in Android's own settings: nothing this app can ask for
        // fixes it, so the hint has to send the user to system settings.
        assertEquals(Notifications.Readiness.BLOCKED, Notifications.readiness(true, true, false))

        assertEquals(Notifications.Readiness.ON, Notifications.readiness(true, true, true))
    }

    // ------------------------------------------------------------------ the ask

    @Test
    fun weAskOnlyWhenTheSwitchGoesOnWithoutTheGrant() {
        assertTrue(Notifications.shouldRequestPermission(switchOn = true, hasPermission = false))
        assertFalse(Notifications.shouldRequestPermission(switchOn = true, hasPermission = true))
        // Turning it OFF asks for nothing. Neither does anything else in the app: STANDARDS
        // §11.2 is "the settings screen is the only place POST_NOTIFICATIONS is requested",
        // and a cold start must show no dialog at all.
        assertFalse(Notifications.shouldRequestPermission(switchOn = false, hasPermission = false))
        assertFalse(Notifications.shouldRequestPermission(switchOn = false, hasPermission = true))
    }

    // ------------------------------------------------------------------ the rebound

    /**
     * Denied → the switch springs back off (STANDARDS §11.2). Leaving it on would be the app
     * claiming a state Android just refused, and every later "send a test notification"
     * would fail against a row that reads as on.
     */
    @Test
    fun switchReboundsOffWhenTheUserDenies() {
        assertFalse(Notifications.switchAfterPermissionResult(requestedOn = true, granted = false))
        assertTrue(Notifications.switchAfterPermissionResult(requestedOn = true, granted = true))
        // A result arriving for a switch that is no longer on cannot turn it back on.
        assertFalse(Notifications.switchAfterPermissionResult(requestedOn = false, granted = true))
        assertFalse(Notifications.switchAfterPermissionResult(requestedOn = false, granted = false))
    }

    @Test
    fun grantResultsAreReadStrictly() {
        assertTrue(Notifications.permissionGranted(intArrayOf(PackageManager.PERMISSION_GRANTED)))
        assertFalse(Notifications.permissionGranted(intArrayOf(PackageManager.PERMISSION_DENIED)))
        // An EMPTY array is Android saying the request was cancelled — the dialog was
        // dismissed, or the activity was interrupted. A cancellation is not a grant, and
        // reading grantResults[0] off it would also crash.
        assertFalse(Notifications.permissionGranted(intArrayOf()))
    }

    /** The two halves as the settings screen actually chains them. */
    @Test
    fun theSettingsScreensWholePath() {
        val granted = Notifications.permissionGranted(intArrayOf(PackageManager.PERMISSION_DENIED))
        val switch = Notifications.switchAfterPermissionResult(true, granted)
        assertFalse(switch)
        assertEquals(
            Notifications.Readiness.OFF,
            Notifications.readiness(switch, hasPermission = false, systemEnabled = true),
        )
    }
}
