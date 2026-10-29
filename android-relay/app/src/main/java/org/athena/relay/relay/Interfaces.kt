package org.athena.relay.relay

import java.io.IOException

/**
 * The BLE link to one connected, bonded watch, after MTU negotiation and subscription to
 * `data` (docs/twatch-ble-protocol.md §6 steps 1-3). The Android implementation is
 * [org.athena.relay.ble.BleWatchLink]; tests use an in-memory fake watch.
 */
interface WatchLink {
    /** Reads the `info` characteristic. */
    suspend fun readInfo(): ByteArray

    /** Reads the `status` characteristic. */
    suspend fun readStatus(): ByteArray

    /** Writes one command to `ctrl` (Write Request) and returns once the write is confirmed. */
    suspend fun write(command: ByteArray)

    /**
     * Returns the next `data` notification, in order. Throws [LinkException] on timeout or
     * disconnect.
     */
    suspend fun receive(timeoutMs: Long): ByteArray
}

/** The link is gone or stopped answering. */
open class LinkException(message: String, cause: Throwable? = null) : IOException(message, cause)

/** Result of one HTTP call to the owner's server, classified per §6 step 6.5. */
sealed class ServerResult {
    /** `200` with `"ok": true` (a `"duplicate": true` answer counts as ok). */
    data class Ok(val duplicate: Boolean = false) : ServerResult()

    /** Network error, timeout, `5xx`, `408`, `429` (or an answer that isn't ours): retry later. */
    data class Transient(val detail: String) : ServerResult()

    /** `401` / `403`: stop until the settings change. */
    data class AuthRejected(val code: Int) : ServerResult()

    /** Any other `4xx`: skip this chunk for this connection. */
    data class Rejected(val code: Int, val detail: String) : ServerResult()
}

/**
 * One chunk upload (§6 step 6.2). [kind] is the session kind form field: `note` for a voice
 * note, `null` to omit the field (the server then defaults to `conversation`).
 */
class ChunkUpload(
    val device: String,
    val sessionId: String,
    val sequence: Long,
    val timestampMs: Long,
    val audio: ByteArray,
    val kind: String? = null,
) {
    companion object {
        const val KIND_NOTE = "note"
    }
}

/** The owner's server. The Android app uses [org.athena.relay.net.HttpUploader]. */
interface Uploader {
    /** `GET /health` (no token). */
    suspend fun health(): ServerResult

    /** `POST /v1/ingest/chunk`. */
    suspend fun upload(chunk: ChunkUpload): ServerResult

    /** `POST /v1/sessions/{id}/end`. */
    suspend fun endSession(sessionId: String): ServerResult
}

fun interface Clock {
    fun nowMs(): Long

    companion object {
        val SYSTEM = Clock { System.currentTimeMillis() }
    }
}

enum class Level { DEBUG, INFO, WARN, ERROR }

/** Event log. Implementations must never receive tokens or audio (callers don't pass them). */
fun interface RelayLog {
    fun log(level: Level, message: String)

    companion object {
        val NONE = RelayLog { _, _ -> }
    }
}

/**
 * Anchors for chunks that are still `TIME_UNSET` (§2.3): the relay's clock when it first
 * saw that session. The app persists them so a session keeps its anchor across reconnects.
 */
interface AnchorStore {
    fun anchorMs(sessionId: String, nowMs: Long): Long
}

class MemoryAnchorStore : AnchorStore {
    private val anchors = HashMap<String, Long>()

    @Synchronized
    override fun anchorMs(sessionId: String, nowMs: Long): Long = anchors.getOrPut(sessionId) { nowMs }
}
