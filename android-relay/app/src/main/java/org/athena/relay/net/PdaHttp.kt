package org.athena.relay.net

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.athena.relay.relay.ApiResult
import org.athena.relay.relay.CalEvent
import org.athena.relay.relay.CalendarApi
import org.athena.relay.relay.ConvoApi
import org.athena.relay.relay.FeedBuilder
import org.athena.relay.relay.InsightApi
import org.athena.relay.relay.Level
import org.athena.relay.relay.NewEvent
import org.athena.relay.relay.RelayLog
import org.athena.relay.relay.ServerResult
import org.athena.relay.relay.TodoItem
import org.athena.relay.relay.TodoNote
import org.athena.relay.relay.TodosApi
import java.io.IOException
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.ProtocolException
import java.net.URL
import java.net.URLEncoder

/** A raw HTTP answer. */
class HttpAnswer(val code: Int, val body: String)

/** One HTTP exchange; throws [IOException] on network errors. Tests use a fake. */
fun interface HttpTransport {
    suspend fun exchange(method: String, url: String, headers: Map<String, String>, body: ByteArray?): HttpAnswer
}

/**
 * [HttpTransport] over [HttpURLConnection], with the uploader's rules: no redirects (a token is
 * never sent anywhere else), no caches, bounded answers.
 */
class UrlConnectionTransport(
    private val connectTimeoutMs: Int = 10_000,
    private val readTimeoutMs: Int = 20_000,
    private val maxBody: Int = 2 * 1024 * 1024,
) : HttpTransport {
    override suspend fun exchange(method: String, url: String, headers: Map<String, String>, body: ByteArray?): HttpAnswer =
        withContext(Dispatchers.IO) {
            val conn = URL(url).openConnection() as HttpURLConnection
            try {
                try {
                    conn.requestMethod = method
                } catch (e: ProtocolException) {
                    // The JDK's HttpURLConnection has no PATCH; Android's (OkHttp-based) has.
                    throw IOException("this HTTP stack can't send $method")
                }
                conn.connectTimeout = connectTimeoutMs
                conn.readTimeout = readTimeoutMs
                conn.instanceFollowRedirects = false
                conn.useCaches = false
                conn.setRequestProperty("Accept", "application/json")
                conn.setRequestProperty("User-Agent", "barvis-relay")
                for ((k, v) in headers) conn.setRequestProperty(k, v)
                if (body != null) {
                    conn.doOutput = true
                    conn.setFixedLengthStreamingMode(body.size)
                    conn.outputStream.use { it.write(body) }
                }
                val code = conn.responseCode
                HttpAnswer(code, read(if (code < 400) conn.inputStream else conn.errorStream))
            } finally {
                conn.disconnect()
            }
        }

    private fun read(stream: InputStream?): String {
        if (stream == null) return ""
        return stream.use { s ->
            val out = java.io.ByteArrayOutputStream()
            val buf = ByteArray(16 * 1024)
            while (out.size() < maxBody) {
                val n = s.read(buf, 0, minOf(buf.size, maxBody - out.size()))
                if (n < 0) break
                out.write(buf, 0, n)
            }
            out.toString(Charsets.UTF_8.name())
        }
    }
}

