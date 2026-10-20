package org.barvis.sound.relay.relay

import org.barvis.sound.relay.net.Json
import org.barvis.sound.relay.net.bool
import org.barvis.sound.relay.net.long
import org.barvis.sound.relay.net.objects
import org.barvis.sound.relay.net.str
import org.barvis.sound.relay.net.strings
import java.security.MessageDigest
import java.time.DateTimeException
import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit

/** Server times -> what the T5 shows: ISO-8601 with the feed zone's offset, or a plain date. */
object PdaTime {
    private val ISO_OFFSET: DateTimeFormatter = DateTimeFormatter.ISO_OFFSET_DATE_TIME

    /**
     * `2026-10-20` stays a date. A time with `Z` or an offset is converted to [zone]. A time
     * without one (legacy Odysseus rows) is taken as local time in [zone]. Null if unparseable.
     */
    fun inZone(s: String?, zone: ZoneId): String? {
        if (s.isNullOrBlank()) return null
        if (s.length == 10) return try {
            LocalDate.parse(s).toString()
        } catch (e: DateTimeException) {
            null
        }
        val t = try {
            OffsetDateTime.parse(s).atZoneSameInstant(zone)
        } catch (e: DateTimeException) {
            try {
                LocalDateTime.parse(s).atZone(zone)
            } catch (e2: DateTimeException) {
                return null
            }
        }
        return ISO_OFFSET.format(t.truncatedTo(ChronoUnit.SECONDS))
    }

    fun iso(epochMs: Long, zone: ZoneId): String =
        ISO_OFFSET.format(Instant.ofEpochMilli(epochMs).atZone(zone).truncatedTo(ChronoUnit.SECONDS))

    fun offsetMinutes(epochMs: Long, zone: ZoneId): Int =
        zone.rules.getOffset(Instant.ofEpochMilli(epochMs)).totalSeconds / 60
}

/** The feed's content (§4.1) without `version` and `generated`, plus notes for the status line. */
class FeedContent(val sections: Map<String, Any?>, val calendarNote: String?) {
    /** Content hash: unchanged content means no `PUT` (§3.4 step 5). */
    val hash: String by lazy {
        MessageDigest.getInstance("SHA-256").digest(Json.write(sections).toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it.toInt() and 0xFF) }
    }

    /** The bytes to `PUT`: `v`, `version`, `generated`, then the sections. */
    fun render(version: Long, generatedMs: Long): ByteArray {
        val m = LinkedHashMap<String, Any?>()
        m["v"] = 1
        m["version"] = version
        m["generated"] = Instant.ofEpochMilli(generatedMs).truncatedTo(ChronoUnit.SECONDS).toString()
        m.putAll(sections)
        return Json.write(m).toByteArray(Charsets.UTF_8)
    }
}

sealed class FeedBuild {
    class Ok(val content: FeedContent) : FeedBuild()

    /** An odysseus-sound section failed: leave the T5's feed as it is. */
    class Fail(val error: ServerResult, val what: String) : FeedBuild()
}

/**
 * Builds the feed (§4.1) from the odysseus-sound API and the Odysseus calendar. Pure Kotlin.
 * The calendar is optional: if Odysseus can't be reached the last good calendar section is
 * reused, and if it isn't set up the section is left out.
 */
