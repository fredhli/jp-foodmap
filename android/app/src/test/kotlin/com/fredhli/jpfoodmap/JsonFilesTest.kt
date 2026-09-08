package com.fredhli.jpfoodmap

import org.junit.Assert.*
import org.junit.Test

class JsonFilesTest {
    @Test fun filenameCannotEscapeDestinationOrChangeType() {
        assertTrue(JsonFiles.validFilename("jpfoodmap-2026-09-08.json"))
        for (name in listOf("../data.json", "a/b.json", "file.html", "x.json.exe", "content://x.json", "\u0000.json")) {
            assertFalse(name, JsonFiles.validFilename(name))
        }
    }

    @Test fun pickerAllowsOnlyExplicitJsonAndSingleSelectionTypes() {
        assertTrue(JsonFiles.acceptsJson(arrayOf(".json", "application/json")))
        for (types in listOf(emptyArray(), arrayOf("*/*"), arrayOf("application/json", "image/*"), arrayOf("text/html"))) {
            assertFalse(JsonFiles.acceptsJson(types))
        }
    }

    @Test fun payloadLimitCountsUtf8AndRequiresAnObject() {
        assertTrue(JsonFiles.validJson("{\"favorites\":[],\"folders\":[]}"))
        assertFalse(JsonFiles.validJson("[]"))
        assertFalse(JsonFiles.validJson("broken"))
        val chinese = "{\"note\":\"" + "中".repeat(JsonFiles.MAX_BYTES / 3 + 1) + "\"}"
        assertTrue(chinese.length < JsonFiles.MAX_BYTES)
        assertFalse(JsonFiles.validJson(chinese))
    }

    @Test fun nestingAndTrailingJunkAreRejectedBeforeNativeParsing() {
        assertFalse(JsonFiles.validJson("{\"x\":" + "[".repeat(200) + "0" + "]".repeat(200) + "}"))
        assertFalse(JsonFiles.validJson("{} trailing"))
        assertTrue(JsonFiles.validJson("{\"text\":\"[not nesting]\"}"))
    }

    @Test fun strictSyntaxRejectsAospLeniencyAndDeepBypasses() {
        for (depth in listOf(200, 20_000)) {
            assertFalse(JsonFiles.validJson("{\"a\":'\"',\"b\":" + "[".repeat(depth) + "0" + "]".repeat(depth) + ",\"c\":'\"'}"))
            assertFalse(JsonFiles.validJson("{\"a\":/*\"*/" + "[".repeat(depth) + "0" + "]".repeat(depth) + "}"))
        }
        for (text in listOf("{'a':1}", "{a:1}", "{\"a\":NaN}", "{\"a\":Infinity}", "{\"a\":01}",
                "{\"a\":+1}", "{\"a\":.1}", "{\"a\":1.}", "{\"a\":1e}", "{\"a\":0x10}",
                "{\"a\":1,}", "{\"a\":[1,]}", "{\"a\":[,1]}", "{\"a\":true false}", "{}/*x*/",
                "{\"a\":\"\\x20\"}", "{\"a\":\"\\u000g\"}", "{\"a\":\"\n\"}", "\u000c{}")) {
            assertFalse(text, JsonFiles.validJson(text))
        }
    }

    @Test fun exactDepthAndStrictEscapesAndUtf8Boundaries() {
        for (depth in listOf(126, 127, 128)) {
            val text = "{\"a\":" + "[".repeat(depth) + "0" + "]".repeat(depth) + "}"
            assertEquals("root object counts toward depth", depth <= 127, JsonFiles.validJson(text))
        }
        assertTrue(JsonFiles.validJson("{\"a\":\"中😀\\\"\\\\\\/\\b\\f\\n\\r\\t\\u0022\\uD83D\\uDE00\",\"n\":[-0,0.25,1e-2,-3E+4,true,false,null,{}]}"))
        val exact = "{\"a\":\"" + "x".repeat(JsonFiles.MAX_BYTES - 8) + "\"}"
        assertEquals(JsonFiles.MAX_BYTES, exact.toByteArray().size)
        assertTrue(JsonFiles.validJson(exact))
        assertFalse(JsonFiles.validJson(exact + " "))
    }

    @Test fun fileBridgeRequestsAreTypedAndRequestIdsAreBounded() {
        assertTrue(Bridge.parse("{\"t\":\"prepareJsonImport\",\"req\":\"file-1\"}") is Bridge.Msg.PrepareJsonImport)
        assertTrue(Bridge.parse("{\"t\":\"exportJson\",\"req\":\"file-2\",\"filename\":\"a.json\",\"text\":\"{}\"}") is Bridge.Msg.ExportJson)
        assertNull(Bridge.parse("{\"t\":\"exportJson\",\"req\":1,\"filename\":\"a.json\",\"text\":\"{}\"}"))
        assertNull(Bridge.parse("{\"t\":\"exportJson\",\"req\":\"file-2\",\"filename\":\"a.json\",\"text\":{}}"))
    }
}
