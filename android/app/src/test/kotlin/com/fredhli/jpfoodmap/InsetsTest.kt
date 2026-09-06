package com.fredhli.jpfoodmap

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
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

    // ---- --app-inset-top (A-1) ------------------------------------------------------------

    @Test
    fun `cssPxFromPx converts device pixels at the Fold's density`() {
        // The emulator and the phone both run 420 dpi -> density 2.625. 63 px is the AVD's
        // status bar (24 dp), 105 px is the phone's (40 dp).
        assertEquals(24, Insets.cssPxFromPx(63, 2.625f))
        assertEquals(40, Insets.cssPxFromPx(105, 2.625f))
        assertEquals(24, Insets.cssPxFromPx(24, 1.0f))
        // Landscape on the inner screen with the bar hidden: nothing to pad.
        assertEquals(0, Insets.cssPxFromPx(0, 2.625f))
    }

    @Test
    fun `cssPxFromPx never returns a negative or a division by a broken density`() {
        assertEquals(0, Insets.cssPxFromPx(-5, 2.625f))
        // Not reachable from a real DisplayMetrics, but Infinity in a CSS declaration would
        // be silently dropped by the parser and the padding would vanish.
        assertEquals(63, Insets.cssPxFromPx(63, 0f))
        assertEquals(63, Insets.cssPxFromPx(63, Float.NaN))
    }

    @Test
    fun `appInsetTopJs sets the variable the page reads`() {
        assertEquals("--app-inset-top", Insets.APP_INSET_TOP_VAR)
        val js = Insets.appInsetTopJs(40)
        assertTrue(js, js.contains("documentElement.style.setProperty('--app-inset-top','40px')"))
        // Guarded: it runs against whatever document is up, error pages included.
        assertTrue(js, js.contains("try{") && js.contains("catch(e){}"))
        // The only interpolation is an Int, so nothing can close the JS string literal.
        assertEquals(0, js.count { it == '\"' })
        assertEquals("(function(){try{document.documentElement.style.setProperty(" +
            "'--app-inset-top','0px')}catch(e){}})()", Insets.appInsetTopJs(-3))
    }
}
