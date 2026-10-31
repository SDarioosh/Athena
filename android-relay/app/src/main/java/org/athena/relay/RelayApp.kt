package org.athena.relay

import android.app.Application
import org.athena.relay.store.CrashLog

/** Installs [CrashLog] so a crash can be shown (and copied) the next time the app opens. */
class RelayApp : Application() {
    override fun onCreate() {
        super.onCreate()
        CrashLog.install(this)
    }
}
