package org.barvis.sound.relay.protocol

/** The `info` characteristic, §4.1: UTF-8 `key=value;…`, unknown keys ignored. */
data class WatchInfo(val values: Map<String, String>) {
    val proto: Int? get() = values["proto"]?.trim()?.toIntOrNull()
    val device: String? get() = values["device"]?.trim()?.takeIf { it.isNotEmpty() }
    val fw: String? get() = values["fw"]
    val board: String? get() = values["board"]

    companion object {
        fun parse(text: String): WatchInfo = WatchInfo(
            text.split(';').mapNotNull { part ->
                val i = part.indexOf('=')
                if (i <= 0) null else part.substring(0, i).trim() to part.substring(i + 1)
            }.toMap()
        )

        fun parse(bytes: ByteArray): WatchInfo = parse(bytes.toString(Charsets.UTF_8))
    }
}

/** The `status` characteristic, §4.2 (24 bytes; longer values are allowed, §9). */
data class WatchStatus(
    val proto: Int,
    val flags: Int,
    val batteryPercent: Int,
    val batteryMv: Int,
    val queuedChunks: Int,
    val queuedBytes: Long,
    val queuedMs: Long,
    val clockEpochS: Long,
    val bootId: Long,
) {
    val recording get() = flags and 0x01 != 0
    val muted get() = flags and 0x02 != 0
    val timeValid get() = flags and 0x04 != 0
    val charging get() = flags and 0x08 != 0
    val usbPower get() = flags and 0x10 != 0
    val sdOk get() = flags and 0x20 != 0
    val syncWindowOpen get() = flags and 0x40 != 0

    fun encode(): ByteArray = ByteArray(SIZE).also {
        Le.putU8(it, 0, proto); Le.putU8(it, 1, flags); Le.putU8(it, 2, batteryPercent)
        Le.putU16(it, 4, batteryMv); Le.putU16(it, 6, queuedChunks)
        Le.putU32(it, 8, queuedBytes); Le.putU32(it, 12, queuedMs)
        Le.putU32(it, 16, clockEpochS); Le.putU32(it, 20, bootId)
    }

    companion object {
        const val SIZE = 24

        fun decode(b: ByteArray): WatchStatus {
            if (b.size < SIZE) throw ProtocolException("short status (${b.size} B)")
            return WatchStatus(
                proto = Le.u8(b, 0),
                flags = Le.u8(b, 1),
                batteryPercent = b[2].toInt(), // i8, -1 = unknown
                batteryMv = Le.u16(b, 4),
                queuedChunks = Le.u16(b, 6),
                queuedBytes = Le.u32(b, 8),
                queuedMs = Le.u32(b, 12),
                clockEpochS = Le.u32(b, 16),
                bootId = Le.u32(b, 20),
            )
        }
    }
}
