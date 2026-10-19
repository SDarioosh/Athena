package org.barvis.sound.relay.protocol

/** Command opcodes (relay -> `ctrl`), §5. */
object Cmd {
    const val LIST = 0x01
    const val READ = 0x02
    const val ACK = 0x03
    const val TIME = 0x04
    const val BYE = 0x05
}

/** Response opcodes (`data` notifications), §5. */
object Op {
    const val ENTRY = 0x81
    const val DATA = 0x82
    const val READ_END = 0x83
    const val ACKED = 0x84
    const val LIST_END = 0x85
    const val TIME_OK = 0x86
    const val BYE_OK = 0x87
    const val ERROR = 0xEE
}

/** Error codes, §5.6. */
enum class ErrorCode(val code: Int) {
    BAD_LENGTH(1), UNKNOWN_CMD(2), NOT_FOUND(3), BUSY(4), MTU_TOO_SMALL(5), IO_ERROR(6), BAD_TIME(7),
    UNKNOWN(-1);

    companion object {
        fun of(code: Int): ErrorCode = entries.firstOrNull { it.code == code } ?: UNKNOWN
    }
}

/** A command the relay writes to `ctrl`. */
sealed class Command {
    abstract fun encode(): ByteArray

    data class List(val afterId: Long, val max: Int) : Command() {
        override fun encode(): ByteArray = ByteArray(7).also {
            Le.putU8(it, 0, Cmd.LIST); Le.putU32(it, 1, afterId); Le.putU16(it, 5, max)
        }
    }

    /** `length = 0` reads to the end of the file. */
    data class Read(val chunkId: Long, val offset: Long, val length: Long = 0) : Command() {
        override fun encode(): ByteArray = ByteArray(13).also {
            Le.putU8(it, 0, Cmd.READ); Le.putU32(it, 1, chunkId)
            Le.putU32(it, 5, offset); Le.putU32(it, 9, length)
        }
    }

    data class Ack(val chunkId: Long) : Command() {
        override fun encode(): ByteArray = ByteArray(5).also {
            Le.putU8(it, 0, Cmd.ACK); Le.putU32(it, 1, chunkId)
        }
    }

    data class Time(val epochMs: Long) : Command() {
        override fun encode(): ByteArray = ByteArray(12).also {
            Le.putU8(it, 0, Cmd.TIME); Le.putU64(it, 4, epochMs)
        }
    }

    data object Bye : Command() {
        override fun encode(): ByteArray = byteArrayOf(Cmd.BYE.toByte())
    }

    companion object {
        /** Decodes a command (used by the in-memory fake watch in tests). */
        fun decode(b: ByteArray): Command {
            if (b.isEmpty()) throw ProtocolException("empty command")
            fun need(n: Int) { if (b.size != n) throw ProtocolException("bad command length ${b.size}") }
            return when (Le.u8(b, 0)) {
                Cmd.LIST -> { need(7); List(Le.u32(b, 1), Le.u16(b, 5)) }
                Cmd.READ -> { need(13); Read(Le.u32(b, 1), Le.u32(b, 5), Le.u32(b, 9)) }
                Cmd.ACK -> { need(5); Ack(Le.u32(b, 1)) }
                Cmd.TIME -> { need(12); Time(Le.u64(b, 4)) }
                Cmd.BYE -> { need(1); Bye }
                else -> throw ProtocolException("unknown command 0x%02x".format(Le.u8(b, 0)))
            }
        }
    }
}

/** One queued chunk on the watch, §4.3. */
data class ChunkEntry(
    val flags: Int,
    val chunkId: Long,
    val sessionId: ByteArray,
    val sequence: Long,
    val startMs: Long,
    val durationMs: Long,
    val bytes: Long,
    val crc32: Long,
) {
    init {
        require(sessionId.size == 8) { "session id must be 8 bytes" }
    }

    val isLast: Boolean get() = flags and FLAG_LAST != 0
    val timeUnset: Boolean get() = flags and FLAG_TIME_UNSET != 0
    /** The chunk belongs to a voice note (a short session of its own). */
    val isNote: Boolean get() = flags and FLAG_NOTE != 0

    /** 16 lowercase hex digits (§2.2). */
    val sessionHex: String get() = sessionId.toHex()

    fun encode(): ByteArray = ByteArray(SIZE).also {
        Le.putU8(it, 0, Op.ENTRY); Le.putU8(it, 1, flags)
        Le.putU32(it, 4, chunkId)
        sessionId.copyInto(it, 8)
        Le.putU32(it, 16, sequence)
        Le.putU64(it, 20, startMs)
        Le.putU32(it, 28, durationMs)
        Le.putU32(it, 32, bytes)
        Le.putU32(it, 36, crc32)
    }

    override fun equals(other: Any?): Boolean =
        other is ChunkEntry && flags == other.flags && chunkId == other.chunkId &&
            sessionId.contentEquals(other.sessionId) && sequence == other.sequence &&
            startMs == other.startMs && durationMs == other.durationMs && bytes == other.bytes &&
            crc32 == other.crc32

    override fun hashCode(): Int = chunkId.hashCode() * 31 + sessionId.contentHashCode()

    override fun toString(): String =
        "chunk $chunkId (session $sessionHex seq $sequence, $bytes B, ${durationMs} ms" +
            (if (isLast) ", last" else "") + (if (timeUnset) ", time unset" else "") +
            (if (isNote) ", note" else "") + ")"

    companion object {
        const val SIZE = 40
        const val FLAG_LAST = 0x01
        const val FLAG_TIME_UNSET = 0x02
        const val FLAG_NOTE = 0x04

        fun decode(b: ByteArray): ChunkEntry {
            // Extra trailing bytes are allowed: fields may be appended in proto 1 (§9).
            if (b.size < SIZE || Le.u8(b, 0) != Op.ENTRY) throw ProtocolException("bad ENTRY")
            return ChunkEntry(
                flags = Le.u8(b, 1),
                chunkId = Le.u32(b, 4),
                sessionId = b.copyOfRange(8, 16),
                sequence = Le.u32(b, 16),
                startMs = Le.u64(b, 20),
                durationMs = Le.u32(b, 28),
                bytes = Le.u32(b, 32),
                crc32 = Le.u32(b, 36),
            )
        }
    }
}

