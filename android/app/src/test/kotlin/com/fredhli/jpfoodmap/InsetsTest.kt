package com.fredhli.jpfoodmap

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Who owns the keyboard inset, decided from the WebView provider's version string.
 *
 * Worth a test even though the rule is one comparison: the input is a string that arrives
 * from a package the user updates behind our back, the wrong answer hides the site's search
 * box behind the keyboard, and neither emulator here can reproduce an old WebView.
 */
class InsetsTest {

    @Test
    fun `majorVersion reads the leading number`() {
        assertEquals(145, Insets.majorVersion("145.0.7632.218"))
        assertEquals(143, Insets.majorVersion("143.0.1"))
        assertEquals(144, Insets.majorVersion("144"))
        assertEquals(151, Insets.majorVersion(" 151.0.0.0 "))
    }

    @Test
    fun `majorVersion is null for anything that is not a version`() {
        assertNull(Insets.majorVersion(null))
        assertNull(Insets.majorVersion(""))
        assertNull(Insets.majorVersion("abc"))
        assertNull(Insets.majorVersion("v144"))
        // Digits, but not an Int: a comparison against a number we cannot parse is a guess.
        assertNull(Insets.majorVersion("99999999999999999999.1"))
    }

    @Test
    fun `imeModeFor is WEBVIEW from 144 and NATIVE below or unknown`() {
        assertEquals(Insets.ImeMode.NATIVE, Insets.imeModeFor("143.0.1"))
        assertEquals(Insets.ImeMode.WEBVIEW, Insets.imeModeFor("144"))
        assertEquals(Insets.ImeMode.WEBVIEW, Insets.imeModeFor("144.0.7559.24"))
        // The emulator ships 145, Fred's phone 151: both must take the WebView path.
        assertEquals(Insets.ImeMode.WEBVIEW, Insets.imeModeFor("145.0.7632.218"))
        assertEquals(Insets.ImeMode.WEBVIEW, Insets.imeModeFor("151.0.1"))
        assertEquals(Insets.ImeMode.NATIVE, Insets.imeModeFor(null))
        assertEquals(Insets.ImeMode.NATIVE, Insets.imeModeFor("abc"))
        assertEquals(144, Insets.IME_IN_WEBVIEW_FROM_MAJOR)
    }
}
