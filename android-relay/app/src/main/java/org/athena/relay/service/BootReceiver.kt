package org.athena.relay.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import org.athena.relay.store.Settings

/** Restarts the relay after a reboot, only if the owner turned that on and the relay was running. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        val s = Settings(context)
        if (s.startOnBoot && s.relayEnabled && Permissions.hasAll(context)) {
            RelayService.start(context)
        }
    }
}
