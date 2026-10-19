package org.barvis.sound.relay.relay

import org.barvis.sound.relay.protocol.ChunkEntry
import java.io.ByteArrayOutputStream

/**
 * Bytes of the chunk that was being read when a link dropped, so the next connection can
 * resume with `READ offset` (§5.2). In memory only, at most one chunk: the relay doesn't keep
 * its own copy of the audio (§1), this only saves re-sending bytes after a disconnect.
 */
class PartialReads {
    private var entry: ChunkEntry? = null
    private var bytes: ByteArray? = null

    /**
     * Bytes kept for this chunk if its entry is unchanged (same session, sequence, size and
     * CRC, §5.2), or null. Removes them. Flags and `start_ms` may change in between (TIME fixes
     * TIME_UNSET chunks), so they aren't compared.
     */
    @Synchronized
    fun take(e: ChunkEntry): ByteArray? {
        val k = entry
        val same = k != null && k.chunkId == e.chunkId && k.sessionId.contentEquals(e.sessionId) &&
            k.sequence == e.sequence && k.bytes == e.bytes && k.crc32 == e.crc32
        val b = if (same) bytes else null
        clear()
        return b
    }

    @Synchronized
    fun keep(e: ChunkEntry, buf: ByteArrayOutputStream) {
        if (buf.size() == 0) return clear()
        entry = e
        bytes = buf.toByteArray()
    }

    @Synchronized
    fun clear() {
        entry = null
        bytes = null
    }
}
