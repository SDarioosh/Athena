package org.barvis.sound.relay.protocol

import java.util.UUID

/**
 * The T5 E-Paper PDA's "glance" protocol v1, docs/t5-pda-plan.md §3. Framing, the
 * one-command-at-a-time rule and the `ERROR` format are those of the watch protocol (§5).
 */
object PdaGatt {
    val SERVICE: UUID = UUID.fromString("d0230000-51f4-43be-86e4-1653768283cd")
    val INFO: UUID = UUID.fromString("d0230001-51f4-43be-86e4-1653768283cd")
    val STATUS: UUID = UUID.fromString("d0230002-51f4-43be-86e4-1653768283cd")
    val CTRL: UUID = UUID.fromString("d0230003-51f4-43be-86e4-1653768283cd")
    val DATA: UUID = UUID.fromString("d0230004-51f4-43be-86e4-1653768283cd")

    val PROFILE = GattProfile(SERVICE, DATA, STATUS)

    const val PROTO_VERSION = 1
    const val REQUESTED_MTU = 517

    /** One `OB_ENTRY` (16 + 400 bytes of JSON) must fit in one notification (§3.3). */
    const val MIN_MTU = 420

    /** ATT header (3) + `PUT_DATA` header (5). */
    const val PUT_OVERHEAD = 8

    const val OB_LIST_MAX = 16

    /** §3.1. */
    const val MAX_BLOB = 256 * 1024

    /**
     * Scan response (plan §3.5): Service Data for the 128-bit service UUID (AD type 0x21),
     * one flags byte, bit0 = the outbox isn't empty.
     */
    const val ADV_OUTBOX_WAITING = 0x01

    fun outboxWaiting(serviceData: ByteArray?): Boolean =
        serviceData != null && serviceData.isNotEmpty() && (serviceData[0].toInt() and ADV_OUTBOX_WAITING) != 0
}

/** Blob kinds, §3.1. */
object BlobKind {
    const val FEED = 1
    const val DETAIL = 2
}

/** Command opcodes (relay -> `ctrl`), §3.3. `TIME` and `BYE` are the watch's. */
object PdaCmd {
    const val TIME = 0x04
    const val BYE = 0x05
    const val PUT_BEGIN = 0x10
    const val PUT_DATA = 0x11
    const val PUT_END = 0x12
    const val OB_LIST = 0x20
    const val OB_ACK = 0x21
}

/** Response opcodes (`data` notifications), §3.3. */
object PdaOp {
    const val TIME_OK = 0x86
    const val BYE_OK = 0x87
    const val PUT_READY = 0x90
    const val PUT_OK = 0x91
    const val OB_ENTRY = 0xA0
    const val OB_END = 0xA1
    const val OB_ACKED = 0xA2
    const val ERROR = 0xEE
}

/** Error codes: the watch's (§5.6) plus the PDA's 8-10. */
enum class PdaErrorCode(val code: Int) {
    BAD_LENGTH(1), UNKNOWN_CMD(2), NOT_FOUND(3), BUSY(4), MTU_TOO_SMALL(5), IO_ERROR(6), BAD_TIME(7),
    BAD_CRC(8), TOO_LARGE(9), BAD_STATE(10),
    UNKNOWN(-1);

    companion object {
        fun of(code: Int): PdaErrorCode = entries.firstOrNull { it.code == code } ?: UNKNOWN
    }
}

/** `OB_ACK.result`, §3.3. The relay never acks a temporary failure. */
object AckResult {
    const val DONE = 0
    const val REJECTED = 1
}

/** A command the relay writes to the PDA's `ctrl`. */
sealed class PdaCommand {
    abstract fun encode(): ByteArray

    data class Time(val epochMs: Long, val tzOffsetMin: Int) : PdaCommand() {
        override fun encode(): ByteArray = ByteArray(14).also {
            Le.putU8(it, 0, PdaCmd.TIME); Le.putU64(it, 4, epochMs); Le.putU16(it, 12, tzOffsetMin)
        }
    }

    data class PutBegin(val kind: Int, val blobId: Long, val total: Long, val crc32: Long) : PdaCommand() {
        override fun encode(): ByteArray = ByteArray(16).also {
            Le.putU8(it, 0, PdaCmd.PUT_BEGIN); Le.putU8(it, 1, kind)
            Le.putU32(it, 4, blobId); Le.putU32(it, 8, total); Le.putU32(it, 12, crc32)
        }
    }

    class PutData(val offset: Long, val payload: ByteArray) : PdaCommand() {
        override fun encode(): ByteArray = ByteArray(5 + payload.size).also {
            Le.putU8(it, 0, PdaCmd.PUT_DATA); Le.putU32(it, 1, offset); payload.copyInto(it, 5)
        }

