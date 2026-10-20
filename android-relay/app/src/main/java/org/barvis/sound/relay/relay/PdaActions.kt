package org.barvis.sound.relay.relay

import org.barvis.sound.relay.net.Json
import org.barvis.sound.relay.net.bool
import org.barvis.sound.relay.net.long
import org.barvis.sound.relay.net.str
import java.time.DateTimeException
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit

/** A `fetch` reference, §4.2: `conv:<id>`, `transcript:<id>` or `transcript:<id>@<from>-<to>`. */
sealed class DetailRef {
    abstract val text: String

    data class Conv(val id: String) : DetailRef() {
        override val text get() = "conv:$id"
    }

    /** [fromS]/[toS] are offsets in seconds from the conversation's start; null = first page. */
    data class Transcript(val id: String, val fromS: Int? = null, val toS: Int? = null) : DetailRef() {
        override val text get() = "transcript:$id" + if (fromS != null && toS != null) "@$fromS-$toS" else ""
    }

    companion object {
        private val CONV = Regex("conv:($ID)")
        private val TRANSCRIPT = Regex("transcript:($ID)(?:@(\\d{1,6})-(\\d{1,6}))?")

        fun parse(ref: String): DetailRef? {
            CONV.matchEntire(ref)?.let { return Conv(it.groupValues[1]) }
            val m = TRANSCRIPT.matchEntire(ref) ?: return null
            if (m.groupValues[2].isEmpty()) return Transcript(m.groupValues[1])
            val from = m.groupValues[2].toInt()
            val to = m.groupValues[3].toInt()
            return if (to > from) Transcript(m.groupValues[1], from, to) else null
        }
    }
}

/** Conversation ids are uuid hex on the server; this also keeps them safe in a URL path. */
private const val ID = "[A-Za-z0-9_-]{1,64}"

/**
 * One outbox action after validation (§4.2, §7: the phone checks each action's type and fields
 * before it calls a server and never forwards raw JSON).
 */
sealed class PdaAction {
    abstract val type: String

    data class ItemStatus(val id: Long, val status: String) : PdaAction() {
        override val type get() = "item_status"
    }

    data class CalAdd(val event: NewEvent, val fromItem: Long?) : PdaAction() {
        override val type get() = "cal_add"
    }

    data class CalDelete(val id: String) : PdaAction() {
        override val type get() = "cal_delete"
    }

    data class Fetch(val ref: DetailRef) : PdaAction() {
        override val type get() = "fetch"
    }

    data class DigestRefresh(val date: String) : PdaAction() {
        override val type get() = "digest_refresh"
    }

    /** Acked with result 1: the T5 shows "couldn't sync" and drops it. */
    data class Invalid(val reason: String) : PdaAction() {
        override val type get() = "invalid"
    }

    companion object {
        private val STATUSES = setOf("done", "dismissed", "open")
        private const val MAX_TITLE = 200
        private val DATE = Regex("\\d{4}-\\d{2}-\\d{2}")
        private val ISO_OFFSET: DateTimeFormatter = DateTimeFormatter.ISO_OFFSET_DATE_TIME

        /** [zone] is the phone's: a cal_add time without an offset is local time there. */
        fun parse(json: ByteArray, zone: ZoneId): PdaAction {
            if (json.size > org.barvis.sound.relay.protocol.OutboxEntry.MAX_JSON) return Invalid("longer than 400 bytes")
            val o = try {
                Json.parseObject(json.toString(Charsets.UTF_8))
            } catch (e: Json.ParseException) {
                return Invalid("not a JSON object")
            }
            return when (val t = o.str("t")) {
                "item_status" -> {
                    val id = o.long("id")?.takeIf { it > 0 } ?: return Invalid("item_status: bad id")
                    val status = o.str("status")?.takeIf { it in STATUSES } ?: return Invalid("item_status: bad status")
                    ItemStatus(id, status)
                }
                "cal_add" -> calAdd(o, zone)
                "cal_delete" -> {
                    val id = o.str("id")?.takeIf { it.isNotBlank() && it.length <= 200 && it.none { c -> c < ' ' } }
                        ?: return Invalid("cal_delete: bad id")
                    CalDelete(id)
                }
                "fetch" -> {
                    val ref = o.str("ref")?.let { DetailRef.parse(it) } ?: return Invalid("fetch: bad ref")
                    Fetch(ref)
                }
                "digest_refresh" -> {
                    val date = o.str("date")?.takeIf { DATE.matches(it) && validDate(it) }
                        ?: return Invalid("digest_refresh: bad date")
                    DigestRefresh(date)
                }
                null -> Invalid("no type")
                else -> Invalid("unknown type '${t.take(20)}'")
            }
        }

        private fun validDate(s: String) = try {
            LocalDate.parse(s); true
        } catch (e: DateTimeException) {
            false
        }

        private fun calAdd(o: Map<String, Any?>, zone: ZoneId): PdaAction {
            val title = o.str("title")?.filter { it >= ' ' }?.trim()?.takeIf { it.isNotEmpty() }
                ?: return Invalid("cal_add: no title")
            if (title.length > MAX_TITLE) return Invalid("cal_add: title too long")
            val allDay = o.bool("all_day") ?: false
            val startRaw = o.str("start") ?: return Invalid("cal_add: no start")
            val endRaw = o.str("end")
            val fromItem = o.long("from_item")?.takeIf { it > 0 }
            val description = if (fromItem != null) {
                "Added on the T5 PDA from odysseus-sound action item #$fromItem"
            } else "Added on the T5 PDA"

            if (allDay) {
                val start = date(startRaw) ?: return Invalid("cal_add: bad start")
                val end = endRaw?.let { date(it) ?: return Invalid("cal_add: bad end") }
                if (end != null && !end.isAfter(start)) return Invalid("cal_add: end before start")
                return CalAdd(NewEvent(title, start.toString(), end?.toString(), true, description), fromItem)
            }
            val start = dateTime(startRaw, zone) ?: return Invalid("cal_add: bad start")
            val end = endRaw?.let { dateTime(it, zone) ?: return Invalid("cal_add: bad end") }
            if (end != null && !end.isAfter(start)) return Invalid("cal_add: end before start")
            return CalAdd(NewEvent(title, ISO_OFFSET.format(start), end?.let { ISO_OFFSET.format(it) }, false, description),
                fromItem)
        }

        /** `YYYY-MM-DD`, or the date part of a date-time. */
        private fun date(s: String): LocalDate? = try {
            LocalDate.parse(s.take(10))
        } catch (e: DateTimeException) {
            null
        }

        /** ISO date-time with an offset, or without one (local time in [zone]). Seconds kept, no fraction. */
        private fun dateTime(s: String, zone: ZoneId): OffsetDateTime? = try {
            OffsetDateTime.parse(s)
        } catch (e: DateTimeException) {
            try {
                LocalDateTime.parse(s).atZone(zone).toOffsetDateTime()
            } catch (e2: DateTimeException) {
                null
            }
        }?.truncatedTo(ChronoUnit.SECONDS)
    }
}
