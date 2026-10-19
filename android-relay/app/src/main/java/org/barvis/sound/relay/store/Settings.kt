package org.barvis.sound.relay.store

import android.content.Context
import android.content.SharedPreferences
import org.barvis.sound.relay.relay.AnchorStore

/** App settings in private SharedPreferences (never backed up: see the manifest). */
class Settings(context: Context) {
    private val prefs: SharedPreferences =
        context.applicationContext.getSharedPreferences("relay", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = prefs.getString(KEY_URL, "") ?: ""
        set(v) = prefs.edit().putString(KEY_URL, v).apply()

    val hasToken: Boolean get() = prefs.contains(KEY_TOKEN)

    /** The decrypted token, or "" if none is stored. Never log this. */
    fun token(): String = prefs.getString(KEY_TOKEN, null)?.let { TokenCipher.decrypt(it) } ?: ""

    fun setToken(token: String) {
        prefs.edit().putString(KEY_TOKEN, TokenCipher.encrypt(token)).apply()
    }

    var relayEnabled: Boolean
        get() = prefs.getBoolean(KEY_ENABLED, false)
        set(v) = prefs.edit().putBoolean(KEY_ENABLED, v).apply()

    var startOnBoot: Boolean
        get() = prefs.getBoolean(KEY_BOOT, false)
        set(v) = prefs.edit().putBoolean(KEY_BOOT, v).apply()

    /** Set after a 401/403; cleared when the settings are saved again. */
    var tokenRejected: Boolean
        get() = prefs.getBoolean(KEY_REJECTED, false)
        set(v) = prefs.edit().putBoolean(KEY_REJECTED, v).apply()

    var totalUploaded: Long
        get() = prefs.getLong(KEY_TOTAL, 0)
        set(v) = prefs.edit().putLong(KEY_TOTAL, v).apply()

    var lastSyncMs: Long
        get() = prefs.getLong(KEY_LAST_SYNC, 0)
        set(v) = prefs.edit().putLong(KEY_LAST_SYNC, v).apply()

    /** TIME_UNSET anchors (§2.3), kept so a session keeps one anchor across reconnects. */
    val anchors: AnchorStore = object : AnchorStore {
        private val ap = context.applicationContext.getSharedPreferences("anchors", Context.MODE_PRIVATE)

        @Synchronized
        override fun anchorMs(sessionId: String, nowMs: Long): Long {
            val existing = ap.getLong(sessionId, -1)
            if (existing >= 0) return existing
            val edit = ap.edit()
            // Drop anchors older than 30 days.
            for ((k, v) in ap.all) if (v is Long && nowMs - v > 30L * 86_400_000) edit.remove(k)
            edit.putLong(sessionId, nowMs).apply()
            return nowMs
        }
    }

    companion object {
        private const val KEY_URL = "server_url"
        private const val KEY_TOKEN = "token_enc"
        private const val KEY_ENABLED = "relay_enabled"
        private const val KEY_BOOT = "start_on_boot"
        private const val KEY_REJECTED = "token_rejected"
        private const val KEY_TOTAL = "total_uploaded"
        private const val KEY_LAST_SYNC = "last_sync_ms"
    }
}
