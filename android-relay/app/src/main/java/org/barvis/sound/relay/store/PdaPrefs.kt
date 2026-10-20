package org.barvis.sound.relay.store

import android.content.Context
import org.barvis.sound.relay.net.Json
import org.barvis.sound.relay.net.bool
import org.barvis.sound.relay.net.objects
import org.barvis.sound.relay.net.str
import org.barvis.sound.relay.relay.CalEvent
import org.barvis.sound.relay.relay.PdaMemory

/**
 * [PdaMemory] in private SharedPreferences (not backed up, like the rest). Holds the last
 * calendar section (event titles, so it is as private as the feed itself) and the `cal_add`
 * dedupe table, kept for [KEEP_MS].
 */
class PdaPrefs(context: Context) : PdaMemory {
    private val p = context.getSharedPreferences("pda", Context.MODE_PRIVATE)

    @Synchronized override fun lastFeedHash(device: String): String? = p.getString("hash:$device", null)

    @Synchronized override fun lastFeedVersion(device: String): Long = p.getLong("version:$device", 0)

    @Synchronized override fun feedPushed(device: String, hash: String, version: Long) {
        p.edit().putString("hash:$device", hash).putLong("version:$device", version).apply()
    }

    @Synchronized override fun lastCalendar(): List<CalEvent>? {
        val text = p.getString(KEY_CALENDAR, null) ?: return null
        return try {
            Json.parseObject(text).objects("events").map {
                CalEvent(it.str("id") ?: "", it.str("title") ?: "", it.str("start") ?: "", it.str("end") ?: "",
                    it.str("where") ?: "", it.bool("all_day") ?: false)
            }
        } catch (e: Json.ParseException) {
            null
        }
    }

    @Synchronized override fun calendarFetched(events: List<CalEvent>) {
        val list = events.map {
            mapOf("id" to it.id, "title" to it.title, "start" to it.start, "end" to it.end, "where" to it.where,
                "all_day" to it.allDay)
        }
        p.edit().putString(KEY_CALENDAR, Json.write(mapOf("events" to list))).apply()
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

    private companion object {
        const val KEY_CALENDAR = "calendar"
        const val KEEP_MS = 30L * 86_400_000
    }
}
