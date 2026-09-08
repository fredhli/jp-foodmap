package com.fredhli.jpfoodmap

import java.io.Closeable
import java.io.IOException
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.Semaphore
import java.util.concurrent.ThreadFactory
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

/** Owns the provider cancellation handle and every opened resource. No Activity is held here. */
internal class DocumentTransfer(
    private val cancelOpen: () -> Unit,
    completion: (Boolean) -> Unit,
    payload: ByteArray? = null,
) {
    private val lock = Any()
    private var stopped = false
    private val resources = mutableListOf<Closeable>()
    private val callback = AtomicReference<((Boolean) -> Unit)?>(completion)
    private val bytes = AtomicReference(payload)
    private var worker: Thread? = null
    private val outstanding = AtomicInteger(1)
    private var deadline: Future<*>? = null

    fun start(timeoutMs: Long = 15_000L, work: (DocumentTransfer) -> Boolean) {
        if (!permits.tryAcquire()) { finish(false); return }
        try {
            synchronized(lock) {
                if (stopped) { permits.release(); return }
                deadline = timer.schedule({ cancel(notify = true) }, timeoutMs, TimeUnit.MILLISECONDS)
                workers.execute {
                    synchronized(lock) { worker = Thread.currentThread() }
                    var result = false
                    try { check(); result = work(this) }
                    catch (_: Exception) { }
                    finally {
                        try { releaseResources() } catch (_: Exception) { result = false }
                        try { finish(result) }
                        finally { bytes.set(null); releaseSlot() }
                    }
                }
            }
        } catch (_: Exception) { permits.release(); finish(false) }
    }

    fun check() { synchronized(lock) { if (stopped) throw IOException("Transfer cancelled") } }

    fun payload(): ByteArray = bytes.get() ?: throw IOException("Transfer cancelled")

    fun <T : Closeable> own(resource: T): T {
        val accepted = synchronized(lock) { if (stopped) false else { resources.add(resource); true } }
        if (!accepted) { close(resource); throw IOException("Transfer cancelled") }
        return resource
    }

    /** UI state is detached immediately; provider cancellation and close never run on the UI thread. */
    fun cancel(notify: Boolean = false) {
        if (!notify) callback.set(null)
        val pending = synchronized(lock) {
            if (stopped) return
            stopped = true
            deadline?.cancel(false)
            bytes.set(null)
            resources.toList().also {
                resources.clear()
                outstanding.addAndGet(it.size + 1)
                worker?.interrupt()
            }
        }
        // Cancel the provider's open call separately, so a slow remote cancellation cannot delay close.
        cleanup.execute { try { cancelOpen() } catch (_: Exception) { } finally { releaseSlot() } }
        pending.forEach { resource -> cleanup.execute { try { close(resource) } finally { releaseSlot() } } }
        callback.getAndSet(null)?.invoke(false)
    }

    private fun finish(success: Boolean) {
        val deliver = synchronized(lock) {
            if (stopped) return
            stopped = true
            deadline?.cancel(false)
            bytes.set(null)
            callback.getAndSet(null)
        }
        deliver?.invoke(success)
    }

    private fun releaseResources() {
        val pending = synchronized(lock) { resources.toList() }
        var failure: Exception? = null
        for (resource in pending.asReversed()) {
            try { resource.close() } catch (error: Exception) { if (failure == null) failure = error }
        }
        synchronized(lock) { resources.removeAll(pending.toSet()) }
        failure?.let { throw it }
    }

    private fun releaseSlot() { if (outstanding.decrementAndGet() == 0) permits.release() }

    private fun close(resource: Closeable) { try { resource.close() } catch (_: Exception) { } }

    private companion object {
        val threads = ThreadFactory { runnable -> Thread(runnable, "json-document").apply { isDaemon = true } }
        // A provider that ignores both cancellation and close cannot create unbounded blocked workers.
        val permits = Semaphore(2)
        val workers = Executors.newFixedThreadPool(2, threads)
        val cleanup = Executors.newFixedThreadPool(4, threads)
        val timer = Executors.newSingleThreadScheduledExecutor(threads)
    }
}
