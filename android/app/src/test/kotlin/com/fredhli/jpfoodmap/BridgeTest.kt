package com.fredhli.jpfoodmap

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The pure half of the bridge (docs/PLAN.md §5.4): parsing what the page sends, building
 * the one reply that carries a credential, and baking the shell's labels into the façade.
 * org.json is the real implementation on the test classpath.
 *
 * Everything here is a contract with code that ships separately — the page is rebuilt and
 * deployed by Cloudflare Pages, the APK is sideloaded by hand, and the two are routinely
 * months apart. So the wire strings are asserted literally, not through the constants.
 */
class BridgeTest {

    // ---- parse ------------------------------------------------------------------------

    @Test
    fun `parse signin carries the request id and defaults silent to false`() {
        assertEquals(
            Bridge.Msg.SignIn("r1abc", false),
            Bridge.parse("""{"t":"signin","req":"r1abc","silent":false}"""),
        )
        assertEquals(
            Bridge.Msg.SignIn("r1abc", false),
            Bridge.parse("""{"t":"signin","req":"r1abc"}"""),
        )
        assertEquals(
            Bridge.Msg.SignIn("r1abc", true),
            Bridge.parse("""{"t":"signin","req":"r1abc","silent":true}"""),
        )
    }

    @Test
    fun `parse signin without a usable req is not a message`() {
        // Without a req the shell has nowhere to send the answer, and the page would sit
        // on its own timeout — better to drop it here than to reply into the void.
        assertNull(Bridge.parse("""{"t":"signin"}"""))
        assertNull(Bridge.parse("""{"t":"signin","req":""}"""))
        assertNull(Bridge.parse("""{"t":"signin","req":null}"""))
        assertNull(Bridge.parse("""{"t":"signin","req":7}"""))
    }

    @Test
    fun `parse signout`() {
        assertEquals(Bridge.Msg.SignOut, Bridge.parse("""{"t":"signout"}"""))
        // Payload-free: extra keys are ignored, not a reason to reject.
        assertEquals(Bridge.Msg.SignOut, Bridge.parse("""{"t":"signout","why":"user"}"""))
    }

    @Test
    fun `parse share keeps an empty title`() {
        // The façade sends String(title || "") — an empty string, not an absent key, and a
        // restaurant with no name still has a shareable URL.
        assertEquals(
            Bridge.Msg.Share("小舟渡", "https://jpfoodmap.com/?r=1a2b"),
            Bridge.parse("""{"t":"share","title":"小舟渡","url":"https://jpfoodmap.com/?r=1a2b"}"""),
        )
        assertEquals(
            Bridge.Msg.Share("", "https://jpfoodmap.com/?r=1a2b"),
            Bridge.parse("""{"t":"share","title":"","url":"https://jpfoodmap.com/?r=1a2b"}"""),
        )
        assertEquals(
            Bridge.Msg.Share("", "https://jpfoodmap.com/?r=1a2b"),
            Bridge.parse("""{"t":"share","url":"https://jpfoodmap.com/?r=1a2b"}"""),
        )
    }

    @Test
    fun `parse open settings haptic metrics`() {
        assertEquals(
            Bridge.Msg.Open("https://tabelog.com/fukui/A1801/"),
            Bridge.parse("""{"t":"open","url":"https://tabelog.com/fukui/A1801/"}"""),
        )
        assertEquals(Bridge.Msg.Settings, Bridge.parse("""{"t":"settings"}"""))
        assertEquals(Bridge.Msg.Haptic, Bridge.parse("""{"t":"haptic"}"""))
        assertEquals(Bridge.Msg.Metrics, Bridge.parse("""{"t":"metrics"}"""))
    }

    @Test
    fun `parse rejects unknown, malformed and incomplete messages`() {
        assertNull(Bridge.parse("""{"t":"reboot"}"""))
        assertNull(Bridge.parse("""{"t":"credential","idToken":"x"}"""))   // shell -> page only
        assertNull(Bridge.parse("""{"url":"https://jpfoodmap.com/"}"""))
        assertNull(Bridge.parse("not json"))
        assertNull(Bridge.parse(""))
        assertNull(Bridge.parse("[1,2,3]"))
        assertNull(Bridge.parse("""{"t":"share"}"""))
        assertNull(Bridge.parse("""{"t":"share","url":""}"""))
        assertNull(Bridge.parse("""{"t":"open","url":42}"""))
        assertNull(Bridge.parse("""{"t":"Settings"}"""))
        assertNull(Bridge.parse("""{"t":"signIn","req":"r1"}"""))
    }

