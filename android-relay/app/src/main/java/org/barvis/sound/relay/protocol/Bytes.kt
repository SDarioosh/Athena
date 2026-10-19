package org.barvis.sound.relay.protocol

/** Little-endian helpers for the watch protocol (docs/twatch-ble-protocol.md §3: all LE). */
internal object Le {
    fun u8(b: ByteArray, off: Int): Int = b[off].toInt() and 0xFF

    fun u16(b: ByteArray, off: Int): Int = u8(b, off) or (u8(b, off + 1) shl 8)

    fun u32(b: ByteArray, off: Int): Long =
        (u16(b, off).toLong()) or (u16(b, off + 2).toLong() shl 16)

    fun u64(b: ByteArray, off: Int): Long = u32(b, off) or (u32(b, off + 4) shl 32)

    fun putU8(b: ByteArray, off: Int, v: Int) {
        b[off] = v.toByte()
    }

    fun putU16(b: ByteArray, off: Int, v: Int) {
        b[off] = v.toByte()
        b[off + 1] = (v ushr 8).toByte()
    }

    fun putU32(b: ByteArray, off: Int, v: Long) {
        for (i in 0 until 4) b[off + i] = (v ushr (8 * i)).toByte()
    }

    fun putU64(b: ByteArray, off: Int, v: Long) {
        for (i in 0 until 8) b[off + i] = (v ushr (8 * i)).toByte()
    }
}

fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it.toInt() and 0xFF) }

fun hexToBytes(hex: String): ByteArray {
    val clean = hex.filterNot { it.isWhitespace() }
    require(clean.length % 2 == 0) { "odd hex length" }
    return ByteArray(clean.length / 2) { i -> clean.substring(2 * i, 2 * i + 2).toInt(16).toByte() }
}

/** CRC-32 (IEEE 802.3, same as zlib's crc32), as an unsigned value in a Long. */
fun crc32(data: ByteArray, length: Int = data.size): Long {
    val c = java.util.zip.CRC32()
    c.update(data, 0, length)
    return c.value
}

/** Thrown when a frame from the watch can't be decoded. */
class ProtocolException(message: String) : Exception(message)
