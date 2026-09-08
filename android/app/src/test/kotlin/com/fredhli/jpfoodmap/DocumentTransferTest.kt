package com.fredhli.jpfoodmap

import org.junit.Assert.*
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.lang.ref.WeakReference
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

class DocumentTransferTest {
    private class BlockingDocument(val point: String) : JsonDocument {
        val entered = CountDownLatch(1)
        val released = CountDownLatch(1)
        val done = CountDownLatch(1)
        val closed = CountDownLatch(1)
        val writes = AtomicInteger()
        val reads = AtomicInteger()
        private fun block() {
            entered.countDown()
            // Intentionally ignore interrupts: only signal/descriptor close releases this provider.
            while (released.count > 0) try { released.await() } catch (_: InterruptedException) { }
        }
        fun open(): JsonDocument { if (point == "open") block(); return this }
        override fun input(): InputStream {
            if (point == "input") block()
            return object : InputStream() {
                override fun read(): Int = error("use bulk read")
                override fun read(b: ByteArray, off: Int, len: Int): Int {
                    val count = reads.getAndIncrement()
                    if (point == "read" || point == "read-mid" && count == 1) block()
                    if (released.count == 0L) throw IOException("Closed")
                    b[off] = '{'.code.toByte()
                    return 1
                }
                override fun close() { this@BlockingDocument.close() }
            }
        }
        override fun output(): OutputStream {
            if (point == "output") block()
            return object : OutputStream() {
                override fun write(b: Int) = error("use bulk write")
                override fun write(b: ByteArray, off: Int, len: Int) {
                    val count = writes.get()
                    if (point == "write" || point == "write-mid" && count == 1) block()
                    if (released.count == 0L) throw IOException("Closed")
                    writes.incrementAndGet()
                }
                override fun close() { this@BlockingDocument.close() }
            }
        }
        override fun close() { released.countDown(); closed.countDown() }
    }

    private fun await(latch: CountDownLatch) = assertTrue("bounded wait", latch.await(3, TimeUnit.SECONDS))

    @Test fun navigationClosesBlockingOpenReadAndWriteAndAllowsNextTransfer() {
        for (point in listOf("open", "input", "read", "read-mid", "output", "write", "write-mid")) {
            val document = BlockingDocument(point)
            val calls = AtomicInteger()
            val operation = DocumentTransfer({ document.released.countDown() }, { calls.incrementAndGet() }, ByteArray(20_000))
            operation.start { task ->
                try { JsonDocumentIo.transfer(task, point.startsWith("write") || point == "output", document::open) }
                finally { document.done.countDown() }
            }
            await(document.entered)
            val before = document.writes.get()
            operation.cancel()
            await(document.done)
            await(document.closed)
            assertEquals("no writes after cancellation at $point", before, document.writes.get())
            assertEquals("navigation detaches callback at $point", 0, calls.get())
            assertThrows(IOException::class.java) { operation.payload() }
            validTransfer()
        }
    }

    @Test fun deadlineClosesActualResourceAndCompletesExactlyOnce() {
        val document = BlockingDocument("read")
        val replies = AtomicInteger()
        val replied = CountDownLatch(1)
        val operation = DocumentTransfer({ document.released.countDown() }, { ok ->
            assertFalse(ok); replies.incrementAndGet(); replied.countDown()
        })
        operation.start(150) { task ->
            try { JsonDocumentIo.transfer(task, false, document::open) } finally { document.done.countDown() }
        }
        await(document.entered); await(replied); await(document.done); await(document.closed)
        assertEquals(1, replies.get())
        operation.cancel()
        assertEquals(1, replies.get())
        validTransfer()
    }

    @Test fun cancelledProviderReturningLateCannotReachNewDocumentOrRetainUiCallback() {
        val document = BlockingDocument("open")
        val callback = object : (Boolean) -> Unit { override fun invoke(value: Boolean) = error("detached") }
        val weak = WeakReference(callback)
        val operation = DocumentTransfer({}, callback, ByteArray(100))
        operation.start { task ->
            try { JsonDocumentIo.transfer(task, true, document::open) } finally { document.done.countDown() }
        }
        await(document.entered)
        operation.cancel()
        // Inspect ownership directly: the waiting worker must not own the UI callback or payload.
        for (name in listOf("callback", "bytes")) {
            val field = DocumentTransfer::class.java.getDeclaredField(name).apply { isAccessible = true }
            assertNull((field.get(operation) as java.util.concurrent.atomic.AtomicReference<*>).get())
        }
        assertNotNull(weak.get()) // test itself still owns callback; no flaky GC assertion
        validTransfer()
        document.released.countDown()
        await(document.done); await(document.closed)
        assertEquals(0, document.writes.get())
    }

    @Test fun malformedUtf8OversizeAndMalformedJsonFailWithoutSuccess() {
        for (bytes in listOf(byteArrayOf(123,34,120,34,58,34,0xC3.toByte(),34,125),
                ByteArray(JsonFiles.MAX_BYTES + 1) { 32 }, "{'x':1}".toByteArray())) {
            val result = CountDownLatch(1)
            val closed = CountDownLatch(1)
            val operation = DocumentTransfer({}, { ok -> assertFalse(ok); result.countDown() })
            operation.start { task -> JsonDocumentIo.transfer(task, false) {
                object : JsonDocument {
                    override fun input() = ByteArrayInputStream(bytes)
                    override fun output(): OutputStream = error("import")
                    override fun close() { closed.countDown() }
                }
            } }
            await(result); await(closed)
        }
    }

    private fun validTransfer() {
        val result = CountDownLatch(1)
        val output = ByteArrayOutputStream()
        val operation = DocumentTransfer({}, { ok -> assertTrue(ok); result.countDown() }, "{}".toByteArray())
        operation.start { task -> JsonDocumentIo.transfer(task, true) {
            object : JsonDocument {
                override fun input() = ByteArrayInputStream("{}".toByteArray())
                override fun output() = output
                override fun close() { }
            }
        } }
        await(result)
        assertEquals("{}", output.toString("UTF-8"))
    }
}
