package org.athena.relay.relay

import org.athena.relay.net.Json
import org.athena.relay.protocol.BlobKind
import org.athena.relay.protocol.OutboxEntry
import org.athena.relay.protocol.PdaCmd
import org.athena.relay.protocol.PdaCommand
import org.athena.relay.protocol.PdaErrorCode
import org.athena.relay.protocol.PdaGatt
import org.athena.relay.protocol.PdaResponse
import org.athena.relay.protocol.PdaStatus
import org.athena.relay.protocol.crc32
import java.io.ByteArrayOutputStream

/** A settable clock for tests. */
class TestClock(var ms: Long = 1792483200000L) : Clock { // 2026-10-20T08:00:00Z
    override fun nowMs() = ms
}

/** An in-memory T5: the GATT server side of docs/t5-pda-plan.md §3. */
class FakePda(
    private val timeline: Timeline = Timeline(),
    private val clock: TestClock = TestClock(),
    override var mtu: Int = 517,
    var info: String = "proto=1;device=t5-pda;fw=0.1.0;board=t5_epaper_pro",
) : PdaLink {
    val outbox = sortedMapOf<Long, OutboxEntry>()
    val commands = mutableListOf<PdaCommand>()
    val acks = mutableListOf<Pair<Long, Int>>()
    var feed: ByteArray? = null
    var feedVersion = 0L
    val details = mutableListOf<Map<String, Any?>>()
    var flags = 0x01
    var busyErrors = 0
    var connected = true
    var timeSet: PdaCommand.Time? = null

    /** How many more `PUT_END`s answer `bad_crc`. */
    var badCrc = 0
    /** Blobs larger than this answer `too_large`. */
    var maxBlob = PdaGatt.MAX_BLOB
    /** Action ids whose ack is applied, but the link drops before `OB_ACKED` arrives. */
    val dropAfterAck = mutableSetOf<Long>()
    /** What [awaitStatus] returns, in order (null = a timeout); empty = timeouts. */
    val statusScript = ArrayDeque<PdaStatus?>()
    /** Called each time [awaitStatus] runs, e.g. to queue a new outbox entry. */
    var onAwaitStatus: (() -> Unit)? = null
    /** A buggy firmware: the `after_id` it really lists from, given the one asked for. */
    var listFrom: (Long) -> Long = { it }
    /** Fails the test instead of letting a relay loop forever. */
    var maxLists = 100

    private val pending = ArrayDeque<ByteArray>()
    private var putKind = 0
    private var putId = 0L
    private var putTotal = 0L
    private var putCrc = 0L
    private var putBuf: ByteArrayOutputStream? = null
    private var nextId = 1L

    fun queue(json: String, createdMs: Long = clock.ms - 60_000, id: Long = nextId++): OutboxEntry {
        val e = OutboxEntry(id, createdMs, json.toByteArray())
        outbox[id] = e
        if (id >= nextId) nextId = id + 1
        return e
    }

    fun status() = PdaStatus(1, flags, 77, outbox.size, feedVersion, 0x0a0b0c0d)

    @Suppress("UNCHECKED_CAST")
    fun feedJson(): Map<String, Any?> = Json.parseObject(feed!!.toString(Charsets.UTF_8))

    fun reconnect() {
        connected = true
        pending.clear()
    }

    override suspend fun readInfo(): ByteArray = checkLink().let { info.toByteArray() }
    override suspend fun readStatus(): ByteArray = checkLink().let { status().encode() }

    override suspend fun awaitStatus(timeoutMs: Long): ByteArray? {
        checkLink()
        onAwaitStatus?.invoke()
        val s = statusScript.removeFirstOrNull()
        if (s == null) clock.ms += timeoutMs else clock.ms += 1000
        return s?.encode()
    }

    override suspend fun write(command: ByteArray) {
        checkLink()
        if (pending.isNotEmpty()) {
            busyErrors++
            pending.addLast(PdaResponse.Error(command[0].toInt() and 0xFF, PdaErrorCode.BUSY).encode())
            return
        }
        if (mtu < PdaGatt.MIN_MTU) {
            pending.addLast(PdaResponse.Error(command[0].toInt() and 0xFF, PdaErrorCode.MTU_TOO_SMALL, mtu.toLong()).encode())
            return
        }
        val c = PdaCommand.decode(command)
        commands += c
        if (c !is PdaCommand.PutData) timeline.add("pda:" + describe(c))
        when (c) {
            is PdaCommand.Time -> {
                timeSet = c
                pending.addLast(PdaResponse.TimeOk.encode())
            }
            is PdaCommand.ObList -> {
                check(commands.count { it is PdaCommand.ObList } <= maxLists) { "relay sent more than $maxLists OB_LISTs" }
                val all = outbox.tailMap(listFrom(c.afterId) + 1).values.toList()
                val sent = all.take(c.max)
                sent.forEach { pending.addLast(it.encode()) }
                pending.addLast(PdaResponse.ObEnd(sent.size, (all.size - sent.size).toLong()).encode())
            }
            is PdaCommand.ObAck -> {
                val removed = outbox.remove(c.actionId)
                if (removed == null) {
                    pending.addLast(PdaResponse.Error(PdaCmd.OB_ACK, PdaErrorCode.NOT_FOUND, c.actionId).encode())
                    return
                }
                acks += c.actionId to c.result
                if (c.actionId in dropAfterAck) {
                    dropAfterAck.remove(c.actionId)
                    pending.addLast(DROP)
                    return
                }
                pending.addLast(PdaResponse.ObAcked(c.result, c.actionId).encode())
            }
            is PdaCommand.PutBegin -> {
                if (c.total > maxBlob) {
                    pending.addLast(PdaResponse.Error(PdaCmd.PUT_BEGIN, PdaErrorCode.TOO_LARGE, c.total).encode())
                    return
                }
                putKind = c.kind; putId = c.blobId; putTotal = c.total; putCrc = c.crc32
                putBuf = ByteArrayOutputStream()
                pending.addLast(PdaResponse.PutReady(c.kind, c.blobId).encode())
            }
            is PdaCommand.PutData -> {
                val buf = putBuf
                check(command.size <= minOf(mtu - 3, 512)) { "PUT_DATA of ${command.size} B doesn't fit MTU $mtu or 512" }
                if (buf == null || c.offset != buf.size().toLong()) {
                    pending.addLast(PdaResponse.Error(PdaCmd.PUT_DATA, PdaErrorCode.BAD_STATE).encode())
                    return
                }
                buf.write(c.payload)
            }
            PdaCommand.PutEnd -> {
                val bytes = putBuf?.toByteArray()
                putBuf = null
                if (bytes == null) {
                    pending.addLast(PdaResponse.Error(PdaCmd.PUT_END, PdaErrorCode.BAD_STATE).encode())
                    return
                }
                if (badCrc > 0 || bytes.size.toLong() != putTotal || crc32(bytes) != putCrc) {
                    if (badCrc > 0) badCrc--
                    pending.addLast(PdaResponse.Error(PdaCmd.PUT_END, PdaErrorCode.BAD_CRC).encode())
                    return
                }
                if (putKind == BlobKind.FEED) {
                    feed = bytes
                    feedVersion = putId
                } else {
                    details += Json.parseObject(bytes.toString(Charsets.UTF_8))
                }
                pending.addLast(PdaResponse.PutOk(putKind, putId).encode())
            }
            PdaCommand.Bye -> pending.addLast(PdaResponse.ByeOk.encode())
        }
    }

    override suspend fun receive(timeoutMs: Long): ByteArray {
        checkLink()
        val f = pending.removeFirstOrNull() ?: throw LinkException("timeout waiting for the PDA")
        if (f === DROP) {
            connected = false
            pending.clear()
            putBuf = null
            throw LinkException("disconnected")
        }
        return f
    }

    private fun checkLink() {
        if (!connected) throw LinkException("not connected")
    }

    private fun describe(c: PdaCommand) = when (c) {
        is PdaCommand.Time -> "TIME"
        is PdaCommand.ObList -> "OB_LIST ${c.afterId}"
        is PdaCommand.ObAck -> "OB_ACK ${c.actionId}=${c.result}"
        is PdaCommand.PutBegin -> "PUT ${c.kind}"
        is PdaCommand.PutData -> "PUT_DATA"
        PdaCommand.PutEnd -> "PUT_END"
        PdaCommand.Bye -> "BYE"
    }

    companion object {
        private val DROP = ByteArray(0)
    }
}

