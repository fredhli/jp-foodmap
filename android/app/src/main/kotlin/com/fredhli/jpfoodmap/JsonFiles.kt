package com.fredhli.jpfoodmap

import android.app.Activity
import android.content.ContentResolver
import android.content.Intent
import android.net.Uri
import android.os.CancellationSignal
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.lifecycle.Lifecycle
import java.lang.ref.WeakReference

/** One user-selected JSON transfer. The page still owns its storage and backup schema. */
class JsonFiles(private val host: MainActivity) {
    private enum class Kind { IMPORT, EXPORT }
    private class Request(val generation: Long, val kind: Kind) {
        var callback: ValueCallback<Array<Uri>>? = null
        var reply: ((String, String?) -> Unit)? = null
        var bytes: ByteArray? = null
        var transfer: DocumentTransfer? = null
        var uri: Uri? = null
    }

    private var generation = 0L
    private var importUntil = 0L
    private var request: Request? = null
    // A cancelled document can still have a system picker open. Consume its result before another launch.
    private var picker: Kind? = null
    private var disposed = false

    private val open = host.registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        picker = null
        val current = request?.takeIf { it.kind == Kind.IMPORT } ?: return@registerForActivityResult
        val uri = result.data?.data
        if (result.resultCode != Activity.RESULT_OK || uri?.scheme != "content") {
            request = null
            current.callback?.onReceiveValue(null)
            current.callback = null
        } else startTransfer(current, uri)
    }

    private val create = host.registerForActivityResult(ActivityResultContracts.CreateDocument("application/json")) { uri ->
        picker = null
        val current = request?.takeIf { it.kind == Kind.EXPORT } ?: return@registerForActivityResult
        if (uri == null || uri.scheme != "content") {
            request = null
            current.reply?.invoke(if (uri == null) "cancelled" else "error", if (uri == null) null else "destination")
            current.reply = null
            current.bytes = null
        } else startTransfer(current, uri)
    }

    private fun startTransfer(current: Request, uri: Uri) {
        current.uri = uri
        val signal = CancellationSignal()
        val weak = WeakReference(this)
        val epoch = current.generation
        val kind = current.kind
        val resolver = host.applicationContext.contentResolver
        val transfer = DocumentTransfer(signal::cancel, { success ->
            main.post { weak.get()?.complete(epoch, kind, success) }
        }, current.bytes)
        current.bytes = null
        current.transfer = transfer
        // Captures only application resolver, URI and operation data, never host/Request/WebView/reply.
        transfer.start { operation -> transferJson(resolver, uri, kind == Kind.EXPORT, signal, operation) }
    }

    private fun complete(epoch: Long, kind: Kind, success: Boolean) {
        val current = request ?: return
        if (disposed || host.isDestroyed || current.generation != epoch || current.kind != kind) return
        request = null
        current.transfer = null
        val callback = current.callback
        val reply = current.reply
        current.callback = null
        current.reply = null
        if (kind == Kind.IMPORT) {
            callback?.onReceiveValue(if (success) arrayOf(current.uri!!) else null)
            if (!success) Toast.makeText(host, R.string.files_invalid_json, Toast.LENGTH_LONG).show()
        } else reply?.invoke(if (success) "saved" else "error", if (success) null else "write")
    }

    private fun ready(): Boolean = !disposed && !host.isFinishing && !host.isDestroyed &&
        host.lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED) &&
        Routes.isAppOrigin(host.webView?.url, host.appOrigins)

    /** Called only after Bridge checked origin and main frame. Consumed by one file input. */
    fun prepareImport(): Boolean {
        if (!ready() || request != null || picker != null) return false
        importUntil = SystemClock.elapsedRealtime() + 3000L
        return true
    }

    fun choose(view: WebView, callback: ValueCallback<Array<Uri>>, params: WebChromeClient.FileChooserParams): Boolean {
        val armed = importUntil > 0L && SystemClock.elapsedRealtime() <= importUntil
        importUntil = 0L
        if (!armed || !ready() || view !== host.webView || request != null || picker != null ||
            params.mode != WebChromeClient.FileChooserParams.MODE_OPEN || !acceptsJson(params.acceptTypes)) {
            callback.onReceiveValue(null)
            return true
        }
        request = Request(++generation, Kind.IMPORT).apply { this.callback = callback }
        picker = Kind.IMPORT
        try {
            open.launch(Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                addCategory(Intent.CATEGORY_OPENABLE)
                type = "application/json"
                putExtra(Intent.EXTRA_ALLOW_MULTIPLE, false)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            })
        } catch (_: Exception) {
            request = null
            picker = null
            callback.onReceiveValue(null)
        }
        return true
    }

    fun export(filename: String, text: String, reply: (String, String?) -> Unit) {
        if (!ready() || request != null || picker != null || !validFilename(filename) || !validJson(text)) {
            reply("error", "unavailable")
            return
        }
        request = Request(++generation, Kind.EXPORT).apply { bytes = text.toByteArray(Charsets.UTF_8); this.reply = reply }
        picker = Kind.EXPORT
        try { create.launch(filename) } catch (_: Exception) {
            request = null
            picker = null
            reply("error", "picker")
        }
    }

    /** A picker result or transfer completion must never reach a replacement document or renderer. */
    fun onNavigation() {
        generation++
        importUntil = 0L
        val old = request
        request = null
        old?.transfer?.cancel()
        old?.transfer = null
        old?.callback?.onReceiveValue(null)
        old?.callback = null
        old?.reply = null
        old?.bytes = null
    }

    fun dispose() { disposed = true; onNavigation() }

    companion object {
        const val MAX_BYTES = 2 * 1024 * 1024
        private val main by lazy { Handler(Looper.getMainLooper()) }

        private fun transferJson(resolver: ContentResolver, uri: Uri, writing: Boolean,
                                 signal: CancellationSignal, operation: DocumentTransfer): Boolean {
            return JsonDocumentIo.transfer(operation, writing) {
                resolver.openAssetFileDescriptor(uri, if (writing) "wt" else "r", signal)?.let { asset ->
                    object : JsonDocument {
                        override fun input() = asset.createInputStream()
                        override fun output() = asset.createOutputStream()
                        override fun close() = asset.close()
                    }
                }
            }
        }

        internal fun validFilename(name: String): Boolean =
            name.length in 6..100 && Regex("[A-Za-z0-9][A-Za-z0-9._-]*\\.json", RegexOption.IGNORE_CASE).matches(name)

        internal fun acceptsJson(types: Array<String>): Boolean {
            val values = types.flatMap { it.split(',') }.map { it.trim().lowercase() }.filter { it.isNotEmpty() }
            return values.isNotEmpty() && values.all { it == ".json" || it == "application/json" }
        }

        internal fun validJson(text: String): Boolean = text.length <= MAX_BYTES &&
            text.toByteArray(Charsets.UTF_8).size <= MAX_BYTES && StrictJson(text).objectDocument()
    }
}
