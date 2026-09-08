package com.fredhli.jpfoodmap

/** Strict JSON grammar with an explicit stack, independent of Android's lenient JSONTokener. */
internal class StrictJson(private val text: String) {
    private var pos = 0
    private val states = IntArray(128)
    private var depth = 0

    fun objectDocument(): Boolean {
        space()
        if (!take('{')) return false
        states[depth++] = OBJECT_FIRST
        while (depth > 0) {
            space()
            when (states[depth - 1]) {
                OBJECT_FIRST, OBJECT_KEY -> {
                    val emptyAllowed = states[depth - 1] == OBJECT_FIRST
                    if (emptyAllowed && take('}')) depth--
                    else {
                        if (!string()) return false
                        space()
                        if (!take(':')) return false
                        states[depth - 1] = OBJECT_END
                        if (!value()) return false
                    }
                }
                OBJECT_END -> when {
                    take('}') -> depth--
                    take(',') -> states[depth - 1] = OBJECT_KEY
                    else -> return false
                }
                ARRAY_FIRST, ARRAY_VALUE -> {
                    if (states[depth - 1] == ARRAY_FIRST && take(']')) depth--
                    else {
                        states[depth - 1] = ARRAY_END
                        if (!value()) return false
                    }
                }
                ARRAY_END -> when {
                    take(']') -> depth--
                    take(',') -> states[depth - 1] = ARRAY_VALUE
                    else -> return false
                }
            }
        }
        space()
        return pos == text.length
    }

    private fun value(): Boolean {
        space()
        if (pos == text.length) return false
        return when (text[pos]) {
            '{', '[' -> {
                if (depth == states.size) false
                else { states[depth++] = if (text[pos++] == '{') OBJECT_FIRST else ARRAY_FIRST; true }
            }
            '"' -> string()
            't' -> literal("true")
            'f' -> literal("false")
            'n' -> literal("null")
            else -> number()
        }
    }

    private fun string(): Boolean {
        if (!take('"')) return false
        while (pos < text.length) {
            val c = text[pos++]
            if (c == '"') return true
            if (c < ' ') return false
            if (c != '\\') continue
            if (pos == text.length) return false
            when (text[pos++]) {
                '"', '\\', '/', 'b', 'f', 'n', 'r', 't' -> Unit
                'u' -> repeat(4) {
                    if (pos == text.length || text[pos++] !in "0123456789abcdefABCDEF") return false
                }
                else -> return false
            }
        }
        return false
    }

    private fun number(): Boolean {
        take('-')
        if (!take('0') && !digits(true)) return false
        if (take('.') && !digits(false)) return false
        if (take('e') || take('E')) {
            if (!take('+')) take('-')
            if (!digits(false)) return false
        }
        return true
    }

    private fun digits(nonzero: Boolean): Boolean {
        val start = pos
        if (nonzero && (pos == text.length || text[pos] !in '1'..'9')) return false
        while (pos < text.length && text[pos] in '0'..'9') pos++
        return pos > start
    }

    private fun literal(value: String): Boolean {
        if (!text.startsWith(value, pos)) return false
        pos += value.length
        return true
    }

    private fun take(c: Char): Boolean = (pos < text.length && text[pos] == c).also { if (it) pos++ }
    private fun space() { while (pos < text.length && text[pos] in " \t\r\n") pos++ }

    private companion object {
        const val OBJECT_FIRST = 0
        const val OBJECT_KEY = 1
        const val OBJECT_END = 2
        const val ARRAY_FIRST = 3
        const val ARRAY_VALUE = 4
        const val ARRAY_END = 5
    }
}