        override fun equals(other: Any?) =
            other is PutData && offset == other.offset && payload.contentEquals(other.payload)

        override fun hashCode() = offset.hashCode() * 31 + payload.contentHashCode()
        override fun toString() = "PutData(offset=$offset, ${payload.size} B)"
    }

    data object PutEnd : PdaCommand() {
        override fun encode(): ByteArray = byteArrayOf(PdaCmd.PUT_END.toByte())
    }

    data class ObList(val afterId: Long, val max: Int) : PdaCommand() {
        override fun encode(): ByteArray = ByteArray(7).also {
            Le.putU8(it, 0, PdaCmd.OB_LIST); Le.putU32(it, 1, afterId); Le.putU16(it, 5, max)
        }
    }

    data class ObAck(val actionId: Long, val result: Int) : PdaCommand() {
        override fun encode(): ByteArray = ByteArray(6).also {
            Le.putU8(it, 0, PdaCmd.OB_ACK); Le.putU32(it, 1, actionId); Le.putU8(it, 5, result)
        }
    }

    data object Bye : PdaCommand() {
        override fun encode(): ByteArray = byteArrayOf(PdaCmd.BYE.toByte())
    }

    companion object {
        /** Decodes a command (used by the in-memory fake PDA in tests). */
        fun decode(b: ByteArray): PdaCommand {
            if (b.isEmpty()) throw ProtocolException("empty command")
            fun need(n: Int) { if (b.size != n) throw ProtocolException("bad command length ${b.size}") }
            return when (Le.u8(b, 0)) {
                PdaCmd.TIME -> { need(14); Time(Le.u64(b, 4), Le.u16(b, 12).toShort().toInt()) }
                PdaCmd.PUT_BEGIN -> { need(16); PutBegin(Le.u8(b, 1), Le.u32(b, 4), Le.u32(b, 8), Le.u32(b, 12)) }
                PdaCmd.PUT_DATA -> {
                    if (b.size < 5) throw ProtocolException("short PUT_DATA")
                    PutData(Le.u32(b, 1), b.copyOfRange(5, b.size))
                }
                PdaCmd.PUT_END -> { need(1); PutEnd }
                PdaCmd.OB_LIST -> { need(7); ObList(Le.u32(b, 1), Le.u16(b, 5)) }
                PdaCmd.OB_ACK -> { need(6); ObAck(Le.u32(b, 1), Le.u8(b, 5)) }
                PdaCmd.BYE -> { need(1); Bye }
                else -> throw ProtocolException("unknown command 0x%02x".format(Le.u8(b, 0)))
            }
        }
    }
}

/** One queued action in the PDA's outbox (`OB_ENTRY`), §3.3 and §4.2. */
class OutboxEntry(val actionId: Long, val createdMs: Long, val json: ByteArray) {
    fun encode(): ByteArray = ByteArray(HEADER + json.size).also {
        Le.putU8(it, 0, PdaOp.OB_ENTRY); Le.putU16(it, 2, json.size)
        Le.putU32(it, 4, actionId); Le.putU64(it, 8, createdMs)
        json.copyInto(it, HEADER)
    }

    val text: String get() = json.toString(Charsets.UTF_8)

    override fun equals(other: Any?) = other is OutboxEntry && actionId == other.actionId &&
        createdMs == other.createdMs && json.contentEquals(other.json)

    override fun hashCode() = actionId.hashCode()
    override fun toString() = "action $actionId (${json.size} B)"

    companion object {
        const val HEADER = 16
        const val MAX_JSON = 400

        fun decode(b: ByteArray): OutboxEntry {
            if (b.size < HEADER || Le.u8(b, 0) != PdaOp.OB_ENTRY) throw ProtocolException("bad OB_ENTRY")
            val len = Le.u16(b, 2)
            if (b.size < HEADER + len) throw ProtocolException("OB_ENTRY says $len B of JSON, got ${b.size - HEADER}")
            return OutboxEntry(Le.u32(b, 4), Le.u64(b, 8), b.copyOfRange(HEADER, HEADER + len))
        }
    }
}

/** A `data` notification from the PDA, §3.3. */
sealed class PdaResponse {
    abstract fun encode(): ByteArray

    data object TimeOk : PdaResponse() {
        override fun encode(): ByteArray = byteArrayOf(PdaOp.TIME_OK.toByte(), 0, 0, 0)
    }

    data object ByeOk : PdaResponse() {
        override fun encode(): ByteArray = byteArrayOf(PdaOp.BYE_OK.toByte())
    }

