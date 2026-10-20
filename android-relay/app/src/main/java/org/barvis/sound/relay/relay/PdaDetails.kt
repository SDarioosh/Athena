package org.barvis.sound.relay.relay

import org.barvis.sound.relay.net.Json
import org.barvis.sound.relay.net.bool
import org.barvis.sound.relay.net.long
import org.barvis.sound.relay.net.objects
import org.barvis.sound.relay.net.str
import org.barvis.sound.relay.net.strings
import java.time.ZoneId

/**
 * Detail blobs (`kind` 2, §4.3), the answer to a `fetch` action. Transcripts are paged by
 * offset ([PAGE_S] seconds per page by default); a page that would be larger than
 * [maxBytes] ends early and its `next` ref starts where it stopped.
 */
class DetailBuilder(
    private val api: ConvoApi,
    private val zone: ZoneId,
    private val maxBytes: Int = MAX_DETAIL_BYTES,
) {
    suspend fun build(ref: DetailRef): ApiResult<ByteArray> = when (ref) {
        is DetailRef.Conv -> when (val r = api.conversation(ref.id)) {
            is ApiResult.Ok -> ApiResult.Ok(conversation(ref, r.value))
            is ApiResult.Fail -> r
            is ApiResult.Unsupported -> r
        }
        is DetailRef.Transcript -> when (val r = api.transcript(ref.id)) {
            is ApiResult.Ok -> ApiResult.Ok(transcript(ref, r.value))
            is ApiResult.Fail -> r
            is ApiResult.Unsupported -> r
        }
    }

    private fun conversation(ref: DetailRef.Conv, c: Map<String, Any?>): ByteArray {
        val m = linkedMapOf<String, Any?>(
            "v" to 1,
            "ref" to ref.text,
            "title" to (c.str("title") ?: "Untitled conversation"),
            "at" to (PdaTime.inZone(c.str("started_at"), zone) ?: ""),
            "people" to c.strings("people"),
            "summary" to FeedBuilder.cutText(c.str("summary") ?: "", 4000),
            "points" to c.strings("key_points").take(20),
            "actions" to c.objects("action_items").map { a ->
                linkedMapOf<String, Any?>(
                    "id" to a.long("id"), "text" to (a.str("text") ?: ""), "owner" to (a.str("owner") ?: ""),
                    "due" to (a.str("due") ?: ""), "status" to (a.str("status") ?: ""),
                )
            },
        )
        return Json.write(m).toByteArray(Charsets.UTF_8)
    }

    private fun transcript(ref: DetailRef.Transcript, t: Map<String, Any?>): ByteArray {
        val from = ref.fromS ?: 0
        val to = ref.toS ?: (from + PAGE_S)
        val all = t.objects("segments")
        fun offset(s: Map<String, Any?>): Double = (s["offset"] as? Number)?.toDouble() ?: 0.0
        val page = all.filter { offset(it) >= from && offset(it) < to }.map { s ->
            linkedMapOf<String, Any?>(
                "o" to offset(s), "s" to (s.str("speaker") ?: ""), "me" to (s.bool("is_owner") ?: false),
                "x" to (s.str("text") ?: ""),
            )
        }.toMutableList()
        val more = all.any { offset(it) >= to }
        var nextFrom: Int? = if (more) to else null
        while (true) {
            val m = linkedMapOf<String, Any?>(
                "v" to 1,
                "ref" to ref.text,
                "title" to (t.str("title") ?: ""),
                "from" to from,
                "to" to (nextFrom ?: to),
                "segments" to page,
            )
            if (nextFrom != null) {
                val len = to - from
                m["next"] = DetailRef.Transcript(ref.id, nextFrom, nextFrom + len).text
            }
            val bytes = Json.write(m).toByteArray(Charsets.UTF_8)
            if (bytes.size <= maxBytes || page.size <= 1) return bytes
            // Too big: drop the tail; the next page starts at the first dropped segment.
            val dropped = page.removeAt(page.size - 1)
            nextFrom = (dropped["o"] as Double).toInt().coerceAtLeast(from + 1)
        }
    }

    companion object {
        const val PAGE_S = 600
        const val MAX_DETAIL_BYTES = 48 * 1024
    }
}
