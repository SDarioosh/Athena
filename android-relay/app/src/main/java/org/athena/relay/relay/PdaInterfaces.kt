package org.athena.relay.relay

/**
 * The BLE link to one connected, bonded T5 PDA, after MTU negotiation and subscription to
 * `data` and `status` (docs/t5-pda-plan.md §3.4 steps 1-2). The Android implementation is
 * [org.athena.relay.ble.BlePdaLink]; tests use an in-memory fake PDA.
 */
interface PdaLink {
    /** The negotiated ATT MTU. */
    val mtu: Int

    suspend fun readInfo(): ByteArray

    suspend fun readStatus(): ByteArray

    /** Writes one command to `ctrl` (Write Request) and returns once the write is confirmed. */
    suspend fun write(command: ByteArray)

    /** The next `data` notification, in order. Throws [LinkException] on timeout or disconnect. */
    suspend fun receive(timeoutMs: Long): ByteArray

    /** The next `status` notification, or null if none came within [timeoutMs]. */
    suspend fun awaitStatus(timeoutMs: Long): ByteArray?
}

/** An HTTP read: the parsed value, an error classified like the watch's uploads, or "not supported". */
sealed class ApiResult<out T> {
    data class Ok<T>(val value: T) : ApiResult<T>()

    data class Fail(val error: ServerResult) : ApiResult<Nothing>()

    /** The other side has no such endpoint (or isn't set up). Not retried until settings change. */
    data class Unsupported(val reason: String) : ApiResult<Nothing>()
}

/** The Athena HTTP API calls the PDA needs (server/convo_server/api.py, §4). */
interface ConvoApi {
    /** `GET /health` (no token). */
    suspend fun health(): ServerResult

    /** `GET /v1/digests/{date}`; 404 when there is none. */
    suspend fun digest(date: String): ApiResult<Map<String, Any?>>

    /** `GET /v1/conversations?since=…&limit=…`, newest first. */
    suspend fun conversations(sinceEpochS: Long, limit: Int): ApiResult<List<Map<String, Any?>>>

    /** `GET /v1/action-items?status=open`. */
    suspend fun openActionItems(): ApiResult<List<Map<String, Any?>>>

    /** `GET /v1/conversations/{id}`. */
    suspend fun conversation(id: String): ApiResult<Map<String, Any?>>

    /** `GET /v1/conversations/{id}/transcript`. */
    suspend fun transcript(id: String): ApiResult<Map<String, Any?>>

    /** `PATCH /v1/action-items/{id}`. */
    suspend fun setActionItemStatus(id: Long, status: String): ServerResult

    /** `POST /v1/digests/{date}`. */
    suspend fun makeDigest(date: String): ServerResult
}

/** One calendar event as the feed carries it (§4.1). Times are ISO-8601 with an offset, or a date. */
data class CalEvent(
    val id: String,
    val title: String,
    val start: String,
    val end: String,
    val where: String,
    val allDay: Boolean,
)

/** A `cal_add` action after validation. [start]/[end] are ISO with an offset, or dates if [allDay]. */
data class NewEvent(
    val title: String,
    val start: String,
    val end: String?,
    val allDay: Boolean,
    val description: String = "",
)

/** The Odysseus calendar (hub `/api/codex/calendar/…`, docs/t5-pda-plan.md §6). */
interface CalendarApi {
    /** Events overlapping [fromIso, toIso). */
    suspend fun listEvents(fromIso: String, toIso: String): ApiResult<List<CalEvent>>

    /** Returns the new event's uid. Not idempotent on the hub: callers dedupe. */
    suspend fun createEvent(event: NewEvent): ApiResult<String>

    /** Deletes the event (the whole series). 404 = already gone. */
    suspend fun deleteEvent(uid: String): ApiResult<Unit>
}

/** Used when no Odysseus URL and token are set: the feed has `"calendar": []`, calendar actions wait. */
object NoCalendar : CalendarApi {
    private val why = ApiResult.Unsupported("Odysseus isn't set up in the relay settings")
    override suspend fun listEvents(fromIso: String, toIso: String) = why
    override suspend fun createEvent(event: NewEvent) = why
    override suspend fun deleteEvent(uid: String) = why
}

/** One Odysseus checklist item (plan §4.4). */
data class TodoItem(val text: String, val done: Boolean)