    // ---- the credential reply ---------------------------------------------------------

    @Test
    fun `credentialJson carries exactly one of the token and the error`() {
        val ok = JSONObject(Bridge.credentialJson("r1abc", "header.payload.sig", null))
        assertEquals("credential", ok.getString("t"))
        assertEquals("r1abc", ok.getString("req"))
        assertEquals("header.payload.sig", ok.getString("idToken"))
        assertFalse(ok.has("error"))

        val bad = JSONObject(Bridge.credentialJson("r1abc", null, "not_configured"))
        assertEquals("not_configured", bad.getString("error"))
        assertFalse(bad.has("idToken"))

        // A failure with no error string still has to be a failure, not a silent success.
        assertEquals("error", JSONObject(Bridge.credentialJson("r1", null, null)).getString("error"))
    }

    @Test
    fun `credentialJson escapes rather than concatenates`() {
        // A token is opaque; the page reads a syntax error as "no credential", which would
        // look exactly like the not-configured path and send Fred hunting the wrong bug.
        val json = Bridge.credentialJson("r\"1\\", "a\"b\\c", null)
        val o = JSONObject(json)
        assertEquals("r\"1\\", o.getString("req"))
        assertEquals("a\"b\\c", o.getString("idToken"))
    }

    // ---- the façade -------------------------------------------------------------------

