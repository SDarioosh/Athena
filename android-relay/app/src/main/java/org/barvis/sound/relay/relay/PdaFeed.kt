package org.barvis.sound.relay.relay

import org.barvis.sound.relay.net.Json
import org.barvis.sound.relay.net.bool
import org.barvis.sound.relay.net.objects
import org.barvis.sound.relay.net.str
import java.security.MessageDigest
import java.time.DateTimeException
import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit

/** Times as the T5 gets them (plan §4.1, same as the PC relay): UTC with `Z`, or a plain date. */
object PdaTime {
    private val ISO_Z: DateTimeFormatter = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss'Z'")
        .withZone(ZoneOffset.UTC)

    /**
     * `2026-10-20` stays a date. A time with `Z` or an offset is converted to UTC. A time
     * without one is taken as local time in [naiveZone] (legacy Odysseus rows; odysseus-sound
     * always sends an offset). Null if unparseable.
     */
    fun utc(s: String?, naiveZone: ZoneId = ZoneOffset.UTC): String? {
        if (s.isNullOrBlank()) return null
        if (s.length == 10) return try {
            LocalDate.parse(s).toString()
        } catch (e: DateTimeException) {
            null
        }
        val instant = try {
            OffsetDateTime.parse(s).toInstant()
        } catch (e: DateTimeException) {
            try {
                LocalDateTime.parse(s).atZone(naiveZone).toInstant()
            } catch (e2: DateTimeException) {
                return null
            }
        }
        return ISO_Z.format(instant.truncatedTo(ChronoUnit.SECONDS))
    }

    fun utc(epochMs: Long): String = ISO_Z.format(Instant.ofEpochMilli(epochMs).truncatedTo(ChronoUnit.SECONDS))

    fun offsetMinutes(epochMs: Long, zone: ZoneId): Int =
        zone.rules.getOffset(Instant.ofEpochMilli(epochMs)).totalSeconds / 60
}

/** The feed's content (§4.1) without `version` and `generated`. */
class FeedContent(val sections: Map<String, Any?>) {
    /** SHA-256 of the content: unchanged content means no `PUT` (§4.1 push rule). */
    val hash: String by lazy {
        MessageDigest.getInstance("SHA-256").digest(Json.write(sections).toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it.toInt() and 0xFF) }
    }

    /** The bytes to `PUT`: `v`, `version`, `generated`, then the sections. */
    fun render(version: Long, generatedMs: Long): ByteArray {
        val m = LinkedHashMap<String, Any?>()
        m["v"] = 1
        m["version"] = version
        m["generated"] = PdaTime.utc(generatedMs)
        m.putAll(sections)
        return Json.write(m).toByteArray(Charsets.UTF_8)
    }
}

sealed class FeedBuild {
    class Ok(val content: FeedContent) : FeedBuild()

    /** A source failed: nothing is pushed and the T5 keeps its feed. */
    class Fail(val error: ServerResult, val what: String) : FeedBuild()
}

/**
 * Builds the feed (§4.1) from the odysseus-sound API and the Odysseus calendar, following the
 * PC relay's rules (plan §4.1 "How the PC relay builds it"). Pure Kotlin.
 *
 * A source that fails fails the build, except a digest 404 and a calendar that isn't set up
 * (or a hub without the calendar routes), which give no briefing and `"calendar": []`.
 */
