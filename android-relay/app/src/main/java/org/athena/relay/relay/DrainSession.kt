package org.athena.relay.relay

import org.athena.relay.protocol.ChunkEntry
import org.athena.relay.protocol.Command
import org.athena.relay.protocol.ErrorCode
import org.athena.relay.protocol.ProtocolException
import org.athena.relay.protocol.Response
import org.athena.relay.protocol.WatchGatt
import org.athena.relay.protocol.WatchInfo
import org.athena.relay.protocol.WatchStatus
import org.athena.relay.protocol.crc32
import java.io.ByteArrayOutputStream

/** How one connection ended. */
sealed class DrainOutcome {
    /** Everything listable was handled; [skipped] chunks stay on the watch. */
    data class Done(val uploaded: Int, val skipped: Int) : DrainOutcome()

    /** `/health` failed, or an upload hit a network error / 5xx / 408 / 429: back off. */
    data class ServerUnavailable(val detail: String, val uploaded: Int) : DrainOutcome()

    /** `401` / `403`: stop until the settings change. */
    data class AuthRejected(val code: Int, val uploaded: Int) : DrainOutcome()

    /**
     * The watch speaks another protocol version, sent something we can't use, or its SD card
     * failed during a read (§5.2): retry later with the backoff.
     */
    data class WatchError(val detail: String, val uploaded: Int) : DrainOutcome()

    /** The BLE link dropped or timed out. */
    data class LinkLost(val detail: String, val uploaded: Int) : DrainOutcome()
}

/** Live progress for the status screen. All callbacks are optional. */
interface DrainListener {
    fun onInfo(info: WatchInfo) {}
    fun onStatus(status: WatchStatus) {}
    fun onDraining() {}
    fun onUploaded(entry: ChunkEntry, duplicate: Boolean) {}
    fun onSkipped(entry: ChunkEntry, reason: String) {}
}

/**
 * One connection's work, docs/twatch-ble-protocol.md §6 steps 4-7, over an already set-up
 * [WatchLink] (steps 1-3 are BLE glue). Pure Kotlin so it runs in JVM unit tests.
 *
 * Delivery rule (§1): a chunk is `ACK`ed only after the server answered ok for it (and, for a
 * `LAST` chunk, after `/end` answered ok too).
 */
