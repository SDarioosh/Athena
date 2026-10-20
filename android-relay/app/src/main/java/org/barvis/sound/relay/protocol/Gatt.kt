package org.barvis.sound.relay.protocol

import java.util.UUID

/** GATT layout, docs/twatch-ble-protocol.md §3. */
object WatchGatt {
    val SERVICE: UUID = UUID.fromString("f8060000-61c0-4fda-8b7b-26b08e916438")
    val INFO: UUID = UUID.fromString("f8060001-61c0-4fda-8b7b-26b08e916438")
    val STATUS: UUID = UUID.fromString("f8060002-61c0-4fda-8b7b-26b08e916438")
    val CTRL: UUID = UUID.fromString("f8060003-61c0-4fda-8b7b-26b08e916438")
    val DATA: UUID = UUID.fromString("f8060004-61c0-4fda-8b7b-26b08e916438")

    /** Client Characteristic Configuration descriptor (standard). */
    val CCCD: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

    const val PROTO_VERSION = 1
    const val REQUESTED_MTU = 517
    const val MIN_MTU = 64

    /** The watch caps LIST at 32 entries (§5.1). */
    const val LIST_MAX = 32

    /** ATT header (3) + DATA header (9). */
    const val DATA_OVERHEAD = 12

    /** The Core spec caps an attribute value at 512 bytes and Android drops longer
     *  notifications, so the watch never sends more: a DATA frame carries <= 503 bytes. */
    const val MAX_NOTIFICATION = 512

    /** DATA payload bytes per frame at this ATT MTU (§7.2). */
    fun dataPayloadMax(mtu: Int): Int = minOf(mtu - 3, MAX_NOTIFICATION) - (DATA_OVERHEAD - 3)
}
