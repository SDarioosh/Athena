package org.barvis.sound.relay.ble

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothStatusCodes
import android.content.Context
import android.os.Build
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withTimeoutOrNull
import org.barvis.sound.relay.protocol.GattProfile
import org.barvis.sound.relay.protocol.WatchGatt
import org.barvis.sound.relay.relay.Level
import org.barvis.sound.relay.relay.LinkException
import org.barvis.sound.relay.relay.RelayLog
import java.util.UUID

/** A GATT operation finished with a non-success status. */
class GattStatusException(what: String, val status: Int) : LinkException("$what failed (GATT status $status)")

/**
 * Coroutine wrapper around [BluetoothGatt]. Android allows one outstanding GATT operation per
 * connection, so every operation goes through [op], which holds a mutex until the matching
 * callback arrives (or times out). Notifications of `data` go to an ordered channel;
 * `status` notifications go to [onStatus].
 *
 * Permissions (BLUETOOTH_CONNECT) are checked by the service before it starts.
 */
@SuppressLint("MissingPermission")
class GattClient(
    private val context: Context,
    val device: BluetoothDevice,
    private val log: RelayLog,
    /** Which service this link talks to: the watch (default) or the PDA. */
    private val profile: GattProfile = WatchGatt.PROFILE,
) {
    private enum class Kind { MTU, DISCOVER, PHY, READ, WRITE, DESCRIPTOR }

    private class Pending(val kind: Kind, val result: CompletableDeferred<Any?>)

    private val mutex = Mutex()
    @Volatile private var pending: Pending? = null
    @Volatile private var gatt: BluetoothGatt? = null
    private val connected = CompletableDeferred<Unit>()
    private val data = Channel<ByteArray>(Channel.UNLIMITED)

    @Volatile var onStatus: ((ByteArray) -> Unit)? = null
    @Volatile var mtu = 23
        private set

    private fun finish(kind: Kind, status: Int, value: Any? = null) {
        val p = pending ?: return
        if (p.kind != kind) return // late callback of an op that already timed out
        if (status == BluetoothGatt.GATT_SUCCESS) p.result.complete(value)
        else p.result.completeExceptionally(GattStatusException(kind.name.lowercase(), status))
    }

    private val callback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            if (newState == BluetoothProfile.STATE_CONNECTED && status == BluetoothGatt.GATT_SUCCESS) {
                connected.complete(Unit)
            } else {
                val e = LinkException("disconnected (status $status)")
                connected.completeExceptionally(e)
                pending?.result?.completeExceptionally(e)
                data.close(e)
            }
        }

        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            if (status == BluetoothGatt.GATT_SUCCESS) this@GattClient.mtu = mtu
            finish(Kind.MTU, status, mtu)
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) = finish(Kind.DISCOVER, status)

        override fun onPhyUpdate(g: BluetoothGatt, txPhy: Int, rxPhy: Int, status: Int) =
            finish(Kind.PHY, status, txPhy)

        override fun onCharacteristicRead(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray, status: Int) =
            finish(Kind.READ, status, value)

        @Deprecated("Android 12 and older")
        @Suppress("DEPRECATION")
        override fun onCharacteristicRead(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) =
            finish(Kind.READ, status, c.value?.copyOf() ?: ByteArray(0))

        override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) =
            finish(Kind.WRITE, status)

        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) =
            finish(Kind.DESCRIPTOR, status)

        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray) =
            notified(c.uuid, value.copyOf())

        @Deprecated("Android 12 and older")
        @Suppress("DEPRECATION")
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) =
            notified(c.uuid, c.value?.copyOf() ?: ByteArray(0))
    }

    private fun notified(uuid: UUID, value: ByteArray) {
        when (uuid) {
            profile.data -> data.trySend(value)
            profile.status -> onStatus?.invoke(value)
        }
    }

    suspend fun connect(timeoutMs: Long = 15_000) {
        gatt = device.connectGatt(context, false, callback, BluetoothDevice.TRANSPORT_LE)
            ?: throw LinkException("connectGatt returned null")
        withTimeoutOrNull(timeoutMs) { connected.await() } ?: throw LinkException("connect timed out")
    }

    private suspend fun op(kind: Kind, timeoutMs: Long, start: (BluetoothGatt) -> Boolean): Any? = mutex.withLock {
        val g = gatt ?: throw LinkException("not connected")
        val p = Pending(kind, CompletableDeferred())
        pending = p
        try {
            if (!start(g)) throw LinkException("${kind.name.lowercase()} could not start")
            withTimeout(timeoutMs) { p.result.await() }
        } catch (e: TimeoutCancellationException) {
            throw LinkException("${kind.name.lowercase()} timed out")
        } finally {
            pending = null
        }
    }

    suspend fun requestMtu(mtu: Int): Int = op(Kind.MTU, 5_000) { it.requestMtu(mtu) } as Int

    /** Discovers the watch's services afresh. Android otherwise reuses the database it cached
     *  for a bonded device ("Using cached database without robust caching"), and the watch has
     *  no database hash to tell it that a firmware update moved the handles. */
    suspend fun discoverServices() {
        gatt?.let { refreshCache(it) }
        op(Kind.DISCOVER, 15_000) { it.discoverServices() }
    }

    /** BluetoothGatt.refresh() is hidden API (greylisted, still callable); best effort. */
    private fun refreshCache(g: BluetoothGatt) {
        try {
            val ok = g.javaClass.getMethod("refresh").invoke(g) as? Boolean
            android.util.Log.i("BarvisRelay", "gatt cache refresh: $ok")
        } catch (e: ReflectiveOperationException) {
            android.util.Log.w("BarvisRelay", "gatt cache refresh unavailable: $e")
        }
    }

    /** 2M PHY if both sides support it (§7.2). Best effort. */
    suspend fun preferPhy2M() {
        val adapter = (context.getSystemService(Context.BLUETOOTH_SERVICE) as android.bluetooth.BluetoothManager).adapter
        if (adapter?.isLe2MPhySupported != true) return
        try {
            op(Kind.PHY, 3_000) {
                it.setPreferredPhy(BluetoothDevice.PHY_LE_2M_MASK, BluetoothDevice.PHY_LE_2M_MASK,
                    BluetoothDevice.PHY_OPTION_NO_PREFERRED)
                true
            }
        } catch (e: LinkException) {
            log.log(Level.DEBUG, "2M PHY not applied: ${e.message}")
        }
    }

    fun highPriority(on: Boolean) {
        gatt?.requestConnectionPriority(
            if (on) BluetoothGatt.CONNECTION_PRIORITY_HIGH else BluetoothGatt.CONNECTION_PRIORITY_BALANCED
        )
    }

    private fun characteristic(uuid: UUID): BluetoothGattCharacteristic =
        gatt?.getService(profile.service)?.getCharacteristic(uuid)
            ?: throw LinkException("device has no characteristic $uuid")

    fun hasWatchService(): Boolean = gatt?.getService(WatchGatt.SERVICE) != null

    fun hasService(): Boolean = gatt?.getService(profile.service) != null

    suspend fun read(uuid: UUID): ByteArray {
        val c = characteristic(uuid)
        return op(Kind.READ, 5_000) { it.readCharacteristic(c) } as ByteArray
    }

    @Suppress("DEPRECATION")
    suspend fun write(uuid: UUID, value: ByteArray) {
        val c = characteristic(uuid)
        op(Kind.WRITE, 5_000) { g ->
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                g.writeCharacteristic(c, value, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT) ==
                    BluetoothStatusCodes.SUCCESS
            } else {
                c.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                c.value = value
                g.writeCharacteristic(c)
            }
        }
    }

    @Suppress("DEPRECATION")
    suspend fun enableNotifications(uuid: UUID) {
        val c = characteristic(uuid)
        val g = gatt ?: throw LinkException("not connected")
        if (!g.setCharacteristicNotification(c, true)) throw LinkException("setCharacteristicNotification failed")
        val d = c.getDescriptor(WatchGatt.CCCD) ?: throw LinkException("no CCCD on $uuid")
        val v = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
        // Encrypted characteristic: on a new phone this is where pairing happens (§3.1).
        op(Kind.DESCRIPTOR, 30_000) { gg ->
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                gg.writeDescriptor(d, v) == BluetoothStatusCodes.SUCCESS
            } else {
                d.value = v
                gg.writeDescriptor(d)
            }
        }
    }

    /** Next `data` notification. */
    suspend fun receive(timeoutMs: Long): ByteArray {
        val r = withTimeoutOrNull(timeoutMs) { data.receiveCatching() }
            ?: throw LinkException("no answer from the watch for ${timeoutMs / 1000} s")
        r.exceptionOrNull()?.let { throw it as? LinkException ?: LinkException("link closed", it) }
        return r.getOrNull() ?: throw LinkException("link closed")
    }

    fun close() {
        val g = gatt ?: return
        gatt = null
        try {
            g.disconnect()
        } finally {
            g.close()
        }
        val e = LinkException("closed")
        pending?.result?.completeExceptionally(e)
        data.close(e)
    }
}
