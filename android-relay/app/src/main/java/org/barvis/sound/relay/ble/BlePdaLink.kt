package org.barvis.sound.relay.ble

import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.withTimeoutOrNull
import org.barvis.sound.relay.protocol.PdaGatt
import org.barvis.sound.relay.relay.PdaLink

/**
 * [PdaLink] over a connected, subscribed [GattClient] made with [PdaGatt.PROFILE]. `status`
 * notifications are kept (latest only) for [awaitStatus]; [onStatus] also sees each one.
 */
class BlePdaLink(private val client: GattClient, onStatus: (ByteArray) -> Unit = {}) : PdaLink {
    private val statuses = Channel<ByteArray>(Channel.CONFLATED)

    init {
        client.onStatus = { bytes ->
            onStatus(bytes)
            statuses.trySend(bytes)
        }
    }

    override val mtu: Int get() = client.mtu
    override suspend fun readInfo(): ByteArray = client.read(PdaGatt.INFO)
    override suspend fun readStatus(): ByteArray = client.read(PdaGatt.STATUS)
    override suspend fun write(command: ByteArray) = client.write(PdaGatt.CTRL, command)
    override suspend fun receive(timeoutMs: Long): ByteArray = client.receive(timeoutMs)
    override suspend fun awaitStatus(timeoutMs: Long): ByteArray? =
        withTimeoutOrNull(timeoutMs) { statuses.receive() }
}
