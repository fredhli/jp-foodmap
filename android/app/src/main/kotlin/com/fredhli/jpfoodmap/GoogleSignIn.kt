package com.fredhli.jpfoodmap

import android.app.Activity
import androidx.credentials.ClearCredentialStateRequest
import androidx.credentials.CredentialManager
import androidx.credentials.CustomCredential
import androidx.credentials.GetCredentialRequest
import androidx.credentials.exceptions.GetCredentialCancellationException
import androidx.credentials.exceptions.GetCredentialException
import androidx.credentials.exceptions.GetCredentialProviderConfigurationException
import androidx.credentials.exceptions.GetCredentialUnsupportedException
import androidx.credentials.exceptions.NoCredentialException
import com.google.android.libraries.identity.googleid.GetGoogleIdOption
import com.google.android.libraries.identity.googleid.GoogleIdTokenCredential

/**
 * Native Google sign-in, because the page cannot do it here: Google refuses GIS and OAuth
 * inside a WebView (`disallowed_useragent`), so the shell asks Credential Manager for an
 * ID token and hands it to the page, which posts it to the Worker exactly as it does in a
 * browser (docs/PLAN.md D4). The shell never stores the token, never logs it, and never
 * talks to api.jpfoodmap.com itself.
 *
 * Until Fred registers an Android OAuth client (docs/PLAN.md §9 step 1) every request fails
 * with [Error.NOT_CONFIGURED], and the page falls back to "open in browser". That is the
 * designed degraded state, not a bug.
 */
object GoogleSignIn {

    /**
     * The WEB client id, not an Android one: this is the `aud` the Worker verifies, and it
     * is the same value as GOOGLE_CLIENT_ID in src/tabelog/scrape/map.py. The Android OAuth
     * client Fred registers is matched by package name + signing certificate and is never
     * named in code. Not a secret — it ships in every copy of the site already.
     */
    const val WEB_CLIENT_ID = "536198170238-me7dpu2og75tseuekl3pu8rjjgo2ig2p.apps.googleusercontent.com"

    /** What the page is told when there is no token; the strings are part of the contract. */
    enum class Error(val wire: String) {
        NO_CREDENTIAL("no_credential"),
        CANCELLED("cancelled"),
        NOT_CONFIGURED("not_configured"),
        ERROR("error"),
    }

    sealed class Result {
        data class Token(val idToken: String) : Result()
        data class Failed(val error: Error) : Result()
    }

    /**
     * @param silent true for the startup re-auth attempt: authorized accounts only, auto
     *   select on, no UI if there is nothing to pick.
     *
     * Never throws: every failure is an [Error], because the caller's one obligation is to
     * answer the page's `req` exactly once.
     */
    suspend fun request(activity: Activity, silent: Boolean): Result {
        // filterByAuthorizedAccounts + autoSelect is what makes the silent path silent: it
        // offers only accounts that have already granted this app, and takes the single
        // match without asking. The interactive path wants the opposite of both — every
        // account on the device, and always a chooser.
        val option = GetGoogleIdOption.Builder()
            .setServerClientId(WEB_CLIENT_ID)
            .setFilterByAuthorizedAccounts(silent)
            .setAutoSelectEnabled(silent)
            .build()
        val request = GetCredentialRequest.Builder().addCredentialOption(option).build()
        return try {
            val credential = CredentialManager.create(activity)
                .getCredential(activity, request)
                .credential
            if (credential !is CustomCredential ||
                credential.type != GoogleIdTokenCredential.TYPE_GOOGLE_ID_TOKEN_CREDENTIAL
            ) {
                // A password or passkey came back instead — nothing this app can use.
                return Result.Failed(Error.NO_CREDENTIAL)
            }
            val token = GoogleIdTokenCredential.createFrom(credential.data).idToken
            if (token.isBlank()) Result.Failed(Error.NO_CREDENTIAL) else Result.Token(token)
        } catch (_: GetCredentialCancellationException) {
            Result.Failed(Error.CANCELLED)
        } catch (_: NoCredentialException) {
            // No Google account on the device, or none authorized in the silent case.
            Result.Failed(Error.NO_CREDENTIAL)
        } catch (_: GetCredentialProviderConfigurationException) {
            // Credential Manager found no provider: an emulator image without Play
            // services, or a phone where they are disabled.
            Result.Failed(Error.NOT_CONFIGURED)
        } catch (_: GetCredentialUnsupportedException) {
            Result.Failed(Error.NOT_CONFIGURED)
        } catch (e: GetCredentialException) {
            Result.Failed(classify(e.type, e.message))
        } catch (_: Exception) {
            // GoogleIdTokenParsingException and anything else the provider throws on its
            // way out. The page's job is to show one line and move on, not to diagnose.
            Result.Failed(Error.ERROR)
        }
    }

    /**
     * Forget which account was used, so the next [request] shows the chooser again
     * (STANDARDS §7.6). Best effort: the page has already dropped the Worker session, and a
     * provider that refuses to clear must not turn a successful sign-out into a crash.
     */
    suspend fun signOut(activity: Activity) {
        try {
            CredentialManager.create(activity)
                .clearCredentialState(ClearCredentialStateRequest())
        } catch (_: Exception) {
            // nothing to do and nothing to say
        }
    }

    /**
     * Pure, so the mapping can be unit-tested without a device.
     *
     * The one distinction that matters to the user is NOT_CONFIGURED: it means "this build
     * is not registered with Google yet" (docs/PLAN.md §9 step 1), which is a thing Fred can
     * fix, and the shell offers "open in browser" for it. Google reports it as an
     * unstructured message from one-tap — status 10 (developer error: package + SHA-1 not
     * on the OAuth client) or 16, with the console named in the text.
     */
    internal fun classify(type: String?, message: String?): Error {
        val haystack = "${type.orEmpty()} ${message.orEmpty()}"
        if (NOT_CONFIGURED_RE.containsMatchIn(haystack)) return Error.NOT_CONFIGURED
        if (TYPE_NO_CREDENTIAL_RE.containsMatchIn(haystack)) return Error.NO_CREDENTIAL
        return Error.ERROR
    }

    /** "Developer console", "10: Caller not whitelisted…", "16: …". */
    private val NOT_CONFIGURED_RE = Regex(
        """developer\s*console|unregistered|\b(10|16)\s*[:.]""",
        RegexOption.IGNORE_CASE,
    )

    /** The exception types that mean "nothing to sign in with", by name rather than class. */
    private val TYPE_NO_CREDENTIAL_RE = Regex("NO_CREDENTIAL", RegexOption.IGNORE_CASE)
}