/** Shared request helper: bearer header, JSON in and out, errors classified like the uploader's. */
internal class JsonClient(
    baseUrl: String,
    private val token: () -> String,
    private val transport: HttpTransport,
    private val log: RelayLog,
    private val name: String,
) {
    private val base = baseUrl.trimEnd('/')

    suspend fun call(method: String, path: String, auth: Boolean = true, json: Any? = null, logPath: String = path): HttpAnswer? {
        val headers = LinkedHashMap<String, String>()
        if (auth) headers["Authorization"] = "Bearer ${token()}"
        val body = json?.let {
            headers["Content-Type"] = "application/json"
            Json.write(it).toByteArray(Charsets.UTF_8)
        }
        return try {
            transport.exchange(method, base + path, headers, body).also {
                log.log(Level.INFO, "$name $method $logPath -> HTTP ${it.code}")
            }
        } catch (e: IOException) {
            log.log(Level.INFO, "$name $method $logPath -> ${e.message ?: e.javaClass.simpleName}")
            null
        }
    }

    fun networkError(): ServerResult = ServerResult.Transient("$name not reachable (network error)")

    /** A 2xx JSON object, or the classified error. */
    suspend fun obj(path: String, logPath: String = path): ApiResult<Map<String, Any?>> {
        val a = call("GET", path, logPath = logPath) ?: return ApiResult.Fail(networkError())
        return objOf(a)
    }

    fun objOf(a: HttpAnswer): ApiResult<Map<String, Any?>> {
        if (a.code !in 200..299) return ApiResult.Fail(HttpUploader.classify(a.code, a.body, needOkField = false))
        return try {
            ApiResult.Ok(Json.parseObject(a.body))
        } catch (e: Json.ParseException) {
            ApiResult.Fail(ServerResult.Transient("$name answered HTTP ${a.code} without JSON"))
        }
    }

    companion object {
        fun enc(s: String): String = URLEncoder.encode(s, "UTF-8").replace("+", "%20")

        /** FastAPI's `{"detail": "…"}`. */
        fun detail(body: String): String? = try {
            Json.parseObject(body).str("detail")
        } catch (e: Json.ParseException) {
            null
        }
    }
}

/**
 * The Athena HTTP API (server/convo_server/api.py), directly or through Odysseus
 * ([routes]: the same paths under `/api/codex/sound` and the same JSON). The token is never
 * logged.
 */
class ConvoHttpApi(
    baseUrl: String,
    token: () -> String,
    transport: HttpTransport = UrlConnectionTransport(),
    log: RelayLog = RelayLog.NONE,
    private val routes: Routes = Routes.DIRECT,
) : ConvoApi {
    private val c = JsonClient(baseUrl, token, transport, log, routes.name)
    private val p = routes.prefix

    override suspend fun health(): ServerResult {
        val a = c.call("GET", routes.health, auth = routes.healthNeedsToken) ?: return c.networkError()
        return HttpUploader.classify(a.code, a.body)
    }

    override suspend fun digest(date: String): ApiResult<Map<String, Any?>> {
        require(DATE.matches(date)) { "bad date" }
        return c.obj("$p/digests/$date")
    }

    override suspend fun conversations(sinceEpochS: Long, limit: Int): ApiResult<List<Map<String, Any?>>> =
        list(c.obj("$p/conversations?since=$sinceEpochS&limit=$limit"), "conversations")

    override suspend fun openActionItems(): ApiResult<List<Map<String, Any?>>> =
        list(c.obj("$p/action-items?status=open"), "action_items")

    override suspend fun conversation(id: String): ApiResult<Map<String, Any?>> =
        c.obj("$p/conversations/${JsonClient.enc(id)}")

    override suspend fun transcript(id: String): ApiResult<Map<String, Any?>> =
        c.obj("$p/conversations/${JsonClient.enc(id)}/transcript")

    override suspend fun setActionItemStatus(id: Long, status: String): ServerResult {
        val a = c.call("PATCH", "$p/action-items/$id", json = mapOf("status" to status)) ?: return c.networkError()
        return HttpUploader.classify(a.code, a.body)
    }

    override suspend fun makeDigest(date: String): ServerResult {
        require(DATE.matches(date)) { "bad date" }
        val a = c.call("POST", "$p/digests/$date", json = emptyMap<String, Any?>()) ?: return c.networkError()
        return HttpUploader.classify(a.code, a.body, needOkField = false)
    }

    private fun list(r: ApiResult<Map<String, Any?>>, key: String): ApiResult<List<Map<String, Any?>>> = when (r) {
        is ApiResult.Ok -> if (r.value[key] is List<*>) ApiResult.Ok(r.value.objects(key))
            else ApiResult.Fail(ServerResult.Transient("${routes.name} answer has no \"$key\""))
        is ApiResult.Fail -> r
        is ApiResult.Unsupported -> r
    }

    private companion object {
        val DATE = Regex("\\d{4}-\\d{2}-\\d{2}")
    }
}

