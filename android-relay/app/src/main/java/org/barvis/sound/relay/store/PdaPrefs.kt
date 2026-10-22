package org.barvis.sound.relay.store

import android.content.Context
import org.barvis.sound.relay.relay.PdaMemory

/**
 * [PdaMemory] in private SharedPreferences (not backed up, like the rest). Holds the last feed
 * hash and version per T5, the `cal_add` dedupe table, kept for [KEEP_MS], and the hub's last
 * good rule-weights table (one for all T5s: it comes from the hub).
 */
class PdaPrefs(context: Context) : PdaMemory {
    private val p = context.getSharedPreferences("pda", Context.MODE_PRIVATE)

    @Synchronized override fun lastFeedHash(device: String): String? = p.getString("hash:$device", null)

    @Synchronized override fun lastFeedVersion(device: String): Long = p.getLong("version:$device", 0)

    @Synchronized override fun feedPushed(device: String, hash: String, version: Long) {
        p.edit().putString("hash:$device", hash).putLong("version:$device", version).apply()
    }

    @Synchronized override fun createdEvent(actionKey: String): String? =
        p.getString("cal:$actionKey", null)?.substringBefore('|')

    @Synchronized override fun eventCreated(actionKey: String, uid: String, nowMs: Long) {
        val edit = p.edit()
        for ((k, v) in p.all) {
            if (!k.startsWith("cal:") || v !is String) continue
            val at = v.substringAfterLast('|').toLongOrNull() ?: 0
            if (nowMs - at > KEEP_MS) edit.remove(k)
        }
        edit.putString("cal:$actionKey", "$uid|$nowMs").apply()
    }

    @Synchronized override fun lastWeights(): String? = p.getString("weights", null)

    @Synchronized override fun weightsFetched(json: String) {
        p.edit().putString("weights", json).apply()
    }

    private companion object {
        const val KEEP_MS = 30L * 86_400_000
    }
}
