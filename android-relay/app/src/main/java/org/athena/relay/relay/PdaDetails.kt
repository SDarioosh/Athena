package org.athena.relay.relay

import org.athena.relay.net.Json
import org.athena.relay.net.bool
import org.athena.relay.net.objects
import org.athena.relay.net.str
import org.athena.relay.protocol.PdaGatt

/**
 * Detail blobs (`kind` 2), the answer to a `fetch` action, in the formats of plan §4.3 (the
 * same as the PC relay's):
 * - `conv:<id>`: `v`, `ref`, `title`, `at`, `people`, `summary`, `points`,
 *   `actions` [`id`, `text`, `status`, `due`]
 * - `transcript:<id>[@from-to]`: `v`, `ref`, `title`, `from`, `to`, `next`, `segments`
 *   [`o`, `s`, `me`, `x`]. The first page is 0–600 s; `next` is the following page's ref,
 *   or null at the end. Only an absurdly long page is cut to fit [maxBytes].
 */
class DetailBuilder(
    private val api: ConvoApi,
    private val maxBytes: Int = PdaGatt.MAX_BLOB,
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
            "title" to FeedBuilder.cut(c.str("title"), FeedBuilder.TITLE_MAX),
            "at" to PdaTime.utc(c.str("started_at")),
            "people" to ((c["people"] as? List<*>) ?: emptyList<Any?>()),
            "summary" to (c.str("summary") ?: ""),
            "points" to ((c["key_points"] as? List<*>) ?: emptyList<Any?>()),
            "actions" to c.objects("action_items").filter { it["id"] != null }.map { a ->
                linkedMapOf("id" to a["id"], "text" to a["text"], "status" to a["status"], "due" to a["due_date"])
            },
        )
        return Json.write(m).toByteArray(Charsets.UTF_8)
    }

    private fun transcript(ref: DetailRef.Transcript, t: Map<String, Any?>): ByteArray {
        val from = ref.fromS ?: 0.0
        var to = ref.toS ?: (from + PAGE_S)
        val segs = ArrayList<Map<String, Any?>>()
        var more = false
        for (s in t.objects("segments")) {
            val o = (s["offset"] as? Number)?.toDouble() ?: 0.0
            if (o < from) continue
            if (o >= to) {
                more = true
                break
            }
            segs += linkedMapOf(
                "o" to o, "s" to (s.str("speaker")?.takeIf { it.isNotEmpty() } ?: "?"),
                "me" to (s.bool("is_owner") ?: false), "x" to (s.str("text") ?: ""),
            )
        }
        fun blob(): ByteArray {
            val m = linkedMapOf<String, Any?>(
                "v" to 1, "ref" to ref.text, "title" to FeedBuilder.cut(t.str("title"), FeedBuilder.TITLE_MAX),
                "from" to from, "to" to to,
                "next" to if (more) DetailRef.Transcript(ref.id, to, to + PAGE_S).text else null,
                "segments" to segs,
            )
            return Json.write(m).toByteArray(Charsets.UTF_8)
        }
        var bytes = blob()
        while (bytes.size > maxBytes && segs.isNotEmpty()) {
            to = segs.removeAt(segs.size - 1)["o"] as Double
            more = true
            bytes = blob()
        }
        return bytes
    }

    companion object {
        const val PAGE_S = 600.0
    }
}