class FeedBuilder(
    private val api: ConvoApi,
    private val calendar: CalendarApi,
    private val clock: Clock,
    private val zone: ZoneId,
    private val templates: List<String>,
    private val maxBytes: Int = MAX_FEED_BYTES,
    private val log: RelayLog = RelayLog.NONE,
    private val todos: TodosApi = NoTodos,
) {
    private class Stop(val fail: FeedBuild.Fail) : Exception()

    private fun <T> need(r: ApiResult<T>, what: String): T = when (r) {
        is ApiResult.Ok -> r.value
        is ApiResult.Fail -> throw Stop(FeedBuild.Fail(r.error, what))
        is ApiResult.Unsupported -> throw Stop(FeedBuild.Fail(ServerResult.Transient(r.reason), what))
    }

    /** Why the last build has an empty calendar, if it is because Odysseus isn't usable. */
    var calendarNote: String? = null
        private set

    /** Why the last build has no `todos` (plan §4.4), or null. */
    var todosNote: String? = null
        private set

    suspend fun build(): FeedBuild = try {
        FeedBuild.Ok(buildContent())
    } catch (e: Stop) {
        e.fail
    }

    private suspend fun buildContent(): FeedContent {
        val now = clock.nowMs()
        val today = Instant.ofEpochMilli(now).atZone(zone).toLocalDate()
        val m = LinkedHashMap<String, Any?>()
        m["tz"] = zone.id
        briefing(today)?.let { m["briefing"] = it }

        val convs = need(api.conversations(now / 1000 - CONVERSATION_WINDOW_S, MAX_CONVERSATIONS), "conversations")
            .filter { it.str("status") == "summarized" && !it.str("id").isNullOrEmpty() }
            .map { c ->
                linkedMapOf<String, Any?>(
                    "id" to c.str("id"),
                    "title" to cut(c.str("title"), TITLE_MAX),
                    "at" to PdaTime.utc(c.str("started_at")),
                    "people" to strings(c["people"], 8, 40),
                    "summary" to cut(c.str("summary"), SUMMARY_MAX),
                    "points" to strings(c["key_points"], POINTS_MAX, POINT_MAX),
                )
            }.sortedByDescending { it["at"] as String? ?: "" }.toMutableList()
        m["conversations"] = convs

        val actions = need(api.openActionItems(), "action items").mapNotNull { a ->
            val id = a["id"] as? Long ?: return@mapNotNull null
            linkedMapOf<String, Any?>(
                "id" to id,
                "text" to cut(a.str("text"), TEXT_MAX),
                "owner" to a.str("owner")?.takeIf { it.isNotBlank() }?.let { cut(it, 40) },
                "due" to a.str("due_date")?.takeIf { it.isNotEmpty() },
                "conv" to a.str("conversation_id"),
            )
        }.sortedWith(compareBy<Map<String, Any?>> { it["due"] == null }.thenBy { it["due"] as String? ?: "" })
            .toMutableList()
        m["actions"] = actions

        calendarNote = null
        val start = today.atStartOfDay(zone)
        val f = DateTimeFormatter.ISO_OFFSET_DATE_TIME
        val events = when (val r = calendar.listEvents(f.format(start), f.format(start.plusDays(CALENDAR_DAYS)))) {
            is ApiResult.Ok -> r.value.mapNotNull { calMap(it) }.sortedBy { it["start"] as String }
            is ApiResult.Unsupported -> {
                calendarNote = r.reason
                emptyList()
            }
            is ApiResult.Fail -> throw Stop(FeedBuild.Fail(r.error, "Odysseus calendar"))
        }.toMutableList()
        m["calendar"] = events

        todosNote = null
        val notes = when (val r = todos.notes()) {
            is ApiResult.Ok -> todoSection(r.value)
            is ApiResult.Unsupported -> {
                todosNote = r.reason
                null
            }
            is ApiResult.Fail -> throw Stop(FeedBuild.Fail(r.error, "Odysseus todos"))
        }
        if (notes != null) m["todos"] = notes
        // Not read by the PC relay or required by the firmware (plan §4.1: optional, left out
        // when there are none).
        val templateList = templates.map { it.trim().take(40) }.filter { it.isNotEmpty() }.distinct().take(12)
        if (templateList.isNotEmpty()) m["templates"] = templateList

        // Too big (§4.1): cut summaries, then drop the oldest conversations, then done
        // checklist items, then the last action items, then the last unpinned checklists,
        // then the last calendar entries.
        fun size() = FeedContent(m).render(0xFFFFFFFFL, now).size
        if (size() > maxBytes) {
            for (c in convs) c["summary"] = cut(c["summary"] as String?, SUMMARY_CUT)
            var steps = 0
            while (size() > maxBytes) {
                steps++
                when {
                    convs.isNotEmpty() -> convs.removeAt(convs.size - 1)
                    notes != null && dropLastDoneItem(notes) -> Unit
                    actions.isNotEmpty() -> actions.removeAt(actions.size - 1)
                    notes != null && notes.any { it["pinned"] != true } ->
                        notes.removeAt(notes.indexOfLast { it["pinned"] != true })
                    events.isNotEmpty() -> events.removeAt(events.size - 1)
                    else -> break
                }
            }
            log.log(Level.INFO, "feed trimmed to ${size()} B (summaries cut, $steps entries dropped)")
        }
        return FeedContent(m)
    }

    /** §4.4: checklists with items, in the hub's order; 20 notes, 40 items each, items keep `i`. */
    private fun todoSection(notes: List<TodoNote>): MutableList<MutableMap<String, Any?>> =
        notes.filter { it.type == "checklist" && it.items.isNotEmpty() }.take(MAX_TODO_NOTES).map { n ->
            linkedMapOf<String, Any?>(
                "id" to n.id,
                "title" to n.title?.let { cut(it, TITLE_MAX) }?.takeIf { it.isNotEmpty() },
                "pinned" to n.pinned,
                "due" to n.dueDate?.takeIf { DATE.matches(it.take(10)) }?.take(10),
                "items" to n.items.mapIndexed { i, it -> Triple(i, it.text, it.done) }.take(MAX_TODO_ITEMS).map { (i, text, done) ->
                    linkedMapOf<String, Any?>("i" to i, "text" to cut(text, TODO_TEXT_MAX), "done" to done)
                }.toMutableList(),
            )
        }.toMutableList()

    /** Drops the last done item of the last checklist that has one; false if none is left. */
    @Suppress("UNCHECKED_CAST")
    private fun dropLastDoneItem(notes: List<MutableMap<String, Any?>>): Boolean {
        for (n in notes.asReversed()) {
            val items = n["items"] as MutableList<Map<String, Any?>>
            val k = items.indexOfLast { it["done"] == true }
            if (k >= 0) {
                items.removeAt(k)
                return true
            }
        }
        return false
    }

    private suspend fun briefing(today: LocalDate): Map<String, Any?>? {
        for (date in listOf(today, today.minusDays(1))) {
            when (val r = api.digest(date.toString())) {
                is ApiResult.Ok -> {
                    val d = r.value
                    val text = listOf(cut(d.str("headline"), TITLE_MAX), cut(d.str("summary"), BRIEFING_MAX))
                        .filter { it.isNotEmpty() }.joinToString("\n\n")
                    return linkedMapOf(
                        "date" to (d.str("date") ?: date.toString()),
                        "text" to text,
                        "highlights" to strings(d["highlights"], HIGHLIGHTS_MAX, POINT_MAX),
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

    private fun calMap(e: CalEvent): MutableMap<String, Any?>? {
        val start = PdaTime.utc(e.start, zone) ?: return null
        return linkedMapOf(
            "id" to e.id,
            "title" to cut(e.title, TITLE_MAX),
            "start" to start,
            "end" to (PdaTime.utc(e.end, zone) ?: start),
            "where" to e.where.takeIf { it.isNotBlank() }?.let { cut(it, TEXT_MAX) },
            "all_day" to e.allDay,
        )
    }

    companion object {
        const val MAX_FEED_BYTES = 32 * 1024
        const val CONVERSATION_WINDOW_S = 48 * 3600L
        const val MAX_CONVERSATIONS = 20
        const val CALENDAR_DAYS = 8L // today and the next 7 days
        const val TITLE_MAX = 120
        const val SUMMARY_MAX = 600
        const val SUMMARY_CUT = 240
        const val POINTS_MAX = 5
        const val POINT_MAX = 160
        const val TEXT_MAX = 200
        const val HIGHLIGHTS_MAX = 5
        const val BRIEFING_MAX = 1200
        const val MAX_TODO_NOTES = 20
        const val MAX_TODO_ITEMS = 40
        const val TODO_TEXT_MAX = 160
        private val DATE = Regex("\\d{4}-\\d{2}-\\d{2}")

        /** [text] as one line of at most [limit] characters, cut at a word with an ellipsis. */
        fun cut(text: String?, limit: Int): String {
            val s = (text ?: "").split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ")
            if (s.length <= limit) return s
            var head = s.substring(0, limit - 1)
            val space = head.lastIndexOf(' ')
            if (space > limit / 2) head = head.substring(0, space)
            return head.trimEnd(' ', ',', ';', ':', '.') + "…"
        }

        private fun strings(v: Any?, n: Int, limit: Int): List<String> =
            (v as? List<*>)?.filterIsInstance<String>()?.filter { it.isNotBlank() }?.map { cut(it, limit) }?.take(n)
                ?: emptyList()

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

        fun parseEvents(body: Map<String, Any?>): List<CalEvent> = body.objects("events").mapNotNull { parseEvent(it) }    }
}
