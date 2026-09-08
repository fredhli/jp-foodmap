package com.fredhli.jpfoodmap

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat

/**
 * One channel, one test notification, one permission. There is no server that pushes
 * anything to this app yet (docs/PLAN.md D10), so what ships is the plumbing and a way to
 * prove it works: the channel exists, POST_NOTIFICATIONS is asked for at the moment the
 * user turns the switch on (never at launch), and "send a test notification" posts one.
 *
 * Deliberately absent: FCM, any Service, any BroadcastReceiver, any scheduled work. The
 * power audit in STANDARDS §10 asserts their absence, and `tools/power-audit.sh` is the
 * command that does it — nothing in this file may grow a component, a wakelock, or a
 * background wakeup without that audit going red first.
 *
 * **Why the decisions live in pure functions.** Four of the five things this file knows —
 * what a `notify()` outcome means, what the settings row should read, whether to ask for
 * the permission at all, and where the switch lands after the system dialog answers — are
 * rules, not Android calls. They are `internal` functions over plain values so
 * `NotificationsTest` can pin them on the JVM, because the interesting cases (the grant
 * revoked between the check and the post, the channel muted in system settings, a second
 * denial that Android will never show a dialog for again) are precisely the ones an
 * emulator run does not reliably reproduce.
 *
 * **The T5 contract.** The settings screen owns the switch, the request and the strings
 * around it; this file owns the channel, the post, and the rules above. T5 calls, in this
 * order: [shouldRequestPermission] before asking, [ensureChannel] when the switch goes on,
 * [switchAfterPermissionResult] in `onRequestPermissionsResult` (the "denied → the switch
 * springs back off" rule), [readiness] to pick the hint under the row, and [postTest] for
 * the button. It must never re-implement any of them.
 */
object Notifications {

    /** logcat tag. Nothing logged here carries a URL, a title or a token (STANDARDS §0.4). */
    private const val TAG = "JpfmNotify"

    /**
     * The one channel. **The id is permanent.** A renamed channel is a new channel to
     * Android: everything the user changed on the old one (importance, sound, whether it
     * shows on the lock screen) stays behind on an id nothing posts to any more, and the
     * new one arrives at its defaults. Same rule as the site's localStorage keys.
     */
    const val CHANNEL_ID = "jpfoodmap_general"

    /** Fixed id: a test notification replaces the previous one instead of stacking. */
    const val TEST_NOTIFICATION_ID = 1

    /**
     * PendingIntent request code for the test entry's tap. Constant for the same reason the
     * notification id is: one live intent, replaced rather than accumulated. It differs from
     * [TEST_NOTIFICATION_ID] on purpose — they index different tables, and reusing one
     * number for both is how a second kind of notification later ends up delivering the
     * first kind's intent.
     */
    private const val TEST_REQUEST_CODE = 2001

    /**
     * The runtime permission's name, so the settings screen does not have to import
     * [Manifest] and guard it by API level itself. A compile-time constant, so reading it
     * costs nothing and works below API 33 (where the permission simply does not exist and
     * the system grants notifications by default).
     */
    const val PERMISSION: String = Manifest.permission.POST_NOTIFICATIONS

    /** What became of one entry. [POSTED] is the only one that means the shade has it. */
    enum class Delivery {
        /** `notify()` returned and nothing this side knows of dropped it. */
        POSTED,

        /** The runtime grant was gone by the time `notify()` ran — see [post]. */
        NO_PERMISSION,

        /** The person has this channel switched off in system settings. */
        CHANNEL_OFF,

        /** `notify()` raised something else. The entry is not in the shade either way. */
        ERROR,
    }

    /**
     * What the settings row should say, including the channel setting that decides whether anything
     * can actually reach the shade. They are genuinely three: the app's own switch, the
     * runtime grant, and the user's system-level switch for this app — and a person who
     * turned the app off in Android's settings while leaving our switch on is a real state
     * that must not read as "on".
     */
    enum class Readiness {
        /** The app's own switch is off. Nothing is asked for and nothing is posted. */
        OFF,

        /** The switch is on but the runtime grant is missing — the row offers to ask again. */
        NEEDS_PERMISSION,

        /** Granted, but notifications for this app are off in system settings. */
        BLOCKED,

        /** This app is allowed, but its only channel is disabled. */
        CHANNEL_OFF,

        /** Everything says yes. */
        ON,
    }