/**
 * The owner's Odysseus notes through `/api/codex/todos` (plan §4.4), with the same URL and
 * token as the calendar (scopes `todos:read`, `todos:write`).
 */
class OdysseusTodosClient(
    baseUrl: String,
    token: () -> String,
    transport: HttpTransport = UrlConnectionTransport(),
    log: RelayLog = RelayLog.NONE,
) : TodosApi {
    private val c = JsonClient(baseUrl, token, transport, log, "Odysseus")

    override suspend fun notes(): ApiResult<List<TodoNote>> {
        val a = c.call("GET", "$TODOS?format=json", logPath = TODOS) ?: return ApiResult.Fail(c.networkError())
        noTodos(a)?.let { return it }
        if (a.code !in 200..299) return ApiResult.Fail(HttpUploader.classify(a.code, a.body, needOkField = false))
        val body = try {
            Json.parseObject(a.body)
        } catch (e: Json.ParseException) {
            return ApiResult.Unsupported(OLD_HUB) // the chat-model text of a hub without format=json
        }
        if (body["notes"] !is List<*>) return ApiResult.Unsupported(OLD_HUB)
        return ApiResult.Ok(body.objects("notes").mapNotNull { n ->
            val id = n.str("id") ?: (n["id"] as? Long)?.toString() ?: return@mapNotNull null
            TodoNote(
                id = id,
                title = n.str("title"),
                type = n.str("type") ?: "",
                pinned = n.bool("pinned") ?: false,
                dueDate = n.str("due_date"),
                items = n.objects("items").map { TodoItem(it.str("text") ?: "", it.bool("done") ?: false) },
            )
        })
    }

    override suspend fun toggle(noteId: String, index: Int, done: Boolean): ApiResult<Boolean> {
        val body = linkedMapOf<String, Any?>("action" to "toggle_item", "note_id" to noteId, "index" to index, "done" to done)
        val a = c.call("POST", TODOS, json = body) ?: return ApiResult.Fail(c.networkError())
        noTodos(a)?.let { return it }
        return when (val r = c.objOf(a)) {
            // Plan §4.4: an exit_code other than 0 is result 1; only a missing one is temporary.
            is ApiResult.Ok -> when (r.value.long("exit_code")) {
                null -> ApiResult.Fail(ServerResult.Transient("Odysseus todos answer has no exit_code"))
                0L -> ApiResult.Ok(true)
                else -> ApiResult.Ok(false)
            }
            is ApiResult.Fail -> r
            is ApiResult.Unsupported -> r
        }
    }

    /** 403 (no `todos` scope) and a missing route mean "no todos", not a failure. */
    private fun noTodos(a: HttpAnswer): ApiResult.Unsupported? = when (a.code) {
        403 -> ApiResult.Unsupported(NO_SCOPE)
        404, 405 -> ApiResult.Unsupported(OLD_HUB)
        else -> null
    }

    companion object {
        const val TODOS = "/api/codex/todos"
        const val NO_SCOPE = "add the Todos and Todos write permissions to the Odysseus token"
        const val OLD_HUB = "this Odysseus can't list checklists (update the hub)"
    }
}

/**
 * The hub's Today insight through `/api/codex/insight/today` (plan §4.1), and its rule weights
 * through `/api/codex/insight/weights` (plan §5), with the same URL and token as the calendar
 * (scope `insight:read`). The reasons are the PC relay's words.
 */
