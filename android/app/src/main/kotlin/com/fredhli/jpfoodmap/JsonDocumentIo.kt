package com.fredhli.jpfoodmap

import java.io.ByteArrayOutputStream
import java.io.Closeable
import java.io.InputStream
import java.io.OutputStream
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction

internal interface JsonDocument : Closeable {
    fun input(): InputStream
    fun output(): OutputStream
}

internal object JsonDocumentIo {
    fun transfer(operation: DocumentTransfer, writing: Boolean, open: () -> JsonDocument?): Boolean {
        operation.check()
        val document = open() ?: return false
        operation.own(document)
        operation.check()
        if (writing) {
            val out = operation.own(document.output())
            operation.check()
            val bytes = operation.payload()
            var offset = 0
            while (offset < bytes.size) {
                operation.check()
                val length = minOf(8192, bytes.size - offset)
                out.write(bytes, offset, length)
                offset += length
            }
            operation.check()
            out.flush()
            operation.check()
            return true
        }
        val input = operation.own(document.input())
        val out = ByteArrayOutputStream()
        val buffer = ByteArray(8192)
        while (true) {
            operation.check()
            val n = input.read(buffer)
            if (n < 0) break
            if (out.size() + n > JsonFiles.MAX_BYTES) return false
            out.write(buffer, 0, n)
        }
        operation.check()
        val text = Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(out.toByteArray())).toString()
        return JsonFiles.validJson(text)
    }
}