/** Fictional Athena data (like server/scripts/seed_demo.py), answers overridable. */
class FakeConvoApi(private val timeline: Timeline = Timeline()) : ConvoApi {
    var healthAnswer: ServerResult = ServerResult.Ok()
    var patchAnswer: (Long, String) -> ServerResult = { _, _ -> ServerResult.Ok() }
    var digestAnswer: ServerResult = ServerResult.Ok()
    var conversationsFail: ServerResult? = null
    val patches = mutableListOf<Pair<Long, String>>()
    val digestPosts = mutableListOf<String>()
    var getCalls = 0

    val digests = mutableMapOf(
        "2026-10-19" to mapOf<String, Any?>(
            "date" to "2026-10-19", "headline" to "Planning day", "summary" to "Anna and you planned the garden party.",
            "highlights" to listOf("Garden party on Saturday", "Order the lanterns"),
        ),
    )
    val conversationList = mutableListOf<Map<String, Any?>>(
        conv("c1", "Garden party", "2026-10-19T15:00:00+00:00", "Anna and you planned the party."),
        conv("c2", "Bike repair", "2026-10-19T09:30:00+00:00", "Ben fixes the bike on Friday."),
    )
    val items = mutableListOf<Map<String, Any?>>(
        mapOf("id" to 42L, "text" to "Order the lanterns", "owner" to "me", "due" to "Friday", "due_date" to "2026-10-23",
            "conversation_id" to "c1", "status" to "open"),
        mapOf("id" to 43L, "text" to "Call Ben", "owner" to null, "due" to null, "due_date" to null, "conversation_id" to "c2", "status" to "open"),
    )
    val transcripts = mutableMapOf<String, Map<String, Any?>>()