class OdysseusInsightClient(
    baseUrl: String,
    token: () -> String,
    transport: HttpTransport = UrlConnectionTransport(),
    log: RelayLog = RelayLog.NONE,
) : InsightApi {
    private val c = JsonClient(baseUrl, token, transport, log, "Odysseus")

    override suspend fun today(): ApiResult<Map<String, Any?>> {
        val a = c.call("GET", INSIGHT) ?: return ApiResult.Fail(c.networkError())
        when {
            a.code == 403 -> return ApiResult.Unsupported(NO_SCOPE)
            // FastAPI's 404 for a missing route says "Not Found"; no insight yet says {"error": …}.
            a.code == 405 || a.code == 404 && JsonClient.detail(a.body) == "Not Found" -> return ApiResult.Unsupported(OLD_HUB)
            a.code == 404 -> return ApiResult.Unsupported(NOT_YET)
            a.code == 200 -> return try {
                ApiResult.Ok(Json.parseObject(a.body))
            } catch (e: Json.ParseException) {
                ApiResult.Unsupported(MALFORMED)
            }
        }
        return when (val r = HttpUploader.classify(a.code, a.body, needOkField = false)) {
            is ServerResult.Rejected -> ApiResult.Unsupported("Odysseus insight: HTTP ${a.code}") // another final 4xx
            is ServerResult.Ok -> ApiResult.Fail(ServerResult.Transient("Odysseus insight: HTTP ${a.code}")) // an odd 2xx
            else -> ApiResult.Fail(r) // network, 5xx, 408/429, 3xx, 401: like the calendar
        }
    }

    override suspend fun weights(): ApiResult<Map<String, Any?>> {
        val a = c.call("GET", WEIGHTS) ?: return ApiResult.Fail(c.networkError())
        return when (a.code) {
            200 -> try {
                ApiResult.Ok(Json.parseObject(a.body))
            } catch (e: Json.ParseException) {
                ApiResult.Unsupported(WEIGHTS_MALFORMED)
            }
            404, 405 -> ApiResult.Unsupported(WEIGHTS_OLD_HUB)
            403 -> ApiResult.Unsupported(NO_SCOPE)
            else -> ApiResult.Fail(HttpUploader.classify(a.code, a.body, needOkField = false))
        }
    }

    companion object {
        const val INSIGHT = "/api/codex/insight/today"
        const val WEIGHTS = "/api/codex/insight/weights"
        const val WEIGHTS_OLD_HUB = "this Odysseus has no weights route (update the hub)"
        const val WEIGHTS_MALFORMED = FeedBuilder.WEIGHTS_MALFORMED
        const val NOT_YET = FeedBuilder.INSIGHT_NOT_YET
        const val NO_SCOPE = "token lacks insight:read"
        const val OLD_HUB = "this Odysseus has no insight route (update the hub)"
        const val MALFORMED = FeedBuilder.INSIGHT_MALFORMED
    }
}

/**
 * The Odysseus calendar through the hub's token-aware routes, docs/t5-pda-plan.md §6:
 * `/api/codex/calendar/events` with an `ody_…` token scoped `calendar:read,calendar:write`,
 * sent as a Bearer header (the hub has no query-token form). [calendarId] is the CalDAV
 * calendar's id (`calendar_href`); without it the hub picks the owner's first calendar, which
 * may not be written back to CalDAV.
 */