    data class PutReady(val kind: Int, val blobId: Long) : PdaResponse() {
        override fun encode(): ByteArray = ByteArray(8).also {
            Le.putU8(it, 0, PdaOp.PUT_READY); Le.putU8(it, 1, kind); Le.putU32(it, 4, blobId)
        }
    }

    data class PutOk(val kind: Int, val blobId: Long) : PdaResponse() {
        override fun encode(): ByteArray = ByteArray(8).also {
            Le.putU8(it, 0, PdaOp.PUT_OK); Le.putU8(it, 1, kind); Le.putU32(it, 4, blobId)
        }
    }

    data class Entry(val entry: OutboxEntry) : PdaResponse() {
        override fun encode() = entry.encode()
    }

    data class ObEnd(val count: Int, val remaining: Long) : PdaResponse() {
        override fun encode(): ByteArray = ByteArray(8).also {
            Le.putU8(it, 0, PdaOp.OB_END); Le.putU16(it, 2, count); Le.putU32(it, 4, remaining)
        }
    }

    data class ObAcked(val result: Int, val actionId: Long) : PdaResponse() {
        override fun encode(): ByteArray = ByteArray(8).also {
            Le.putU8(it, 0, PdaOp.OB_ACKED); Le.putU8(it, 1, result); Le.putU32(it, 4, actionId)
        }
    }

    data class Error(val cmd: Int, val code: PdaErrorCode, val rawCode: Int, val arg: Long) : PdaResponse() {
        constructor(cmd: Int, code: PdaErrorCode, arg: Long = 0) : this(cmd, code, code.code, arg)

        override fun encode(): ByteArray = ByteArray(8).also {
            Le.putU8(it, 0, PdaOp.ERROR); Le.putU8(it, 1, cmd); Le.putU8(it, 2, rawCode)
            Le.putU32(it, 4, arg)
        }
    }

    companion object {
        fun decode(b: ByteArray): PdaResponse {
            if (b.isEmpty()) throw ProtocolException("empty notification")
            fun need(n: Int, what: String) { if (b.size < n) throw ProtocolException("short $what (${b.size} B)") }
            return when (Le.u8(b, 0)) {
                PdaOp.TIME_OK -> TimeOk
                PdaOp.BYE_OK -> ByeOk
                PdaOp.PUT_READY -> { need(8, "PUT_READY"); PutReady(Le.u8(b, 1), Le.u32(b, 4)) }
                PdaOp.PUT_OK -> { need(8, "PUT_OK"); PutOk(Le.u8(b, 1), Le.u32(b, 4)) }
                PdaOp.OB_ENTRY -> Entry(OutboxEntry.decode(b))
                PdaOp.OB_END -> { need(8, "OB_END"); ObEnd(Le.u16(b, 2), Le.u32(b, 4)) }
                PdaOp.OB_ACKED -> { need(8, "OB_ACKED"); ObAcked(Le.u8(b, 1), Le.u32(b, 4)) }
                PdaOp.ERROR -> {
                    need(8, "ERROR")
                    val raw = Le.u8(b, 2)
                    Error(Le.u8(b, 1), PdaErrorCode.of(raw), raw, Le.u32(b, 4))
                }
                else -> throw ProtocolException("unknown response 0x%02x".format(Le.u8(b, 0)))
            }
        }
    }
}

/** The PDA's `status` characteristic, §3.2 (16 bytes; longer values are allowed). */
data class PdaStatus(
    val proto: Int,
    val flags: Int,
    val batteryPercent: Int,
    val outboxWaiting: Int,
    val feedVersion: Long,
    val bootId: Long,
) {
    val timeValid get() = flags and 0x01 != 0
    val charging get() = flags and 0x02 != 0
    val usbPower get() = flags and 0x04 != 0
    val pairingWindowOpen get() = flags and 0x08 != 0
    val userActive get() = flags and 0x10 != 0

    fun encode(): ByteArray = ByteArray(SIZE).also {
        Le.putU8(it, 0, proto); Le.putU8(it, 1, flags); Le.putU8(it, 2, batteryPercent)
        Le.putU16(it, 4, outboxWaiting); Le.putU32(it, 8, feedVersion); Le.putU32(it, 12, bootId)
    }

    companion object {
        const val SIZE = 16
        const val FLAG_USER_ACTIVE = 0x10

        fun decode(b: ByteArray): PdaStatus {
            if (b.size < SIZE) throw ProtocolException("short PDA status (${b.size} B)")
            return PdaStatus(
                proto = Le.u8(b, 0),
                flags = Le.u8(b, 1),
                batteryPercent = b[2].toInt(), // i8, -1 = unknown
                outboxWaiting = Le.u16(b, 4),
                feedVersion = Le.u32(b, 8),
                bootId = Le.u32(b, 12),
            )
        }
    }
}
