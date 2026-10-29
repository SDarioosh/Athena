package org.athena.relay.ble

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Bonds with the watch (LE Secure Connections, passkey entry: Android asks the user for the
 * 6-digit code shown on the watch, §3.1). The watch accepts new bonds
 * only during its sync window (BOOT long press). Returns true when bonded.
 */
@SuppressLint("MissingPermission")
object Bonding {
    suspend fun ensureBonded(context: Context, device: BluetoothDevice, timeoutMs: Long = 60_000): Boolean {
        if (device.bondState == BluetoothDevice.BOND_BONDED) return true
        val result = CompletableDeferred<Boolean>()
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(c: Context, intent: Intent) {
                @Suppress("DEPRECATION")
                val d: BluetoothDevice? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE, BluetoothDevice::class.java)
                } else intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE)
                if (d?.address != device.address) return
                when (intent.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, BluetoothDevice.ERROR)) {
                    BluetoothDevice.BOND_BONDED -> result.complete(true)
                    BluetoothDevice.BOND_NONE -> result.complete(false)
                }
            }
        }
        val filter = IntentFilter(BluetoothDevice.ACTION_BOND_STATE_CHANGED)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            context.registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            context.registerReceiver(receiver, filter)
        }
        try {
            if (device.bondState == BluetoothDevice.BOND_BONDED) return true
            if (device.bondState != BluetoothDevice.BOND_BONDING && !device.createBond()) {
                return device.bondState == BluetoothDevice.BOND_BONDED
            }
            return withTimeoutOrNull(timeoutMs) { result.await() } ?: false
        } finally {
            context.unregisterReceiver(receiver)
        }
    }
}