    /**
     * Create the channel if it is not there. Idempotent — `createNotificationChannel` on an
     * existing id updates the name and description and leaves everything the user has
     * changed (importance, sound, badge) alone, which is the correct behaviour: once he has
     * silenced it in system settings that is his decision, not ours to re-apply.
     *
     * **Called only when the switch goes on, never at startup.** The channel appearing in
     * Android's settings is itself a promise that the app has something to say; a cold start
     * that creates it would put a row there for an app that has never posted anything, and
     * STANDARDS §11.2's acceptance is literally "no channel until the switch is on".
     *
     * IMPORTANCE_DEFAULT with the sound removed and vibration off: default importance is
     * what makes an entry a normal shade entry rather than a minimised one, and the silence
     * is set here — at creation, where the user can still override it — rather than by
     * choosing a lower importance he cannot raise.
     */
    fun ensureChannel(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        val channel = NotificationChannel(
            CHANNEL_ID,
            context.getString(R.string.notify_channel_name),
            NotificationManager.IMPORTANCE_DEFAULT,
        )
        channel.description = context.getString(R.string.notify_channel_desc)
        // No launcher dot. The app has no unread state and never will from a notification
        // that only ever exists because someone pressed "send a test notification".
        channel.setShowBadge(false)
        channel.enableVibration(false)
        channel.setSound(null, null)
        manager.createNotificationChannel(channel)
    }

    /**
     * Just the runtime grant. The settings screen asks about this one; below API 33 the
     * permission does not exist and the answer is always yes.
     */
    fun hasPermission(context: Context): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            ContextCompat.checkSelfPermission(context, PERMISSION) ==
            PackageManager.PERMISSION_GRANTED

    /**
     * The runtime grant **and** the user's system-level switch for this app. This is the
     * gate a post checks; [hasPermission] alone is not enough, because Android's app-level
     * "Notifications: off" leaves the permission granted and silently drops every entry.
     */
    fun canPost(context: Context): Boolean =
        hasPermission(context) &&
            NotificationManagerCompat.from(context).areNotificationsEnabled()

    /**
     * Post the test entry, and say whether it really arrived.
     *
     * Silent, fixed id, `FLAG_IMMUTABLE or FLAG_UPDATE_CURRENT`, tap opens [MainActivity].
     * `FLAG_UPDATE_CURRENT` is not boilerplate: extras are not part of `PendingIntent`
     * equality, so without it a later intent built for the same activity and request code
     * would silently reuse this one's extras. `FLAG_IMMUTABLE` is mandatory from API 31
     * (this app's minSdk) anyway.
     *
     * The tap intent is deliberately the launcher intent, with no URL and no extras
     * (STANDARDS §11.3): the shell's job here is to bring the task back, and the page
     * restores its own view from its own storage. A route in a notification would be a
     * second source of truth for what the app is showing.
     *
     * **The answer is [Delivery], not "we reached the end of this function".** Two ordinary
     * things make an entry vanish without an exception the caller would otherwise see: the
     * grant can be revoked between [canPost] and the `notify()` a few instructions later
     * (Settings is a different process, and the `SecurityException` is the only word we
     * get), and a channel the user muted accepts the entry and shows nothing. A test button
     * that reported success in either case would be testing nothing.
     */
    fun postTest(context: Context): Boolean {
        if (!canPost(context)) {
            Log.w(TAG, "test notification skipped: not allowed to post")
            return false
        }
        ensureChannel(context)
        return post(
            context,
            context.getString(R.string.notify_test_title),
            context.getString(R.string.notify_test_text),
        ) == Delivery.POSTED
    }

    /**
     * Build and post the one kind of entry this app makes. Separate from [postTest] so the
     * "what an entry looks like" decisions are stated once, in one place a future push
     * feature would reuse rather than copy.
     *
     * The failure line goes to logcat REDACTED — the outcome and the exception's class
     * name, never its message and never the text of the entry (STANDARDS §0.4).
     */
    private fun post(context: Context, title: String, text: String): Delivery {
        val on = channelOn(context)
        var thrown: Exception? = null
        if (on) {
            val tap = PendingIntent.getActivity(
                context,
                TEST_REQUEST_CODE,
                tapIntent(context),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
            )
            val notification = NotificationCompat.Builder(context, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_notif)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(NotificationCompat.BigTextStyle().bigText(text))
                .setWhen(System.currentTimeMillis())
                .setShowWhen(true)
                .setSilent(true)
                .setAutoCancel(true)
                .setCategory(NotificationCompat.CATEGORY_STATUS)
                .setContentIntent(tap)
                .build()
            try {
                NotificationManagerCompat.from(context).notify(TEST_NOTIFICATION_ID, notification)
            } catch (e: SecurityException) {
                // Named on its own line, ahead of the broad one, because it is also what
                // tells lint the revocable POST_NOTIFICATIONS grant is handled here: a
                // `catch (Exception)` that merely happens to contain it reads as unhandled
                // and turns MissingPermission back into a build error.
                thrown = e
            } catch (e: Exception) {
                thrown = e
            }
        }
        val outcome = delivery(on, thrown)
        if (outcome != Delivery.POSTED) {
            Log.w(
                TAG,
                "notification $TEST_NOTIFICATION_ID not posted: $outcome" +
                    (thrown?.let { " (${it.javaClass.simpleName})" } ?: ""),
            )
        }
        return outcome
    }

