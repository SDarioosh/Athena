package org.barvis.sound.relay.ble

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.os.ParcelUuid
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeoutOrNull
import org.barvis.sound.relay.protocol.WatchGatt
import org.barvis.sound.relay.relay.Clock
import org.barvis.sound.relay.relay.Level
import org.barvis.sound.relay.relay.RelayLog
import org.barvis.sound.relay.relay.ScanThrottle

/**
 * Low-power filtered scan for the watch's service UUID. Each call runs one scan for at most
 * [windowMs] (the caller loops, which restarts the scan on a timer) and respects Android's
 * limit of 5 scan starts per 30 s.
 */
@SuppressLint("MissingPermission")
class Scanner(
    private val adapter: BluetoothAdapter,
    private val throttle: ScanThrottle,
    private val log: RelayLog,
    private val clock: Clock = Clock.SYSTEM,
) {
    class ScanFailed(val code: Int) : Exception("BLE scan failed (code $code)")

    suspend fun scan(windowMs: Long): BluetoothDevice? {
        val wait = throttle.delayBeforeStartMs(clock.nowMs())
        if (wait > 0) delay(wait)
        val scanner = adapter.bluetoothLeScanner ?: return null // Bluetooth turned off
        val found = CompletableDeferred<BluetoothDevice>()
        val cb = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: ScanResult) {
                found.complete(result.device)
            }

            override fun onBatchScanResults(results: MutableList<ScanResult>) {
                results.firstOrNull()?.let { found.complete(it.device) }
            }

            override fun onScanFailed(errorCode: Int) {
                found.completeExceptionally(ScanFailed(errorCode))
            }
        }
        val filters = listOf(ScanFilter.Builder().setServiceUuid(ParcelUuid(WatchGatt.SERVICE)).build())
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_POWER)
            .setCallbackType(ScanSettings.CALLBACK_TYPE_ALL_MATCHES)
            .build()
        throttle.recordStart(clock.nowMs())
        scanner.startScan(filters, settings, cb)
        try {
            return withTimeoutOrNull(windowMs) { found.await() }
        } finally {
            try {
                scanner.stopScan(cb)
            } catch (e: IllegalStateException) {
                log.log(Level.DEBUG, "stopScan: Bluetooth is off")
            }
        }
    }
}
