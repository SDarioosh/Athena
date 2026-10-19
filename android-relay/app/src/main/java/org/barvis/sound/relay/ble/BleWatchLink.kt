package org.barvis.sound.relay.ble

import org.barvis.sound.relay.protocol.WatchGatt
import org.barvis.sound.relay.relay.WatchLink

/** [WatchLink] over a connected, subscribed [GattClient]. */
class BleWatchLink(private val client: GattClient) : WatchLink {
    override suspend fun readInfo(): ByteArray = client.read(WatchGatt.INFO)
    override suspend fun readStatus(): ByteArray = client.read(WatchGatt.STATUS)
    override suspend fun write(command: ByteArray) = client.write(WatchGatt.CTRL, command)
    override suspend fun receive(timeoutMs: Long): ByteArray = client.receive(timeoutMs)
}