class DrainSession(
    private val link: WatchLink,
    private val uploader: Uploader,
    private val clock: Clock = Clock.SYSTEM,
    private val anchors: AnchorStore = MemoryAnchorStore(),
    private val partials: PartialReads = PartialReads(),
    private val log: RelayLog = RelayLog.NONE,
    private val listener: DrainListener = object : DrainListener {},
    private val receiveTimeoutMs: Long = 10_000,
) {
    private var uploaded = 0
    private var skipped = 0

    private class WatchProtocolError(message: String) : Exception(message)
    private class ServerStop(val outcome: DrainOutcome) : Exception()

    suspend fun run(): DrainOutcome = try {
        runSteps()
    } catch (e: LinkException) {
        log.log(Level.WARN, "link lost: ${e.message}")
        DrainOutcome.LinkLost(e.message ?: "link lost", uploaded)
    } catch (e: ServerStop) {
        bye()
        e.outcome
    } catch (e: WatchProtocolError) {
        log.log(Level.ERROR, "watch protocol error: ${e.message}")
        bye()
        DrainOutcome.WatchError(e.message ?: "protocol error", uploaded)
    } catch (e: ProtocolException) {
        log.log(Level.ERROR, "bad frame from watch: ${e.message}")
        bye()
        DrainOutcome.WatchError(e.message ?: "bad frame", uploaded)
    }

    private suspend fun runSteps(): DrainOutcome {
        // Step 4: info, status, TIME.
        val info = WatchInfo.parse(link.readInfo())
        if (info.proto != WatchGatt.PROTO_VERSION) {
            throw WatchProtocolError("watch speaks protocol ${info.values["proto"]}, this app speaks 1")
        }
        val device = info.device ?: throw WatchProtocolError("watch info has no device name")
        listener.onInfo(info)
        log.log(Level.INFO, "watch $device fw ${info.fw ?: "?"}")
        try {
            val status = WatchStatus.decode(link.readStatus())
            listener.onStatus(status)
            log.log(Level.INFO, "watch queue: ${status.queuedChunks} chunks, ${status.queuedMs / 1000} s")
        } catch (e: ProtocolException) {
            log.log(Level.WARN, "status unreadable: ${e.message}")
        }
        sendTime()

        // Step 5: don't pull anything the server can't take.
        when (val h = uploader.health()) {
            is ServerResult.Ok -> Unit
            is ServerResult.Transient -> throw ServerStop(DrainOutcome.ServerUnavailable(h.detail, 0))
            is ServerResult.Rejected -> throw ServerStop(DrainOutcome.ServerUnavailable("health: HTTP ${h.code}", 0))
            is ServerResult.AuthRejected -> throw ServerStop(DrainOutcome.ServerUnavailable("health: HTTP ${h.code}", 0))
        }

        // Step 6: drain.
        listener.onDraining()
        var afterId = 0L
        while (true) {
            val (entries, remaining) = list(afterId)
            for (e in entries) handle(device, e)
            if (entries.isEmpty()) {
                if (remaining > 0) log.log(Level.WARN, "LIST returned nothing but $remaining remaining")
                break
            }
            afterId = entries.last().chunkId
        }

        // Step 7.
        bye()
        log.log(Level.INFO, "drain done: $uploaded uploaded, $skipped skipped")
        return DrainOutcome.Done(uploaded, skipped)
    }

    private suspend fun sendTime() {
        val now = clock.nowMs()
        link.write(Command.Time(now).encode())
        when (val r = awaitFinal("TIME")) {
            is Response.TimeOk -> if (r.fixed > 0) log.log(Level.INFO, "watch clock set, ${r.fixed} chunks got timestamps")
            is Response.Error ->
                if (r.code == ErrorCode.BAD_TIME) log.log(Level.WARN, "watch refused the phone's clock (bad_time)")
                else throw WatchProtocolError("TIME failed: ${r.code}")
            else -> throw WatchProtocolError("unexpected answer to TIME: $r")
        }
    }

    private suspend fun list(afterId: Long): Pair<List<ChunkEntry>, Long> {
        link.write(Command.List(afterId, WatchGatt.LIST_MAX).encode())
        val entries = ArrayList<ChunkEntry>()
        var outOfOrder: Long? = null
        while (true) {
            when (val r = Response.decode(link.receive(receiveTimeoutMs))) {
                // §5.1: ids above after_id, ascending; anything else could loop forever. Read
                // up to LIST_END first, so the BYE that follows isn't refused as busy.
                is Response.Entry -> if (r.entry.chunkId > (entries.lastOrNull()?.chunkId ?: afterId)) entries += r.entry
                    else outOfOrder = outOfOrder ?: r.entry.chunkId
                is Response.ListEnd -> {
                    outOfOrder?.let { throw WatchProtocolError("LIST entry $it out of order") }
                    return entries.sortedBy { it.chunkId } to r.remaining
                }
                is Response.Error -> throw WatchProtocolError("LIST failed: ${r.code}")
                else -> log.log(Level.DEBUG, "ignored during LIST: $r")
            }
        }
    }

    private suspend fun handle(device: String, e: ChunkEntry) {
        val audio = when (val r = readChunk(e)) {
            is ReadResult.Ok -> r.bytes
            is ReadResult.Skip -> return skip(e, r.reason)
        }

        val timestampMs = if (e.timeUnset) {
            val anchor = anchors.anchorMs(e.sessionHex, clock.nowMs())
            log.log(Level.WARN, "chunk ${e.chunkId}: time unset, timestamp = relay anchor + ${e.startMs} ms")
            anchor + e.startMs
        } else e.startMs

        val up = uploader.upload(ChunkUpload(device, e.sessionHex, e.sequence, timestampMs, audio,
            kind = if (e.isNote) ChunkUpload.KIND_NOTE else null))
        val duplicate = when (up) {
            is ServerResult.Ok -> up.duplicate
            else -> return serverError(e, up, "upload")
        }
        if (e.isLast) {
            when (val end = uploader.endSession(e.sessionHex)) {
                is ServerResult.Ok -> log.log(Level.INFO, "session ${e.sessionHex} ended on the server")
                else -> return serverError(e, end, "end session")
            }
        }

        // Only now may the watch delete it (§1).
        link.write(Command.Ack(e.chunkId).encode())
        when (val r = awaitFinal("ACK")) {
            is Response.Acked -> if (r.chunkId != e.chunkId) {
                throw WatchProtocolError("ACKED for chunk ${r.chunkId}, expected ${e.chunkId}")
            }
            is Response.Error -> throw WatchProtocolError("ACK failed: ${r.code}")
            else -> throw WatchProtocolError("unexpected answer to ACK: $r")
        }
        uploaded++
        listener.onUploaded(e, duplicate)
        log.log(Level.INFO, "uploaded and acked $e" + if (duplicate) " (server already had it)" else "")
    }

    private fun serverError(e: ChunkEntry, r: ServerResult, what: String) {
        when (r) {
            is ServerResult.Transient -> {
                log.log(Level.WARN, "$what of chunk ${e.chunkId}: ${r.detail}; retrying later")
                throw ServerStop(DrainOutcome.ServerUnavailable(r.detail, uploaded))
            }
            is ServerResult.AuthRejected -> {
                log.log(Level.ERROR, "$what of chunk ${e.chunkId}: token rejected (HTTP ${r.code})")
                throw ServerStop(DrainOutcome.AuthRejected(r.code, uploaded))
            }
            is ServerResult.Rejected -> skip(e, "$what: HTTP ${r.code} ${r.detail}".trim())
            is ServerResult.Ok -> Unit
        }
    }

    private fun skip(e: ChunkEntry, reason: String) {
        skipped++
        log.log(Level.WARN, "skipped $e for this connection: $reason")
        listener.onSkipped(e, reason)
    }

    private sealed class ReadResult {
        class Ok(val bytes: ByteArray) : ReadResult()
        class Skip(val reason: String) : ReadResult()
    }

    /**
     * §5.2: READ, check offsets, size and CRC. On a mismatch read it again: 1 + [MAX_REREADS]
     * reads in total, then skip it for this connection.
     */
    private suspend fun readChunk(e: ChunkEntry): ReadResult {
        var rereads = 0
        while (true) {
            val buf = ByteArrayOutputStream(e.bytes.coerceIn(0, MAX_CHUNK_BYTES).toInt())
            partials.take(e)?.let {
                buf.write(it)
                log.log(Level.INFO, "resuming chunk ${e.chunkId} at byte ${it.size}")
            }
            link.write(Command.Read(e.chunkId, buf.size().toLong(), 0).encode())
            var problem: String? = null
            val end: Response.ReadEnd
            try {
                while (true) {
                    val r = Response.decode(link.receive(receiveTimeoutMs))
                    if (r is Response.ReadEnd) {
                        end = r
                        break
                    }
                    when (r) {
                        is Response.Data -> when {
                            problem != null -> Unit // keep draining until READ_END
                            r.chunkId != e.chunkId -> problem = "DATA for chunk ${r.chunkId}"
                            r.offset != buf.size().toLong() -> problem = "DATA offset ${r.offset}, expected ${buf.size()}"
                            buf.size().toLong() + r.payload.size > e.bytes -> problem = "more bytes than the index says"
                            else -> buf.write(r.payload)
                        }
                        is Response.Error -> when (r.code) {
                            // The chunk went away (acked by an earlier link): skip, go on.
                            ErrorCode.NOT_FOUND -> return ReadResult.Skip("not on the watch any more")
                            // §5.2: the card failed: stop the drain, BYE, back off.
                            ErrorCode.IO_ERROR -> throw WatchProtocolError("SD card read error on the watch (chunk ${e.chunkId})")
                            else -> throw WatchProtocolError("READ failed: ${r.code}")
                        }
                        else -> log.log(Level.DEBUG, "ignored during READ: $r")
                    }
                }
            } catch (ex: LinkException) {
                if (problem == null) partials.keep(e, buf)
                throw ex
            }

            if (end.status != 0) {
                // §5.2: the card failed. Keep what arrived intact; stop the drain and back off.
                if (problem == null) partials.keep(e, buf)
                throw WatchProtocolError("read of chunk ${e.chunkId} stopped early on the watch (status ${end.status})")
            }
            if (problem == null) {
                problem = when {
                    end.chunkId != e.chunkId -> "READ_END for chunk ${end.chunkId}"
                    buf.size().toLong() != e.bytes -> "got ${buf.size()} bytes, index says ${e.bytes}"
                    end.totalBytes != e.bytes -> "READ_END says ${end.totalBytes} bytes, index ${e.bytes}"
                    crc32(buf.toByteArray()) != e.crc32 -> "CRC-32 mismatch"
                    else -> null
                }
            }
            if (problem == null) return ReadResult.Ok(buf.toByteArray())

            rereads++
            if (rereads > MAX_REREADS) return ReadResult.Skip("$problem (after $MAX_REREADS re-reads)")
            log.log(Level.WARN, "chunk ${e.chunkId}: $problem; reading it again ($rereads/$MAX_REREADS)")
        }
    }

    /** Waits for the final response of a single-response command, skipping stray frames. */
    private suspend fun awaitFinal(what: String): Response {
        while (true) {
            val r = Response.decode(link.receive(receiveTimeoutMs))
            if (r is Response.Data || r is Response.Entry) {
                log.log(Level.DEBUG, "ignored during $what: $r")
                continue
            }
            return r
        }
    }

    private suspend fun bye() {
        try {
            link.write(Command.Bye.encode())
            awaitFinal("BYE")
        } catch (e: LinkException) {
            // The watch disconnects on its own after BYE or 30 s of silence.
        } catch (e: ProtocolException) {
            // Same: nothing more to do on this link.
        }
    }

    companion object {
        const val MAX_REREADS = 3

        /** Sanity cap for the buffer hint; a 30 s chunk is ~90 KB. */
        private const val MAX_CHUNK_BYTES = 4L * 1024 * 1024
    }
}
