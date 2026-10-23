package org.barvis.sound.relay.relay

import org.barvis.sound.relay.net.Json
import org.barvis.sound.relay.protocol.OutboxEntry
import java.time.DateTimeException
import java.time.LocalDate
import java.time.OffsetDateTime

/** A `fetch` reference, §4.2: `conv:<id>`, `transcript:<id>` or `transcript:<id>@<from>-<to>`. */
sealed class DetailRef {
    abstract val text: String
    abstract val id: String

    data class Conv(override val id: String) : DetailRef() {
        override val text get() = "conv:$id"
    }

    /** [fromS]/[toS] are offsets in seconds from the conversation's start; null = first page. */
    data class Transcript(override val id: String, val fromS: Double? = null, val toS: Double? = null) : DetailRef() {
        override val text get() = "transcript:$id" + if (fromS != null && toS != null) "@${num(fromS)}-${num(toS)}" else ""
    }

    companion object {
        /** Plan §3.5: ids `[A-Za-z0-9_.:-]{1,64}`, page bounds with up to 3 decimals. */
        private val REF = Regex("(conv|transcript):([A-Za-z0-9_.:-]{1,64})(?:@(\\d{1,6}(?:\\.\\d{1,3})?)-(\\d{1,6}(?:\\.\\d{1,3})?))?")

        fun parse(ref: String): DetailRef? {
            val m = REF.matchEntire(ref) ?: return null
            val (what, id, a, b) = m.destructured
            if (a.isEmpty()) return if (what == "conv") Conv(id) else Transcript(id)
            val from = a.toDouble()
            val to = b.toDouble()
            return if (what == "transcript" && to > from) Transcript(id, from, to) else null
        }

        /** `600.0` -> `600`, `12.5` -> `12.5` (as the PC relay writes page bounds). */
        fun num(x: Double): String = "%.3f".format(java.util.Locale.ROOT, x).trimEnd('0').trimEnd('.')
    }
}

/**
 * One outbox action after validation (§4.2, §7 and the strict checks of plan §3.5, shared
 * with the PC relay): a known `t`, exactly its fields, right types and ranges. Anything else
 * is [Invalid] and acked with result 1; raw JSON is never forwarded.
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

    /** Plan §4.4: set item [index] of note [note] to [done]; [text] is the item's first 80 characters. */
    data class TodoToggle(val note: String, val index: Int, val text: String, val done: Boolean) : PdaAction() {
        override val type get() = "todo_toggle"
    }

    /** Acked with result 1: the T5 shows "couldn't sync" and drops it. */
    data class Invalid(val reason: String) : PdaAction() {
        override val type get() = "invalid"
    }

    companion object {
        private val STATUSES = setOf("done", "dismissed", "open")
        private const val MAX_TITLE = 200
        private val DATE = Regex("\\d{4}-\\d{2}-\\d{2}")

        /** `t` -> (required, optional) fields. */
        private val FIELDS = mapOf(
            "item_status" to (setOf("id", "status") to emptySet<String>()),
            "cal_add" to (setOf("title", "start") to setOf("end", "all_day", "from_item")),
            "cal_delete" to (setOf("id") to emptySet<String>()),
            "fetch" to (setOf("ref") to emptySet<String>()),
            "digest_refresh" to (setOf("date") to emptySet<String>()),
            "todo_toggle" to (setOf("note", "i", "text", "done") to emptySet<String>()),
        )

        private class Bad(val reason: String) : Exception(reason)

        fun parse(json: ByteArray): PdaAction = try {
            parseStrict(json)
        } catch (e: Bad) {
            Invalid(e.reason)
        }

        private fun parseStrict(json: ByteArray): PdaAction {
            if (json.size > OutboxEntry.MAX_JSON) throw Bad("longer than ${OutboxEntry.MAX_JSON} bytes")
            val o = try {
                Json.parseObject(json.toString(Charsets.UTF_8))
            } catch (e: Json.ParseException) {
                throw Bad("not a JSON object")
            }
            val t = o["t"] as? String
            val (required, optional) = FIELDS[t] ?: throw Bad("unknown type")
            val keys = o.keys - "t"
            if (!keys.containsAll(required) || !(required + optional).containsAll(keys)) {
                throw Bad("$t: wrong fields")
            }
            return when (t) {
                "item_status" -> {
                    val status = o["status"] as? String
                    if (status !in STATUSES) throw Bad("item_status: bad status")
                    ItemStatus(positive(o["id"], "id"), status!!)
                }
                "cal_delete" -> CalDelete(str(o["id"], "id", 200))
                "digest_refresh" -> DigestRefresh(date(o["date"], "date").toString())
                "fetch" -> Fetch((o["ref"] as? String)?.let { DetailRef.parse(it) } ?: throw Bad("fetch: bad ref"))
                "todo_toggle" -> TodoToggle(
                    str(o["note"], "note", 64),
                    (o["i"] as? Long)?.takeIf { it in 0..Int.MAX_VALUE }?.toInt() ?: throw Bad("i must be an integer >= 0"),
                    (o["text"] as? String)?.takeIf { it.length in 1..80 } ?: throw Bad("text must be 1-80 characters"),
                    o["done"] as? Boolean ?: throw Bad("done must be true or false"),
                )
                else -> calAdd(o)
            }
        }

        private fun positive(v: Any?, name: String): Long =
            (v as? Long)?.takeIf { it > 0 } ?: throw Bad("$name must be a positive integer")

        private fun str(v: Any?, name: String, limit: Int): String =
            (v as? String)?.takeIf { it.isNotBlank() && it.length <= limit } ?: throw Bad("bad $name")

        private fun date(v: Any?, name: String): LocalDate {
            val s = v as? String
            if (s == null || !DATE.matches(s)) throw Bad("$name must be YYYY-MM-DD")
            return try {
                LocalDate.parse(s)
            } catch (e: DateTimeException) {
                throw Bad("$name is not a real date")
            }
        }

        /** An ISO-8601 date and time with an offset or `Z`; a time without one is refused. */
        private fun dateTime(v: Any?, name: String): OffsetDateTime {
            val s = v as? String
            if (s == null || s.length > 40 || 'T' !in s) throw Bad("$name must be an ISO-8601 date and time")
            return try {
                OffsetDateTime.parse(s)
            } catch (e: DateTimeException) {
                throw Bad("$name needs a UTC offset or Z")
            }
        }

        private fun calAdd(o: Map<String, Any?>): PdaAction {
            val title = str(o["title"], "title", MAX_TITLE)
            val allDay = when (val a = o["all_day"]) {
                null -> false
                is Boolean -> a
                else -> throw Bad("all_day must be true or false")
            }
            val fromItem = if ("from_item" in o) positive(o["from_item"], "from_item") else null
            val description = if (fromItem != null) {
                "Added on the T5 PDA from Athena action item #$fromItem"
            } else "Added on the T5 PDA"
            if (allDay) {
                if ("end" in o) throw Bad("an all-day event has no end")
                val start = date(o["start"], "start")
                return CalAdd(NewEvent(title.trim(), start.toString(), null, true, description), fromItem)
            }
            if ("end" !in o) throw Bad("cal_add needs end or all_day")
            val start = dateTime(o["start"], "start")
            val end = dateTime(o["end"], "end")
            if (!end.isAfter(start)) throw Bad("end must be after start")
            return CalAdd(NewEvent(title.trim(), o["start"] as String, o["end"] as String, false, description), fromItem)
        }
    }
}