    override suspend fun health(): ServerResult {
        timeline.add("server:health")
        return healthAnswer
    }

    override suspend fun digest(date: String): ApiResult<Map<String, Any?>> {
        getCalls++
        return digests[date]?.let { ApiResult.Ok(it) } ?: ApiResult.Fail(ServerResult.Rejected(404, "no digest for that date"))
    }

    override suspend fun conversations(sinceEpochS: Long, limit: Int): ApiResult<List<Map<String, Any?>>> {
        getCalls++
        conversationsFail?.let { return ApiResult.Fail(it) }
        return ApiResult.Ok(conversationList.take(limit))
    }

    override suspend fun openActionItems(): ApiResult<List<Map<String, Any?>>> {
        getCalls++
        return ApiResult.Ok(items.filter { it["status"] == "open" })
    }

    override suspend fun conversation(id: String): ApiResult<Map<String, Any?>> {
        timeline.add("server:conversation $id")
        val c = conversationList.firstOrNull { it["id"] == id } ?: return ApiResult.Fail(ServerResult.Rejected(404, "conversation not found"))
        return ApiResult.Ok(c + mapOf("action_items" to items.filter { it["conversation_id"] == id }))
    }

    override suspend fun transcript(id: String): ApiResult<Map<String, Any?>> {
        timeline.add("server:transcript $id")
        return transcripts[id]?.let { ApiResult.Ok(it) } ?: ApiResult.Fail(ServerResult.Rejected(404, "conversation not found"))
    }

    override suspend fun setActionItemStatus(id: Long, status: String): ServerResult {
        timeline.add("server:patch $id $status")
        patches += id to status
        val r = patchAnswer(id, status)
        if (r is ServerResult.Ok) {
            val i = items.indexOfFirst { it["id"] == id }
            if (i >= 0) items[i] = items[i] + ("status" to status)
        }
        return r
    }

    override suspend fun makeDigest(date: String): ServerResult {
        timeline.add("server:digest $date")
        digestPosts += date
        if (digestAnswer is ServerResult.Ok) {
            digests[date] = mapOf("date" to date, "summary" to "Fresh digest for $date", "highlights" to listOf("New"))
        }
        return digestAnswer
    }

    companion object {
        fun conv(id: String, title: String, at: String, summary: String) = mapOf<String, Any?>(
            "id" to id, "title" to title, "started_at" to at, "people" to listOf("Anna"),
            "summary" to summary, "key_points" to listOf("Point one", "Point two"), "status" to "summarized",
        )
    }
}

class FakeCalendar(private val timeline: Timeline = Timeline()) : CalendarApi {
    val events = mutableListOf(
        CalEvent("e1", "Dentist", "2026-10-21T08:00:00Z", "2026-10-21T08:30:00Z", "Main St", false),
        CalEvent("e2", "Holiday", "2026-10-24", "2026-10-25", "", true),
    )
    val created = mutableListOf<NewEvent>()
    val deleted = mutableListOf<String>()
    var listAnswer: ApiResult<List<CalEvent>>? = null
    var createAnswer: ApiResult<String>? = null
    var deleteAnswer: ApiResult<Unit>? = null
    val ranges = mutableListOf<Pair<String, String>>()