class FeedBuilder(
    private val api: ConvoApi,
    private val calendar: CalendarApi,
    private val memory: PdaMemory,
    private val clock: Clock,
    private val zone: ZoneId,
    private val templates: List<String>,
    private val maxBytes: Int = MAX_FEED_BYTES,
    private val log: RelayLog = RelayLog.NONE,
) {
    private class Conv(
        val id: String, val title: String, val at: String, val people: List<String>,
        val summary: String, val points: List<String>,
    )

    private class Cut(val summary: Int, val points: Int, val pointLen: Int)

    private class Stop(val fail: FeedBuild.Fail) : Exception()

    private fun <T> need(r: ApiResult<T>, what: String): T = when (r) {
        is ApiResult.Ok -> r.value
        is ApiResult.Fail -> throw Stop(FeedBuild.Fail(r.error, what))
        is ApiResult.Unsupported -> throw Stop(FeedBuild.Fail(ServerResult.Transient(r.reason), what))
    }

    suspend fun build(): FeedBuild = try {
        FeedBuild.Ok(buildContent())
    } catch (e: Stop) {
        e.fail
    }

    private suspend fun buildContent(): FeedContent {
        val now = clock.nowMs()
        val today = Instant.ofEpochMilli(now).atZone(zone).toLocalDate()

        val briefing = briefing(today)
        val convs = need(api.conversations(now / 1000 - CONVERSATION_WINDOW_S, MAX_CONVERSATIONS), "conversations")
            .map { c ->
                Conv(
                    id = c.str("id") ?: "",
                    title = c.str("title")?.takeIf { it.isNotBlank() } ?: "Untitled conversation",
                    at = PdaTime.inZone(c.str("started_at"), zone) ?: "",
                    people = c.strings("people").take(8),
                    summary = c.str("summary") ?: "",
                    points = c.strings("key_points"),
                )
            }.filter { it.id.isNotEmpty() }.toMutableList()
        val actions = need(api.openActionItems(), "action items").mapNotNull { a ->
            val id = a.long("id") ?: return@mapNotNull null
            linkedMapOf<String, Any?>(
                "id" to id,
                "text" to (a.str("text") ?: "").take(300),
                "owner" to (a.str("owner") ?: ""),
                "due" to (a.str("due") ?: ""),
                "conv" to (a.str("conversation_id") ?: ""),
            )
        }.take(MAX_ACTIONS).toMutableList()

        var calendarNote: String? = null
        val start = today.atStartOfDay(zone)
        val events: List<Map<String, Any?>>? = when (val r = calendar.listEvents(
            DateTimeFormatter.ISO_OFFSET_DATE_TIME.format(start),
            DateTimeFormatter.ISO_OFFSET_DATE_TIME.format(start.plusDays(CALENDAR_DAYS)))) {
            is ApiResult.Ok -> r.value.mapNotNull { calEvent(it) }.sortedBy { it.start }.also { memory.calendarFetched(it) }
                .map { calMap(it) }
            is ApiResult.Unsupported -> {
                calendarNote = r.reason
                null
            }
            is ApiResult.Fail -> {
                calendarNote = "Odysseus calendar: " + describe(r.error) + "; showing the last calendar"
                log.log(Level.WARN, calendarNote)
                memory.lastCalendar()?.map { calMap(it) }
            }
        }
        val calendarList = events?.take(MAX_EVENTS)?.toMutableList()
        val templateList = templates.map { it.trim().take(40) }.filter { it.isNotEmpty() }.distinct().take(12)

        // Cut to fit (§4.1): summaries first, then the oldest conversations, then the rest.
        val cuts = listOf(Cut(600, 5, 200), Cut(300, 3, 160), Cut(120, 2, 100), Cut(0, 0, 0))
        var level = 0
        while (true) {
            val sections = sections(briefing, convs, cuts[level], actions, calendarList, templateList)
            val content = FeedContent(sections, calendarNote)
            val size = content.render(0xFFFFFFFFL, now).size
            if (size <= maxBytes) {
                if (level > 0) log.log(Level.DEBUG, "feed cut to fit: $size B, level $level, ${convs.size} conversations")
                return content
            }
            when {
                level < cuts.size - 1 -> level++
                convs.isNotEmpty() -> convs.removeAt(convs.size - 1)
                actions.size > 10 -> actions.removeAt(actions.size - 1)
                calendarList != null && calendarList.size > 10 -> calendarList.removeAt(calendarList.size - 1)
                actions.isNotEmpty() -> actions.removeAt(actions.size - 1)
                calendarList != null && calendarList.isNotEmpty() -> calendarList.removeAt(calendarList.size - 1)
                else -> return content // the briefing alone can't exceed it in practice
            }
        }
    }

    private suspend fun briefing(today: LocalDate): Map<String, Any?>? {
        for (date in listOf(today, today.minusDays(1))) {
            when (val r = api.digest(date.toString())) {
                is ApiResult.Ok -> {
                    val d = r.value
                    val text = d.str("summary")?.takeIf { it.isNotBlank() } ?: d.str("headline") ?: ""
                    return linkedMapOf(
                        "date" to (d.str("date") ?: date.toString()),
                        "text" to text.take(1500),
                        "highlights" to d.strings("highlights").take(6).map { it.take(200) },
                    )
                }
                is ApiResult.Fail -> {
                    val e = r.error
                    if (e is ServerResult.Rejected && e.code == 404) continue
                    throw Stop(FeedBuild.Fail(e, "digest"))
                }
                is ApiResult.Unsupported -> continue
            }
        }
        return null
    }

    private fun sections(
        briefing: Map<String, Any?>?, convs: List<Conv>, cut: Cut, actions: List<Map<String, Any?>>,
        calendar: List<Map<String, Any?>>?, templates: List<String>,
    ): Map<String, Any?> {
        val m = LinkedHashMap<String, Any?>()
        m["tz"] = zone.id
        if (briefing != null) m["briefing"] = briefing
        m["conversations"] = convs.map { c ->
            linkedMapOf<String, Any?>(
                "id" to c.id, "title" to c.title.take(120), "at" to c.at, "people" to c.people,
                "summary" to cutText(c.summary, cut.summary),
                "points" to c.points.take(cut.points).map { cutText(it, cut.pointLen) },
            )
        }
        m["actions"] = actions
        if (calendar != null) m["calendar"] = calendar
        m["templates"] = templates
        return m
    }

    private fun calEvent(e: CalEvent): CalEvent? {
        val start = PdaTime.inZone(e.start, zone) ?: return null
        val end = PdaTime.inZone(e.end, zone) ?: start
        return e.copy(start = start, end = end, title = e.title.take(120), where = e.where.take(120))
    }

    private fun calMap(e: CalEvent): Map<String, Any?> = linkedMapOf(
        "id" to e.id, "title" to e.title, "start" to e.start, "end" to e.end, "where" to e.where, "all_day" to e.allDay,
    )

    companion object {
        const val MAX_FEED_BYTES = 32 * 1024
        const val CONVERSATION_WINDOW_S = 48 * 3600L
        const val MAX_CONVERSATIONS = 20
        const val MAX_ACTIONS = 50
        const val MAX_EVENTS = 60
        const val CALENDAR_DAYS = 8L // today and the next 7 days

        fun cutText(s: String, max: Int): String = when {
            max <= 0 -> ""
            s.length <= max -> s
            else -> s.take(max - 1).trimEnd() + "…"
        }

        fun describe(r: ServerResult): String = when (r) {
            is ServerResult.Ok -> "ok"
            is ServerResult.Transient -> r.detail
            is ServerResult.AuthRejected -> "token rejected (HTTP ${r.code})"
            is ServerResult.Rejected -> "HTTP ${r.code} ${r.detail}".trim()
        }

        /** Odysseus event JSON (`_event_to_dict`) -> [CalEvent], times as the hub sent them. */
        fun parseEvent(o: Map<String, Any?>): CalEvent? {
            val id = o.str("uid") ?: return null
            return CalEvent(
                id = id,
                title = o.str("summary") ?: "",
                start = o.str("dtstart") ?: return null,
                end = o.str("dtend") ?: "",
                where = o.str("location") ?: "",
                allDay = o.bool("all_day") ?: false,
            )
        }

        fun parseEvents(body: Map<String, Any?>): List<CalEvent> = body.objects("events").mapNotNull { parseEvent(it) }
    }
}