    @Test
    fun `the facade names every call the page makes`() {
        // The page feature-detects each of these by name; a rename here is a feature that
        // silently stops existing rather than an error anybody sees.
        assertTrue(Bridge.FACADE_JS.contains("window.NativeBridge"))
        assertTrue(Bridge.FACADE_JS.contains("""app: "jpfoodmap""""))
        assertTrue(Bridge.FACADE_JS.contains("""version: "3.1.0""""))
        assertTrue(Bridge.FACADE_JS.contains("signIn: function (req, silent)"))
        assertTrue(Bridge.FACADE_JS.contains("""send({ t: "signout" })"""))
        assertTrue(Bridge.FACADE_JS.contains("share: function (title, url)"))
        assertTrue(Bridge.FACADE_JS.contains("openExternal: function (url)"))
        assertTrue(Bridge.FACADE_JS.contains("""send({ t: "settings" })"""))
        assertTrue(Bridge.FACADE_JS.contains("""send({ t: "haptic" })"""))
        assertTrue(Bridge.FACADE_JS.contains("metrics: function ()"))
        // The one shell -> page callback, and the guard that keeps a page without it (an
        // older deploy, or a page still booting) from throwing inside the listener.
        assertTrue(Bridge.FACADE_JS.contains("""typeof window.__jpfmNativeCredential === "function""""))
        // Kotlin raw string: a `$` here would be a template expression, not a dollar sign.
        assertFalse(Bridge.FACADE_JS.contains('$'))
    }

    @Test
    fun `the facade bakes the labels in and never ships its own slots`() {
        val js = Bridge.facadeJs(
            mapOf(
                Bridge.LABEL_SIGN_IN to "Sign in with Google",
                Bridge.LABEL_SETTINGS to "App settings",
                Bridge.LABEL_BROWSER to "Open in browser",
            ),
        )
        assertTrue(js.contains("""signIn: "Sign in with Google""""))
        assertTrue(js.contains("""settings: "App settings""""))
        assertTrue(js.contains("""browser: "Open in browser""""))
        assertFalse(js.contains(Bridge.SIGN_IN_LABEL_SLOT))
        assertFalse(js.contains(Bridge.SETTINGS_LABEL_SLOT))
        assertFalse(js.contains(Bridge.BROWSER_LABEL_SLOT))
    }

    @Test
    fun `a missing label becomes empty, which the page reads as falsy`() {
        // Not a default string: the page already has its own fallback for the sign-in
        // button, and an English default baked in here would beat it on a Japanese phone.
        val js = Bridge.facadeJs(emptyMap())
        assertTrue(js.contains("""labels: { signIn: "", settings: "", browser: "" }"""))
    }

    @Test
    fun `a label with a quote or a script tag cannot break the injected script`() {
        // The façade runs before the page does; a syntax error in it is a blank app.
        assertEquals("""a\"b""", Bridge.jsEscape("""a"b"""))
        assertEquals("""a\\b""", Bridge.jsEscape("""a\b"""))
        assertEquals("""\u003C/script>""", Bridge.jsEscape("</script>"))
        assertEquals("""a\nb""", Bridge.jsEscape("a\nb"))
        assertEquals("", Bridge.jsEscape(null))
        val js = Bridge.facadeJs(mapOf(Bridge.LABEL_SIGN_IN to """Sign "in"; alert(1)//"""))
        assertTrue(js.contains("""signIn: "Sign \"in\"; alert(1)//""""))
    }

    // ---- constants --------------------------------------------------------------------

    @Test
    fun `constants match the contract`() {
        assertEquals("NativeBridge", Bridge.OBJECT_NAME)
        // The page matches /\bJpFoodMapApp\//, so the leading space and the trailing slash
        // are both load-bearing. The version after it is a version, not part of the marker.
        assertEquals(" JpFoodMapApp/", Bridge.UA_SUFFIX)
        assertEquals(
            "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 JpFoodMapApp/2.0.0",
            Bridge.userAgent("Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36", "2.0.0"),
        )
        assertTrue(Regex("""\bJpFoodMapApp/""").containsMatchIn(Bridge.userAgent("UA", "2.0.0")))
        assertEquals("signIn", Bridge.LABEL_SIGN_IN)
        assertEquals("settings", Bridge.LABEL_SETTINGS)
        assertEquals("browser", Bridge.LABEL_BROWSER)
    }

    // ---- the sign-in error mapping ----------------------------------------------------

    @Test
    fun `the wire words are what the page switches on`() {
        // The page shows "sign-in canceled" for exactly one of these and "sign-in handling
        // failed" for the rest; the shell offers "open in browser" for two of them.
        assertEquals("no_credential", GoogleSignIn.Error.NO_CREDENTIAL.wire)
        assertEquals("cancelled", GoogleSignIn.Error.CANCELLED.wire)
        assertEquals("not_configured", GoogleSignIn.Error.NOT_CONFIGURED.wire)
        assertEquals("error", GoogleSignIn.Error.ERROR.wire)
    }

    @Test
    fun `an unregistered build is recognised from what one-tap actually says`() {
        // These are the real message shapes: status 10 is "package + SHA-1 are not on the
        // OAuth client", which is precisely the step Fred has not done yet.
        assertEquals(
            GoogleSignIn.Error.NOT_CONFIGURED,
            GoogleSignIn.classify(null, "During begin sign in, failure response from one tap: 10: Caller not whitelisted to call this API."),
        )
        assertEquals(
            GoogleSignIn.Error.NOT_CONFIGURED,
            GoogleSignIn.classify(null, "16: Cannot find a matching credential"),
        )
        assertEquals(
            GoogleSignIn.Error.NOT_CONFIGURED,
            GoogleSignIn.classify(null, "Check the Developer Console for your client id"),
        )
    }

    @Test
    fun `everything else degrades to a plain failure`() {
        assertEquals(GoogleSignIn.Error.ERROR, GoogleSignIn.classify(null, null))
        assertEquals(GoogleSignIn.Error.ERROR, GoogleSignIn.classify("TYPE_UNKNOWN", "network error"))
        // A stray number must not read as a status code — the app would tell the user to
        // go and register an OAuth client that is already registered.
        assertEquals(GoogleSignIn.Error.ERROR, GoogleSignIn.classify(null, "took 10 seconds"))
        assertEquals(
            GoogleSignIn.Error.NO_CREDENTIAL,
            GoogleSignIn.classify("android.credentials.GetCredentialException.TYPE_NO_CREDENTIAL", null),
        )
    }

    @Test
    fun `the server client id is the web one the Worker verifies`() {
        // Same value as GOOGLE_CLIENT_ID in src/tabelog/scrape/map.py: the Worker checks
        // the token's `aud` against it, so an Android client id here would be rejected by
        // every /api/session POST.
        assertEquals(
            "536198170238-me7dpu2og75tseuekl3pu8rjjgo2ig2p.apps.googleusercontent.com",
            GoogleSignIn.WEB_CLIENT_ID,
        )
    }
}