    override suspend fun listEvents(fromIso: String, toIso: String): ApiResult<List<CalEvent>> {
        ranges += fromIso to toIso
        return listAnswer ?: ApiResult.Ok(events.toList())
    }

    override suspend fun createEvent(event: NewEvent): ApiResult<String> {
        timeline.add("odysseus:create ${event.title}")
        createAnswer?.let { return it }
        created += event
        val uid = "u${created.size}"
        events += CalEvent(uid, event.title, event.start, event.end ?: event.start, "", event.allDay)
        return ApiResult.Ok(uid)
    }

    override suspend fun deleteEvent(uid: String): ApiResult<Unit> {
        timeline.add("odysseus:delete $uid")
        deleteAnswer?.let { return it }
        deleted += uid
        return if (events.removeIf { it.id == uid }) ApiResult.Ok(Unit) else ApiResult.Fail(ServerResult.Rejected(404, "Event not found"))
    }
}

/** Fictional Odysseus checklists (plan §4.4). */
class FakeTodos(private val timeline: Timeline = Timeline()) : TodosApi {
    val notes = mutableListOf(
        TodoNote("n1", "Groceries", "checklist", true, "2026-10-24T00:00:00",
            listOf(TodoItem("Milk", false), TodoItem("Bread", true), TodoItem("Eggs  (free range)", false))),
        TodoNote("n2", "", "checklist", false, null, listOf(TodoItem("Call the plumber", false))),
        TodoNote("n3", "Ideas", "note", false, null, emptyList()),
        TodoNote("n4", "Empty list", "checklist", false, null, emptyList()),
    )
    var notesAnswer: ApiResult<List<TodoNote>>? = null
    /** Answer for every toggle; null = exit_code 0 and the item changes. */
    var toggleAnswer: ApiResult<Boolean>? = null
    val toggles = mutableListOf<Triple<String, Int, Boolean>>()
    var reads = 0

    override suspend fun notes(): ApiResult<List<TodoNote>> {
        reads++
        timeline.add("odysseus:todos")
        return notesAnswer ?: ApiResult.Ok(notes.toList())
    }

    override suspend fun toggle(noteId: String, index: Int, done: Boolean): ApiResult<Boolean> {
        timeline.add("odysseus:toggle $noteId/$index=$done")
        toggleAnswer?.let { return it }
        toggles += Triple(noteId, index, done)
        val k = notes.indexOfFirst { it.id == noteId }
        notes[k] = notes[k].copy(items = notes[k].items.toMutableList().also { it[index] = it[index].copy(done = done) })
        return ApiResult.Ok(true)
    }
}

/** A fictional Today insight (plan §4.1) whose refs point at [FakeTodos] and [FakeCalendar]. */
class FakeInsight : InsightApi {
    var answer: ApiResult<Map<String, Any?>> = ApiResult.Ok(demo())
    var reads = 0

    /** The weights route (plan §5); by default a hub without it. */
    var weightsAnswer: ApiResult<Map<String, Any?>> = ApiResult.Unsupported("this Odysseus has no weights route (update the hub)")
    var weightReads = 0

    override suspend fun today(): ApiResult<Map<String, Any?>> {
        reads++
        return answer
    }

    override suspend fun weights(): ApiResult<Map<String, Any?>> {
        weightReads++
        return weightsAnswer
    }

    companion object {
        fun item(ref: Any?, why: Any?, title: String = "x") = linkedMapOf("ref" to ref, "why" to why, "title" to title)

        fun demo(): MutableMap<String, Any?> = linkedMapOf(
            "date" to "2026-10-20",
            "generated" to "2026-10-20T05:30:00Z",
            "headline" to "A light day: the dentist in the morning, then the groceries.",
            "focus" to listOf(item("todo:n1:0", "Needed for breakfast tomorrow", "Milk"),
                item("event:e1", "Bring the referral letter", "Dentist")),
            "coming_up" to listOf(item("event:e2", "Book the train", "Holiday")),
            "also" to listOf(item("todo:n2:0", "Before the weekend", "Call the plumber")),
        )
    }
}