class OdysseusCalendarClient(
    baseUrl: String,
    token: () -> String,
    private val calendarId: String? = null,
    transport: HttpTransport = UrlConnectionTransport(),
    log: RelayLog = RelayLog.NONE,
) : CalendarApi {
    private val c = JsonClient(baseUrl, token, transport, log, "Odysseus")

    override suspend fun listEvents(fromIso: String, toIso: String): ApiResult<List<CalEvent>> {
        val q = "start=${JsonClient.enc(fromIso)}&end=${JsonClient.enc(toIso)}" +
            (calendarId?.takeIf { it.isNotBlank() }?.let { "&calendar=${JsonClient.enc(it)}" } ?: "")
        val a = c.call("GET", "$EVENTS?$q", logPath = EVENTS) ?: return ApiResult.Fail(c.networkError())
        unsupported(a)?.let { return it }
        return when (val r = c.objOf(a)) {
            is ApiResult.Ok -> if (r.value["events"] is List<*>) ApiResult.Ok(FeedBuilder.parseEvents(r.value))
                else ApiResult.Fail(ServerResult.Transient("Odysseus answer has no \"events\""))
            is ApiResult.Fail -> r
            is ApiResult.Unsupported -> r
        }
    }

    override suspend fun createEvent(event: NewEvent): ApiResult<String> {
        val body = linkedMapOf<String, Any?>(
            "summary" to event.title,
            "dtstart" to event.start,
            "all_day" to event.allDay,
            "description" to event.description,
        )
        if (event.end != null) body["dtend"] = event.end
        calendarId?.takeIf { it.isNotBlank() }?.let { body["calendar_href"] = it }
        val a = c.call("POST", EVENTS, json = body) ?: return ApiResult.Fail(c.networkError())
        unsupported(a)?.let { return it }
        return when (val r = c.objOf(a)) {
            is ApiResult.Ok -> r.value.str("uid")?.let { ApiResult.Ok(it) }
                ?: ApiResult.Fail(ServerResult.Transient("Odysseus answer has no event uid"))
            is ApiResult.Fail -> r
            is ApiResult.Unsupported -> r
        }
    }

    override suspend fun deleteEvent(uid: String): ApiResult<Unit> {
        // An occurrence id (`<uid>::<occurrence>`): the hub would delete the whole series.
        // Refused without a call, as by the PC relay (pda_calendar.py), so the T5 gets result 1.
        if ("::" in uid) return ApiResult.Fail(ServerResult.Rejected(400, "one occurrence of a recurring event can't be deleted"))
        val a = c.call("DELETE", "$EVENTS/${JsonClient.enc(uid)}", logPath = "$EVENTS/…")
            ?: return ApiResult.Fail(c.networkError())
        // FastAPI's 404 for a missing route says "Not Found"; a missing event says "Event not found".
        if (a.code == 404 && JsonClient.detail(a.body) == "Not Found" || a.code == 405) return notThere()
        if (a.code == 503) unsupported(a)?.let { return it }
        return if (a.code in 200..299) ApiResult.Ok(Unit)
            else ApiResult.Fail(HttpUploader.classify(a.code, a.body, needOkField = false))
    }

    /**
     * The token owner's calendars (`GET /api/codex/calendars`, scope `calendar:read`), for
     * Settings' "Choose calendar". Unsupported on a hub without the route.
     */
    suspend fun calendars(): ApiResult<List<CalendarChoice>> {
        val a = c.call("GET", CALENDARS) ?: return ApiResult.Fail(c.networkError())
        if (a.code == 404 || a.code == 405) return ApiResult.Unsupported("this Odysseus can't list calendars (update the hub)")
        return when (val r = c.objOf(a)) {
            is ApiResult.Ok -> ApiResult.Ok(r.value.objects("calendars").mapNotNull { cal ->
                val href = cal.str("href") ?: return@mapNotNull null
                CalendarChoice(href, cal.str("name") ?: href, cal.str("source") ?: "")
            })
            is ApiResult.Fail -> r
            is ApiResult.Unsupported -> r
        }
    }

    /** "Not supported" rather than a guess: the route is missing, or the hub has no calendar. */
    private fun unsupported(a: HttpAnswer): ApiResult.Unsupported? = when {
        a.code == 404 || a.code == 405 -> notThere()
        a.code == 503 && JsonClient.detail(a.body)?.contains("not available") == true ->
            ApiResult.Unsupported("Odysseus has no calendar integration enabled")
        else -> null
    }

    private fun notThere() =
        ApiResult.Unsupported("this Odysseus has no $EVENTS route (update the hub)")

    private companion object {
        const val EVENTS = "/api/codex/calendar/events"
        const val CALENDARS = "/api/codex/calendars"
    }
}

/** One of the owner's Odysseus calendars; [id] is what `calendar_href` takes. */
data class CalendarChoice(val id: String, val name: String, val source: String) {
    val label: String get() = if (source == "caldav") "$name (CalDAV)" else name
}
