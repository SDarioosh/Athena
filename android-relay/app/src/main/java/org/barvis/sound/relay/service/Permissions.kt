package org.barvis.sound.relay.service

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build

object Permissions {
    /** Runtime permissions the relay can't work without. */
    fun required(): Array<String> = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
        arrayOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
    } else {
        // Android 8-11 only return BLE scan results with location permission (and location on).
        arrayOf(Manifest.permission.ACCESS_FINE_LOCATION)
    }

    /** Wanted but optional: the status notification (Android 13+). */
    fun optional(): Array<String> = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
        arrayOf(Manifest.permission.POST_NOTIFICATIONS)
    } else emptyArray()

    fun missing(context: Context): List<String> = required().filter {
        context.checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED
    }

    fun hasAll(context: Context) = missing(context).isEmpty()
}