/** One Odysseus note as `GET /api/codex/todos?format=json` lists it (plan §4.4). */
data class TodoNote(
    val id: String,
    val title: String?,
    val type: String,
    val pinned: Boolean,
    val dueDate: String?,
    val items: List<TodoItem>,
)

/** The owner's Odysseus notes (hub `/api/codex/todos`, plan §4.4). */
interface TodosApi {
    /**
     * The notes in the hub's order. [ApiResult.Unsupported] for "no todos": Odysseus isn't set
     * up, the hub answers text (no `format=json`), or the token lacks `todos:read` (403).
     */
    suspend fun notes(): ApiResult<List<TodoNote>>

    /**
     * `toggle_item`. Ok(true) = `exit_code` 0, Ok(false) = `exit_code` 1 (the hub refused it:
     * result 1). [ApiResult.Fail] for 5xx / network errors, [ApiResult.Unsupported] as above.
     */
    suspend fun toggle(noteId: String, index: Int, done: Boolean): ApiResult<Boolean>
}

/** Used when no Odysseus URL and token are set: no `todos`, `todo_toggle` waits. */
object NoTodos : TodosApi {
    private val why = ApiResult.Unsupported("Odysseus isn't set up in the relay settings")
    override suspend fun notes() = why
    override suspend fun toggle(noteId: String, index: Int, done: Boolean) = why
}

/**
 * The hub's Today insight (`GET /api/codex/insight/today`, scope `insight:read`, plan §4.1),
 * with the calendar's URL and token.
 */
interface InsightApi {
    /**
     * The hub's answer, a JSON object (the feed builder checks its shape). [ApiResult.Unsupported]
     * for "no insight", with the reason: no insight yet (404), a hub without the route, a token
     * without `insight:read` (403), another final 4xx, or an answer that isn't a JSON object.
     * [ApiResult.Fail] for network errors, 5xx, 408/429 and 401, which fail the feed like the
     * calendar's.
     */
    suspend fun today(): ApiResult<Map<String, Any?>>

    /**
     * The hub's rule weights for the T5's rule-based Today plan (`GET /api/codex/insight/weights`,
     * same scope, plan §5): `{"version": 1, "date", "weights": {"list:<note id>": int}}`. Anything
     * but [ApiResult.Ok] (an older hub's 404, 403, network errors, 5xx, a non-object answer) keeps
     * the last good table; a weights failure never fails the feed.
     */
    suspend fun weights(): ApiResult<Map<String, Any?>> = ApiResult.Unsupported("no weights route")
}

/** Used when no Odysseus URL and token are set: the feed has no `insight` and no `weights`. */
object NoInsight : InsightApi {
    override suspend fun today() = ApiResult.Unsupported("Odysseus isn't set up in the relay settings")
    override suspend fun weights() = ApiResult.Unsupported("Odysseus isn't set up in the relay settings")
}

/**
 * What the relay remembers per PDA between connections. The app persists it in
 * SharedPreferences; tests use [MemoryPdaMemory].
 */
interface PdaMemory {
    /** Hash of the feed content (without `version` / `generated`) last pushed to [device]. */
    fun lastFeedHash(device: String): String?

    /** `version` of the feed last pushed to [device]. */
    fun lastFeedVersion(device: String): Long

    fun feedPushed(device: String, hash: String, version: Long)

    /** `cal_add` dedupe (§4.2): the event uid created for [actionKey], if any. */
    fun createdEvent(actionKey: String): String?

    fun eventCreated(actionKey: String, uid: String, nowMs: Long)

    /** The hub's last good `weights` table (plan §5) as compact JSON, or null: never fetched. */
    fun lastWeights(): String?

    fun weightsFetched(json: String)
}

class MemoryPdaMemory : PdaMemory {
    private val hashes = HashMap<String, Pair<String, Long>>()
    val created = LinkedHashMap<String, String>()

    @Synchronized override fun lastFeedHash(device: String) = hashes[device]?.first
    @Synchronized override fun lastFeedVersion(device: String) = hashes[device]?.second ?: 0L
    @Synchronized override fun feedPushed(device: String, hash: String, version: Long) {
        hashes[device] = hash to version
    }
    @Synchronized override fun createdEvent(actionKey: String) = created[actionKey]
    @Synchronized override fun eventCreated(actionKey: String, uid: String, nowMs: Long) {
        created[actionKey] = uid
    }
    var weights: String? = null
    @Synchronized override fun lastWeights() = weights
    @Synchronized override fun weightsFetched(json: String) {
        weights = json
    }
}