    /**
     * Where a tap lands: the app, as the launcher would open it. `singleTask` means an
     * existing task is brought forward and `onNewIntent` fires — the page is not reloaded
     * and nothing it was showing is lost, which is the whole point of the shell.
     */
    private fun tapIntent(context: Context): Intent =
        Intent(context, MainActivity::class.java).apply {
            action = Intent.ACTION_MAIN
            addCategory(Intent.CATEGORY_LAUNCHER)
            flags = Intent.FLAG_ACTIVITY_NEW_TASK
        }

    /**
     * Has the person left this channel alive? `IMPORTANCE_NONE` is what Android's own
     * per-channel control puts it in, and it is the quiet failure: `notify()` accepts the
     * entry, returns normally, and shows nothing. No channel yet (or no manager) reads as
     * "nothing says otherwise" — [ensureChannel] runs before every post.
     */
    fun channelOn(context: Context): Boolean {
        val manager = context.getSystemService(NotificationManager::class.java) ?: return true
        val channel = manager.getNotificationChannel(CHANNEL_ID) ?: return true
        return channel.importance != NotificationManager.IMPORTANCE_NONE
    }

    // ---------------------------------------------------------------- rules (no Android)

    /**
     * The rule half of [post]'s answer, with no Android in it so it can be tested: what the
     * system reported, turned into which of the four things happened.
     *
     * @param channelOn the channel's own switch, as [channelOn] read it a moment earlier.
     * @param thrown whatever `notify()` raised, or null when it returned.
     */
    internal fun delivery(channelOn: Boolean, thrown: Throwable?): Delivery = when {
        // Order matters: a throw is evidence of what happened, the switch is only a reading
        // taken beforehand — and a SecurityException is the ONLY word we get that the grant
        // went away, so it must not be masked by a stale `false` from the channel read.
        thrown is SecurityException -> Delivery.NO_PERMISSION
        thrown != null -> Delivery.ERROR
        !channelOn -> Delivery.CHANNEL_OFF
        else -> Delivery.POSTED
    }

    /**
     * Which hint the settings row shows, from the four gates. Pure, so T5's screen never
     * has to re-derive the precedence — and the precedence matters: "the switch is off" is
     * the user's own answer and outranks a missing grant, and a missing grant outranks the
     * system-level block because asking for the grant is the action the row can offer.
     *
     * @param switchOn the app's own preference (`ShellPrefs.notifyEnabled`).
     * @param hasPermission [hasPermission].
     * @param systemEnabled `NotificationManagerCompat.areNotificationsEnabled()`.
     */
    internal fun readiness(
        switchOn: Boolean,
        hasPermission: Boolean,
        systemEnabled: Boolean,
        channelEnabled: Boolean = true,
    ): Readiness = when {
        !switchOn -> Readiness.OFF
        !hasPermission -> Readiness.NEEDS_PERMISSION
        !systemEnabled -> Readiness.BLOCKED
        !channelEnabled -> Readiness.CHANNEL_OFF
        else -> Readiness.ON
    }

    /**
     * Is there a runtime permission to ask for right now? Only when the user has just
     * turned the switch on and the grant is missing. Turning the switch OFF asks for
     * nothing, and neither does turning it on when the grant is already there — a
     * `requestPermissions` call for an already-granted permission returns instantly with no
     * dialog, but making the caller's intent explicit is what keeps "we never ask at
     * launch" (STANDARDS §11.2) auditable in one place.
     */
    internal fun shouldRequestPermission(switchOn: Boolean, hasPermission: Boolean): Boolean =
        switchOn && !hasPermission

    /**
     * **Where the switch lands after the system dialog answers.** Denied means the switch
     * springs back off (STANDARDS §11.2): leaving it on would be the app claiming a state
     * Android has just refused to give it, and every later "send a test notification" would
     * fail silently against a row that says notifications are on.
     *
     * T5 must apply this with the change listener DETACHED. Setting `isChecked` fires the
     * listener, the listener re-enters the request path, and on Android 13+ a second denial
     * is permanent — the re-entry would burn the one remaining ask on a dialog the system
     * no longer shows.
     */
    internal fun switchAfterPermissionResult(requestedOn: Boolean, granted: Boolean): Boolean =
        requestedOn && granted

    /**
     * `onRequestPermissionsResult`'s int array, read as a yes or a no. An empty array is
     * Android's way of saying the request was cancelled (the dialog was dismissed, or the
     * activity was interrupted), and a cancellation is not a grant.
     */
    internal fun permissionGranted(grantResults: IntArray): Boolean =
        grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED
}