/** A `data` notification from the watch, §5. */
sealed class Response {
    abstract fun encode(): ByteArray

    data class Entry(val entry: ChunkEntry) : Response() {
        override fun encode() = entry.encode()
    }

    class Data(val chunkId: Long, val offset: Long, val payload: ByteArray) : Response() {
        override fun encode(): ByteArray = ByteArray(9 + payload.size).also {
            Le.putU8(it, 0, Op.DATA); Le.putU32(it, 1, chunkId); Le.putU32(it, 5, offset)
            payload.copyInto(it, 9)
        }

        override fun toString() = "Data(chunk=$chunkId, offset=$offset, ${payload.size} B)"
    }

    data class ReadEnd(val status: Int, val chunkId: Long, val totalBytes: Long, val crc32: Long) : Response() {
        override fun encode(): ByteArray = ByteArray(16).also {
            Le.putU8(it, 0, Op.READ_END); Le.putU8(it, 1, status); Le.putU32(it, 4, chunkId)
            Le.putU32(it, 8, totalBytes); Le.putU32(it, 12, crc32)
        }
    }

    /** result 0 = deleted, 1 = was not queued; both are success (§5.3). */
    data class Acked(val result: Int, val chunkId: Long) : Response() {
        override fun encode(): ByteArray = ByteArray(8).also {
            Le.putU8(it, 0, Op.ACKED); Le.putU8(it, 1, result); Le.putU32(it, 4, chunkId)
        }
    }

    data class ListEnd(val count: Int, val remaining: Long) : Response() {
        override fun encode(): ByteArray = ByteArray(8).also {
            Le.putU8(it, 0, Op.LIST_END); Le.putU16(it, 2, count); Le.putU32(it, 4, remaining)
        }
    }

    data class TimeOk(val fixed: Int) : Response() {
        override fun encode(): ByteArray = ByteArray(4).also {
            Le.putU8(it, 0, Op.TIME_OK); Le.putU16(it, 2, fixed)
        }
    }

    data object ByeOk : Response() {
        override fun encode(): ByteArray = byteArrayOf(Op.BYE_OK.toByte())
    }

    data class Error(val cmd: Int, val code: ErrorCode, val rawCode: Int, val arg: Long) : Response() {
        constructor(cmd: Int, code: ErrorCode, arg: Long = 0) : this(cmd, code, code.code, arg)

        override fun encode(): ByteArray = ByteArray(8).also {
            Le.putU8(it, 0, Op.ERROR); Le.putU8(it, 1, cmd); Le.putU8(it, 2, rawCode)
            Le.putU32(it, 4, arg)
        }
    }

    companion object {
        fun decode(b: ByteArray): Response {
            if (b.isEmpty()) throw ProtocolException("empty notification")
            fun need(n: Int, what: String) { if (b.size < n) throw ProtocolException("short $what (${b.size} B)") }
            return when (Le.u8(b, 0)) {
                Op.ENTRY -> Entry(ChunkEntry.decode(b))
                Op.DATA -> { need(9, "DATA"); Data(Le.u32(b, 1), Le.u32(b, 5), b.copyOfRange(9, b.size)) }
                Op.READ_END -> { need(16, "READ_END"); ReadEnd(Le.u8(b, 1), Le.u32(b, 4), Le.u32(b, 8), Le.u32(b, 12)) }
                Op.ACKED -> { need(8, "ACKED"); Acked(Le.u8(b, 1), Le.u32(b, 4)) }
                Op.LIST_END -> { need(8, "LIST_END"); ListEnd(Le.u16(b, 2), Le.u32(b, 4)) }
                Op.TIME_OK -> { need(4, "TIME_OK"); TimeOk(Le.u16(b, 2)) }
                Op.BYE_OK -> ByeOk
                Op.ERROR -> {
                    need(8, "ERROR")
                    val raw = Le.u8(b, 2)
                    Error(Le.u8(b, 1), ErrorCode.of(raw), raw, Le.u32(b, 4))
                }
                else -> throw ProtocolException("unknown response 0x%02x".format(Le.u8(b, 0)))
            }
        }
    }
}
