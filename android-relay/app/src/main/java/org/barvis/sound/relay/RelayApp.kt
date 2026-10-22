package org.barvis.sound.relay

import android.app.Application
import org.barvis.sound.relay.store.CrashLog

/** Installs [CrashLog] so a crash can be shown (and copied) the next time the app opens. */
class RelayApp : Application() {
    override fun onCreate() {
        super.onCreate()
        CrashLog.install(this)
    }
}
