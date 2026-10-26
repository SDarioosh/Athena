package org.barvis.sound.relay.store

import android.content.Context
import android.content.SharedPreferences
import org.barvis.sound.relay.net.Routes
import org.barvis.sound.relay.net.ServerUrl
import org.barvis.sound.relay.relay.AnchorStore
import org.barvis.sound.relay.relay.PdaMemory

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

    /**
     * Where the watch's audio and the T5's module reads go: [MODE_ODYSSEUS] (after pairing,
     * the Odysseus URL and token) or [MODE_DIRECT] (the server URL and ingest token, the
     * fallback and the behaviour before pairing existed).
     */
    var uploadMode: String
        get() = prefs.getString(KEY_MODE, MODE_DIRECT) ?: MODE_DIRECT
        set(v) = prefs.edit().putString(KEY_MODE, if (v == MODE_ODYSSEUS) MODE_ODYSSEUS else MODE_DIRECT).apply()

    val throughOdysseus: Boolean get() = uploadMode == MODE_ODYSSEUS

    /** Base URL, token and routes for the module, or null with what is missing. Never log the token. */
    class Target(val base: String, val token: () -> String, val routes: Routes)

    fun target(): Target? {
        val odysseus = throughOdysseus
        val url = ServerUrl.check(if (odysseus) odysseusUrl else serverUrl)
        if (url !is ServerUrl.Check.Ok) return null
        return if (odysseus) {
            if (!hasOdysseusToken) null else Target(url.base, ::odysseusToken, Routes.ODYSSEUS)
        } else {
            if (!hasToken) null else Target(url.base, ::token, Routes.DIRECT)
        }
    }

    /** Stores what pairing returned and switches to [MODE_ODYSSEUS]. */
    fun savePairing(base: String, token: String) {
        setOdysseusToken(token)
        prefs.edit()
            .putString(KEY_ODY_URL, base)
            .putString(KEY_MODE, MODE_ODYSSEUS)
            .putBoolean(KEY_REJECTED, false)
            .apply()
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

    // ---------------------------------------------------------- T5 PDA (docs/t5-pda-plan.md §6)

    var pdaEnabled: Boolean
        get() = prefs.getBoolean(KEY_PDA_ENABLED, false)
        set(v) = prefs.edit().putBoolean(KEY_PDA_ENABLED, v).apply()

    /** Odysseus base URL, e.g. http://server.tailnet.ts.net:7000. Empty = no calendar. */
    var odysseusUrl: String
        get() = prefs.getString(KEY_ODY_URL, "") ?: ""
        set(v) = prefs.edit().putString(KEY_ODY_URL, v).apply()

    val hasOdysseusToken: Boolean get() = prefs.contains(KEY_ODY_TOKEN)

    /** The decrypted Odysseus `ody_…` token (from pairing, or typed for the calendar), or "". Never log this. */
    fun odysseusToken(): String = prefs.getString(KEY_ODY_TOKEN, null)?.let { TokenCipher.decrypt(it) } ?: ""

    fun setOdysseusToken(token: String) {
        prefs.edit().putString(KEY_ODY_TOKEN, TokenCipher.encrypt(token)).apply()
    }

    /** The CalDAV calendar's id in Odysseus (`calendar_href`); empty = the hub's default. */
    var odysseusCalendarId: String
        get() = prefs.getString(KEY_ODY_CAL, "") ?: ""
        set(v) = prefs.edit().putString(KEY_ODY_CAL, v).apply()

    /** Event title templates for the T5's "+" picker (§5), one per line. */
    var eventTemplates: List<String>
        get() = (prefs.getString(KEY_TEMPLATES, null) ?: DEFAULT_TEMPLATES).lines().map { it.trim() }.filter { it.isNotEmpty() }
        set(v) = prefs.edit().putString(KEY_TEMPLATES, v.joinToString("\n")).apply()

    /**
     * Sync the T5 on its own (every [pdaSyncMinutes], and when it has actions waiting). Off =
     * the app doesn't look for the T5 at all (it's switched off or away), except for "Sync T5 now".
     */
    var pdaAutoSync: Boolean
        get() = prefs.getBoolean(KEY_PDA_AUTO, true)
        set(v) = prefs.edit().putBoolean(KEY_PDA_AUTO, v).apply()

    /** Minutes between automatic PDA syncs while it isn't in use (§3.4 step 6). */
    var pdaSyncMinutes: Int
        get() = prefs.getInt(KEY_PDA_INTERVAL, 15)
        set(v) = prefs.edit().putInt(KEY_PDA_INTERVAL, v.coerceIn(MIN_PDA_MINUTES, MAX_PDA_MINUTES)).apply()

    var lastPdaSyncMs: Long
        get() = prefs.getLong(KEY_PDA_LAST, 0)
        set(v) = prefs.edit().putLong(KEY_PDA_LAST, v).apply()

    /** Feed hashes, the last calendar and the cal_add dedupe table (no tokens). */
    val pdaMemory: PdaMemory = PdaPrefs(context.applicationContext)

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
        private const val KEY_PDA_ENABLED = "pda_enabled"
        private const val KEY_ODY_URL = "odysseus_url"
        private const val KEY_ODY_TOKEN = "odysseus_token_enc"
        private const val KEY_ODY_CAL = "odysseus_calendar_id"
        private const val KEY_TEMPLATES = "pda_templates"
        private const val KEY_PDA_INTERVAL = "pda_sync_minutes"
        private const val KEY_PDA_AUTO = "pda_auto_sync"
        private const val KEY_PDA_LAST = "pda_last_sync_ms"
        private const val KEY_MODE = "upload_mode"
        const val MODE_DIRECT = "direct"
        const val MODE_ODYSSEUS = "odysseus"
        const val DEFAULT_TEMPLATES = "Call\nMeeting\nReminder\nFollow-up"
        const val MIN_PDA_MINUTES = 5
        const val MAX_PDA_MINUTES = 240
    }
}
