package org.athena.relay.relay

import org.athena.relay.protocol.ChunkEntry
import org.athena.relay.protocol.Cmd
import org.athena.relay.protocol.Command
import org.athena.relay.protocol.ErrorCode
import org.athena.relay.protocol.Response
import org.athena.relay.protocol.WatchGatt
import org.athena.relay.protocol.WatchStatus
import org.athena.relay.protocol.crc32
import org.athena.relay.protocol.hexToBytes
import kotlin.random.Random

/** Shared timeline of watch commands and server calls, to check ordering. */
class Timeline {
    val events = mutableListOf<String>()
    fun add(e: String) = synchronized(events) { events += e }
}

/** An in-memory watch: the GATT server side of docs/twatch-ble-protocol.md §5. */
class FakeWatch(
    private val timeline: Timeline = Timeline(),
    var mtu: Int = 517,
    var info: String = "proto=1;device=twatch-ultra;fw=0.2.0;board=twatch_ultra",
    /** The real watch caps LIST at 32; a smaller cap exercises paging. */
    var listCap: Int = WatchGatt.LIST_MAX,
) : WatchLink {
    class Chunk(var entry: ChunkEntry, val data: ByteArray, var fixableOnTime: Boolean = false)

    val chunks = sortedMapOf<Long, Chunk>()
    val commands = mutableListOf<Command>()
    val dataFrameSizes = mutableListOf<Int>()
    var busyErrors = 0
    var clockMs: Long? = null
    var connected = true

    /** chunk id -> how many more READs of it return a corrupted byte. */
    val corruptReads = mutableMapOf<Long, Int>()
    /** chunk id -> drop the link after this many bytes were streamed (once). */
    val disconnectAfterBytes = mutableMapOf<Long, Int>()
    /** chunk ids whose READ answers ERROR io_error. */
    val ioErrorOnRead = mutableSetOf<Long>()
    /** chunk id -> READ stops after this many bytes with READ_END status 1 (once). */
    val sdFailAfterBytes = mutableMapOf<Long, Int>()
    /** chunk ids the watch "forgets" just before their ACK (as if acked by an earlier link). */
    val forgetBeforeAck = mutableSetOf<Long>()
    /** A buggy firmware: the `after_id` it really lists from, given the one asked for. */
    var listFrom: (Long) -> Long = { it }
    /** Fails the test instead of letting a relay loop forever. */
    var maxLists = 100

    private val pending = ArrayDeque<ByteArray>()
    private var nextId = 1L

    fun add(
        sessionHex: String,
        sequence: Long,
        size: Int = 3000,
        last: Boolean = false,
        timeUnset: Boolean = false,
        note: Boolean = false,
        fixableOnTime: Boolean = false,
        startMs: Long = 1792411200000L + sequence * 30_000,
    ): ChunkEntry {
        val data = Random(nextId * 7919 + sequence).nextBytes(size)
        var flags = 0
        if (last) flags = flags or ChunkEntry.FLAG_LAST
        if (timeUnset) flags = flags or ChunkEntry.FLAG_TIME_UNSET
        if (note) flags = flags or ChunkEntry.FLAG_NOTE
        val e = ChunkEntry(flags, nextId++, hexToBytes(sessionHex), sequence, startMs, 30_000, size.toLong(), crc32(data))
        chunks[e.chunkId] = Chunk(e, data, fixableOnTime)
        return e
    }

    fun dataOf(id: Long): ByteArray = chunks.getValue(id).data

    fun status(): WatchStatus = WatchStatus(
        proto = 1, flags = 0x25, batteryPercent = 80, batteryMv = 3900, queuedChunks = chunks.size,
        queuedBytes = chunks.values.sumOf { it.entry.bytes }, queuedMs = chunks.values.sumOf { it.entry.durationMs },
        clockEpochS = (clockMs ?: 0) / 1000, bootId = 0x01020304,
    )

    /** A new connection: the relay reconnects to the same watch. */
    fun reconnect() {
        connected = true
        pending.clear()
    }

    override suspend fun readInfo(): ByteArray = checkLink().let { info.toByteArray() }

    override suspend fun readStatus(): ByteArray = checkLink().let { status().encode() }

    override suspend fun write(command: ByteArray) {
        checkLink()
        if (pending.isNotEmpty()) {
            // A command is still running (its final response wasn't consumed): §5 busy rule.
            busyErrors++
            pending.addLast(Response.Error(command[0].toInt() and 0xFF, ErrorCode.BUSY).encode())
            return
        }
        if (mtu < WatchGatt.MIN_MTU) {
            pending.addLast(Response.Error(command[0].toInt() and 0xFF, ErrorCode.MTU_TOO_SMALL, mtu.toLong()).encode())
            return
        }
        val c = Command.decode(command)
        commands += c
        timeline.add("watch:" + describe(c))
        when (c) {
            is Command.List -> {
                check(commands.count { it is Command.List } <= maxLists) { "relay sent more than $maxLists LISTs" }
                val all = chunks.tailMap(listFrom(c.afterId) + 1).values.toList()
                val sent = all.take(minOf(c.max, listCap))
                sent.forEach { pending.addLast(it.entry.encode()) }
                pending.addLast(Response.ListEnd(sent.size, (all.size - sent.size).toLong()).encode())
            }
            is Command.Read -> read(c)
            is Command.Ack -> {
                if (c.chunkId in forgetBeforeAck) chunks.remove(c.chunkId)
                val removed = chunks.remove(c.chunkId) != null
                pending.addLast(Response.Acked(if (removed) 0 else 1, c.chunkId).encode())
            }
            is Command.Time -> {
                if (c.epochMs < 1704067200000L) {
                    pending.addLast(Response.Error(Cmd.TIME, ErrorCode.BAD_TIME).encode())
                    return
                }
                clockMs = c.epochMs
                var fixed = 0
                for (ch in chunks.values) if (ch.entry.timeUnset && ch.fixableOnTime) {
                    // Pretend the session started 10 min before this sync.
                    ch.entry = ch.entry.copy(flags = ch.entry.flags and ChunkEntry.FLAG_TIME_UNSET.inv(),
                        startMs = c.epochMs - 600_000 + ch.entry.startMs)
                    fixed++
                }
                pending.addLast(Response.TimeOk(fixed).encode())
            }
            Command.Bye -> pending.addLast(Response.ByeOk.encode())
        }
    }

    private fun read(c: Command.Read) {
        val ch = chunks[c.chunkId]
        if (ch == null) {
            pending.addLast(Response.Error(Cmd.READ, ErrorCode.NOT_FOUND, c.chunkId).encode())
            return
        }
        if (c.chunkId in ioErrorOnRead) {
            pending.addLast(Response.Error(Cmd.READ, ErrorCode.IO_ERROR, c.chunkId).encode())
            return
        }
        val sdFail = sdFailAfterBytes.remove(c.chunkId)
        val data = ch.data.copyOf()
        val corrupt = (corruptReads[c.chunkId] ?: 0) > 0
        if (corrupt) {
            corruptReads[c.chunkId] = corruptReads.getValue(c.chunkId) - 1
            data[data.size / 2] = (data[data.size / 2] + 1).toByte()
        }
        val end = if (c.length == 0L) data.size else minOf(data.size.toLong(), c.offset + c.length).toInt()
        val maxPayload = WatchGatt.dataPayloadMax(mtu)
        var off = c.offset.toInt()
        val dropAt = disconnectAfterBytes.remove(c.chunkId)
        var sent = 0
        while (off < end) {
            if (sdFail != null && sent >= sdFail) {
                pending.addLast(Response.ReadEnd(1, c.chunkId, data.size.toLong(), ch.entry.crc32).encode())
                return
            }
            if (dropAt != null && sent >= dropAt) {
                pending.addLast(DROP)
                return
            }
            val n = minOf(maxPayload, end - off)
            val frame = Response.Data(c.chunkId, off.toLong(), data.copyOfRange(off, off + n)).encode()
            dataFrameSizes += frame.size
            pending.addLast(frame)
            off += n
            sent += n
        }
        pending.addLast(Response.ReadEnd(0, c.chunkId, data.size.toLong(), ch.entry.crc32).encode())
    }

    override suspend fun receive(timeoutMs: Long): ByteArray {
        checkLink()
        val f = pending.removeFirstOrNull() ?: throw LinkException("timeout waiting for the watch")
        if (f === DROP) {
            connected = false
            pending.clear()
            throw LinkException("disconnected")
        }
        return f
    }

    private fun checkLink() {
        if (!connected) throw LinkException("not connected")
    }

    private fun describe(c: Command) = when (c) {
        is Command.List -> "LIST ${c.afterId}"
        is Command.Read -> "READ ${c.chunkId}@${c.offset}"
        is Command.Ack -> "ACK ${c.chunkId}"
        is Command.Time -> "TIME"
        Command.Bye -> "BYE"
    }

    companion object {
        private val DROP = ByteArray(0)
    }
}

/** Fake server: records calls, answers from [uploadAnswer] / [endAnswer]. */
class FakeUploader(private val timeline: Timeline = Timeline()) : Uploader {
    var healthAnswer: ServerResult = ServerResult.Ok()
    var uploadAnswer: (ChunkUpload) -> ServerResult = { ServerResult.Ok() }
    var endAnswer: (String) -> ServerResult = { ServerResult.Ok() }
    val uploads = mutableListOf<ChunkUpload>()
    val ends = mutableListOf<String>()
    var healthCalls = 0

    override suspend fun health(): ServerResult {
        healthCalls++
        timeline.add("server:health")
        return healthAnswer
    }

    override suspend fun upload(chunk: ChunkUpload): ServerResult {
        uploads += chunk
        timeline.add("server:upload ${chunk.sessionId}/${chunk.sequence}")
        return uploadAnswer(chunk)
    }

    override suspend fun endSession(sessionId: String): ServerResult {
        ends += sessionId
        timeline.add("server:end $sessionId")
        return endAnswer(sessionId)
    }
}
