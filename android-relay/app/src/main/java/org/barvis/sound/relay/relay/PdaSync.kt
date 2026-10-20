package org.barvis.sound.relay.relay

import org.barvis.sound.relay.protocol.AckResult
import org.barvis.sound.relay.protocol.BlobKind
import org.barvis.sound.relay.protocol.OutboxEntry
import org.barvis.sound.relay.protocol.PdaCommand
import org.barvis.sound.relay.protocol.PdaErrorCode
import org.barvis.sound.relay.protocol.PdaGatt
import org.barvis.sound.relay.protocol.PdaResponse
import org.barvis.sound.relay.protocol.PdaStatus
import org.barvis.sound.relay.protocol.ProtocolException
import org.barvis.sound.relay.protocol.WatchInfo
import org.barvis.sound.relay.protocol.crc32
import java.time.ZoneId

/** Counters of one PDA connection. */
data class PdaStats(
    val actionsDone: Int = 0,
    val actionsRejected: Int = 0,
    /** Actions left on the T5 for a later sync (a server was unavailable or isn't set up). */
    val actionsWaiting: Int = 0,
    val feedsPushed: Int = 0,
    val detailsPushed: Int = 0,
    /** Why the calendar is missing, stale, or `cal_add` waits; null if all is well. */
    val calendarNote: String? = null,
    /** Why the T5 gets no checklists, or `todo_toggle` waits (plan §4.4); null if all is well. */
    val todosNote: String? = null,
)

/** How one PDA connection ended. */
sealed class PdaOutcome {
    abstract val stats: PdaStats

    data class Done(override val stats: PdaStats) : PdaOutcome()

    /** odysseus-sound unreachable (or 5xx): the T5 keeps its feed; back off (§3.4 step 3). */
    data class ServerUnavailable(val detail: String, override val stats: PdaStats) : PdaOutcome()

    /** odysseus-sound rejected the token. */
    data class AuthRejected(val code: Int, override val stats: PdaStats) : PdaOutcome()

    /** The T5 speaks another protocol, has a too small MTU, or answered something unusable. */
    data class DeviceError(val detail: String, override val stats: PdaStats) : PdaOutcome()

    data class LinkLost(val detail: String, override val stats: PdaStats) : PdaOutcome()
}

/** Live progress for the status screen. All callbacks are optional. Never gets content. */
interface PdaListener {
    fun onInfo(info: WatchInfo) {}
    fun onStatus(status: PdaStatus) {}
    fun onSyncing() {}
    fun onFeedPushed(version: Long, bytes: Int) {}
    fun onAction(type: String, result: String) {}
}

/**
 * One connection's work with the T5 PDA, docs/t5-pda-plan.md §3.4 steps 2-6, over an already
 * set-up [PdaLink]. Pure Kotlin so it runs in JVM unit tests.
 *
 * Delivery rule (as the watch's §1): an action is `OB_ACK`ed only after the server answered;
 * result 0 on success, 1 when it was rejected for good, and no ack for a temporary failure.
 * `cal_add` is deduped by action (the hub's create isn't idempotent).
 */
class PdaSync(
    private val link: PdaLink,
    private val api: ConvoApi,
    private val calendar: CalendarApi,
    private val memory: PdaMemory,
    /** Identifies this T5 in [memory] (the app uses its Bluetooth address). */
    private val deviceKey: String,
    private val clock: Clock = Clock.SYSTEM,
    private val zone: ZoneId = ZoneId.systemDefault(),
    private val templates: List<String> = emptyList(),
    private val log: RelayLog = RelayLog.NONE,
    private val listener: PdaListener = object : PdaListener {},
    private val receiveTimeoutMs: Long = 10_000,
    /** How long to stay connected while the user is active (then `BYE` so the watch gets a turn); 0 = never. */
    private val maxStayMs: Long = 10 * 60_000L,
    private val feedRefreshMs: Long = 5 * 60_000L,
    /** The T5, like the watch, drops a link that sent no command for 30 s. */
    private val keepaliveMs: Long = 20_000L,
    /** The owner's Odysseus checklists (plan §4.4). */
    private val todos: TodosApi = NoTodos,
) {
    private var done = 0
    private var rejected = 0
    private var waiting = 0
    private var feeds = 0
    private var details = 0
    private var calendarNote: String? = null
    private var todosNote: String? = null
    private var todoNotes: ApiResult<List<TodoNote>>? = null
    private var status: PdaStatus? = null
    private var t5FeedVersion = 0L
    private var lastCommandMs = 0L
    private val feedBuilder = FeedBuilder(api, calendar, clock, zone, templates, log = log, todos = todos)
    private val detailBuilder = DetailBuilder(api)

    private class DeviceProblem(message: String) : Exception(message)
    private class ServerStop(val outcome: (PdaStats) -> PdaOutcome) : Exception()

    private fun stats() = PdaStats(done, rejected, waiting, feeds, details, calendarNote, todosNote)

    suspend fun run(): PdaOutcome = try {
        runSteps()
    } catch (e: LinkException) {
        log.log(Level.WARN, "PDA link lost: ${e.message}")
        PdaOutcome.LinkLost(e.message ?: "link lost", stats())
    } catch (e: ServerStop) {
        bye()
        e.outcome(stats())
    } catch (e: DeviceProblem) {
        log.log(Level.ERROR, "PDA: ${e.message}")
        bye()
        PdaOutcome.DeviceError(e.message ?: "protocol error", stats())
    } catch (e: ProtocolException) {
        log.log(Level.ERROR, "bad frame from the PDA: ${e.message}")
        bye()
        PdaOutcome.DeviceError(e.message ?: "bad frame", stats())
    }

    private suspend fun runSteps(): PdaOutcome {
        // Step 2: info, status, TIME.
        val info = WatchInfo.parse(link.readInfo())
        if (info.proto != PdaGatt.PROTO_VERSION) {
            throw DeviceProblem("PDA speaks protocol ${info.values["proto"]}, this app speaks 1")
        }
        listener.onInfo(info)
        log.log(Level.INFO, "PDA ${info.device ?: "?"} fw ${info.fw ?: "?"}, MTU ${link.mtu}")
        if (link.mtu < PdaGatt.MIN_MTU) throw DeviceProblem("MTU ${link.mtu} is below ${PdaGatt.MIN_MTU}")
        readStatus()
        sendTime()

        // Step 3: nothing changes while the server is away; the T5 keeps its feed.
        when (val h = api.health()) {
            is ServerResult.Ok -> Unit
            is ServerResult.Transient -> throw ServerStop { PdaOutcome.ServerUnavailable(h.detail, it) }
            is ServerResult.AuthRejected -> throw ServerStop { PdaOutcome.ServerUnavailable("health: HTTP ${h.code}", it) }
            is ServerResult.Rejected -> throw ServerStop { PdaOutcome.ServerUnavailable("health: HTTP ${h.code}", it) }
        }
        listener.onSyncing()

        // Steps 4 and 5: outbox first, then the feed, then the details asked for.
        syncOnce(forceFeed = false)

        // Step 6: stay while the user is looking at the T5.
        if (status?.userActive == true && maxStayMs > 0) stay()

        bye()
        val s = stats()
        log.log(Level.INFO, "PDA sync done: ${s.actionsDone} actions done, ${s.actionsRejected} rejected, " +
            "${s.actionsWaiting} waiting, ${s.feedsPushed} feed(s) sent")
        return PdaOutcome.Done(s)
    }

    /**
     * Plan §3.5 order: every action's server call in outbox order (each acked at once, except
     * `fetch`), then the feed, then each fetched detail is `PUT` and acked after its `PUT_OK`.
     * A temporary failure or a rejected token stops the sync there: nothing after it is
     * acked and no feed is built.
     */
    private suspend fun syncOnce(forceFeed: Boolean) {
        todoNotes = null // read the notes again in each sync
        val fetched = ArrayList<Pair<OutboxEntry, ByteArray>>()
        val refreshFeed = processOutbox(fetched) || forceFeed
        pushFeed(refreshFeed)
        for ((entry, blob) in fetched) {
            // A detail's blob id is the fetch action's id (plan §3.5).
            put(BlobKind.DETAIL, entry.actionId, blob)
            details++
            ack(entry, AckResult.DONE, "fetch")
        }
    }

    private suspend fun stay() {
        val until = clock.nowMs() + maxStayMs
        var lastFeedMs = clock.nowMs()
        var seenOutbox = status?.outboxWaiting ?: 0
        log.log(Level.INFO, "PDA in use: staying connected")
        while (clock.nowMs() < until) {
            val raw = link.awaitStatus(keepaliveMs)
            if (raw != null) {
                decodeStatus(raw)
            } else {
                readStatus()
            }
            if (clock.nowMs() - lastCommandMs >= keepaliveMs) sendTime()
            val s = status ?: break
            if (!s.userActive) break
            if (s.outboxWaiting > 0 && s.outboxWaiting != seenOutbox) {
                waiting = 0
                syncOnce(forceFeed = false)
                lastFeedMs = clock.nowMs()
                readStatus()
                seenOutbox = status?.outboxWaiting ?: 0
            } else if (clock.nowMs() - lastFeedMs >= feedRefreshMs) {
                pushFeed(force = false)
                lastFeedMs = clock.nowMs()
            }
            if (s.outboxWaiting == 0) seenOutbox = 0
        }
    }

    // ------------------------------------------------------------------ outbox (§3.4 step 4)

    /** Returns true when an action asks for a new feed (`digest_refresh`). */
    private suspend fun processOutbox(fetched: MutableList<Pair<OutboxEntry, ByteArray>>): Boolean {
        var afterId = 0L
        var refresh = false
        while (true) {
            send(PdaCommand.ObList(afterId, PdaGatt.OB_LIST_MAX))
            val entries = ArrayList<OutboxEntry>()
            var remaining = 0L
            while (true) {
                when (val r = PdaResponse.decode(link.receive(receiveTimeoutMs))) {
                    is PdaResponse.Entry -> entries += r.entry
                    is PdaResponse.ObEnd -> {
                        remaining = r.remaining
                        break
                    }
                    is PdaResponse.Error -> throw DeviceProblem("OB_LIST failed: ${r.code}")
                    else -> log.log(Level.DEBUG, "ignored during OB_LIST: $r")
                }
            }
            for (e in entries.sortedBy { it.actionId }) {
                if (handle(e, fetched)) refresh = true
            }
            // Plan §3.5: continue after the last id listed while `remaining > 0`.
            if (entries.isEmpty() || remaining == 0L) return refresh
            afterId = entries.maxOf { it.actionId }
        }
    }

    private suspend fun handle(e: OutboxEntry, fetched: MutableList<Pair<OutboxEntry, ByteArray>>): Boolean {
        val action = PdaAction.parse(e.json)
        when (action) {
            is PdaAction.Invalid -> {
                log.log(Level.WARN, "PDA $e rejected: ${action.reason}")
                ack(e, AckResult.REJECTED, action.type)
            }
            is PdaAction.ItemStatus -> {
                val r = api.setActionItemStatus(action.id, action.status)
                convoAnswer(e, action, r)
            }
            is PdaAction.DigestRefresh -> {
                val r = api.makeDigest(action.date)
                convoAnswer(e, action, r)
                return r is ServerResult.Ok
            }
            is PdaAction.CalAdd -> calAdd(e, action)
            is PdaAction.TodoToggle -> todoToggle(e, action)
            is PdaAction.CalDelete -> when (val r = calendar.deleteEvent(action.id)) {
                is ApiResult.Ok -> ack(e, AckResult.DONE, action.type)
                is ApiResult.Fail -> {
                    val err = r.error
                    if (err is ServerResult.Rejected && err.code == 404) ack(e, AckResult.DONE, action.type) // gone already
                    else calendarAnswer(e, action, err)
                }
                is ApiResult.Unsupported -> calendarWait(e, action, r.reason)
            }
            is PdaAction.Fetch -> when (val r = detailBuilder.build(action.ref)) {
                is ApiResult.Ok -> fetched += e to r.value // PUT and acked after the feed
                is ApiResult.Fail -> convoAnswer(e, action, r.error) // 404: result 1, no PUT
                is ApiResult.Unsupported -> ack(e, AckResult.REJECTED, action.type)
            }
        }
        return false
    }

    /** odysseus-sound's answer to an action: ack, or stop the sync (no ack) on a temporary failure. */
    private suspend fun convoAnswer(e: OutboxEntry, action: PdaAction, r: ServerResult) {
        when (r) {
            is ServerResult.Ok -> ack(e, AckResult.DONE, action.type)
            is ServerResult.Rejected -> {
                log.log(Level.WARN, "PDA $e (${action.type}): server said HTTP ${r.code}; dropping it")
                ack(e, AckResult.REJECTED, action.type)
            }
            is ServerResult.Transient -> {
                log.log(Level.WARN, "PDA $e (${action.type}): ${r.detail}; retrying later")
                waiting++
                throw ServerStop { PdaOutcome.ServerUnavailable(r.detail, it) }
            }
            is ServerResult.AuthRejected -> {
                waiting++
                throw ServerStop { PdaOutcome.AuthRejected(r.code, it) }
            }
        }
    }

    private suspend fun calAdd(e: OutboxEntry, action: PdaAction.CalAdd) {
        // §4.2: a re-sent action after a lost ack must not create a second event. The key
        // includes created_ms and the JSON so a wiped T5 that reuses action ids isn't deduped.
        val key = "$deviceKey/${e.actionId}/${e.createdMs}/${crc32(e.json)}"
        memory.createdEvent(key)?.let {
            log.log(Level.INFO, "PDA $e (cal_add): event already created, acking again")
            ack(e, AckResult.DONE, action.type)
            return
        }
        when (val r = calendar.createEvent(action.event)) {
            is ApiResult.Ok -> {
                memory.eventCreated(key, r.value, clock.nowMs())
                ack(e, AckResult.DONE, action.type)
            }
            is ApiResult.Fail -> calendarAnswer(e, action, r.error)
            is ApiResult.Unsupported -> calendarWait(e, action, r.reason)
        }
    }

    /**
     * Odysseus's answer. A temporary failure or a rejected Odysseus token stops the sync like
     * the server's (plan §3.5), but as "unavailable" with backoff: it must not mark the
     * odysseus-sound token as rejected.
     */
    private suspend fun calendarAnswer(e: OutboxEntry, action: PdaAction, r: ServerResult) {
        when (r) {
            is ServerResult.Ok -> ack(e, AckResult.DONE, action.type)
            is ServerResult.Rejected -> {
                log.log(Level.WARN, "PDA $e (${action.type}): Odysseus said HTTP ${r.code}; dropping it")
                ack(e, AckResult.REJECTED, action.type)
            }
            is ServerResult.Transient, is ServerResult.AuthRejected -> {
                val why = "Odysseus: " + FeedBuilder.describe(r)
                waiting++
                calendarNote = why
                log.log(Level.WARN, "PDA $e (${action.type}): $why; stopping, retrying later")
                throw ServerStop { PdaOutcome.ServerUnavailable(why, it) }
            }
        }
    }

    /**
     * Plan §4.4 `todo_toggle`: find the item (index `i` if its text still starts with `text`,
     * else the one item that does), skip the call if it is already in the wanted state, else
     * `toggle_item`. The notes are read once per sync.
     */
    private suspend fun todoToggle(e: OutboxEntry, action: PdaAction.TodoToggle) {
        val notes = when (val r = todoNotes ?: todos.notes().also { todoNotes = it }) {
            is ApiResult.Ok -> r.value
            is ApiResult.Fail -> return calendarAnswer(e, action, r.error)
            is ApiResult.Unsupported -> return todosWait(e, action, r.reason)
        }
        val note = notes.firstOrNull { it.id == action.note }
            ?: return ack(e, AckResult.REJECTED, action.type).also { log.log(Level.WARN, "PDA $e (todo_toggle): the note is gone") }
        val want = normalize(action.text)
        val items = note.items
        val index = if (action.index < items.size && normalize(items[action.index].text).startsWith(want)) action.index
            else items.indices.filter { normalize(items[it].text).startsWith(want) }.singleOrNull()
        if (index == null) {
            log.log(Level.WARN, "PDA $e (todo_toggle): the item changed on Odysseus; dropping it")
            return ack(e, AckResult.REJECTED, action.type)
        }
        if (items[index].done == action.done) return ack(e, AckResult.DONE, action.type)
        when (val r = todos.toggle(note.id, index, action.done)) {
            is ApiResult.Ok -> if (r.value) {
                // Keep the cached notes current for a later toggle of the same item in this sync.
                val updated = note.copy(items = items.toMutableList().also { it[index] = it[index].copy(done = action.done) })
                todoNotes = ApiResult.Ok(notes.map { if (it.id == note.id) updated else it })
                ack(e, AckResult.DONE, action.type)
            } else {
                log.log(Level.WARN, "PDA $e (todo_toggle): Odysseus refused it (exit_code 1)")
                ack(e, AckResult.REJECTED, action.type)
            }
            is ApiResult.Fail -> calendarAnswer(e, action, r.error)
            is ApiResult.Unsupported -> todosWait(e, action, r.reason)
        }
    }

    private fun todosWait(e: OutboxEntry, action: PdaAction, why: String) {
        waiting++
        todosNote = why
        log.log(Level.WARN, "PDA $e (${action.type}) stays queued: $why")
        listener.onAction(action.type, "waiting")
    }

    private fun normalize(s: String) = s.trim().split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ")

    private fun calendarWait(e: OutboxEntry, action: PdaAction, why: String) {
        waiting++
        calendarNote = why
        log.log(Level.WARN, "PDA $e (${action.type}) stays queued: $why")
        listener.onAction(action.type, "waiting")
    }

    private suspend fun ack(e: OutboxEntry, result: Int, type: String) {
        send(PdaCommand.ObAck(e.actionId, result))
        when (val r = awaitFinal("OB_ACK")) {
            is PdaResponse.ObAcked -> if (r.actionId != e.actionId) {
                throw DeviceProblem("OB_ACKED for action ${r.actionId}, expected ${e.actionId}")
            }
            // Already gone (acked over an earlier link whose answer was lost).
            is PdaResponse.Error -> if (r.code != PdaErrorCode.NOT_FOUND) throw DeviceProblem("OB_ACK failed: ${r.code}")
            else -> throw DeviceProblem("unexpected answer to OB_ACK: $r")
        }
        if (result == AckResult.DONE) done++ else rejected++
        listener.onAction(type, if (result == AckResult.DONE) "done" else "rejected")
        log.log(Level.INFO, "PDA $e ($type) acked, result $result")
    }

    // ------------------------------------------------------------------ feed and details (step 5)

    private suspend fun pushFeed(force: Boolean) {
        val content = when (val b = feedBuilder.build()) {
            is FeedBuild.Ok -> b.content
            is FeedBuild.Fail -> {
                val err = b.error
                log.log(Level.WARN, "feed not built (${b.what}), the T5 keeps its last one: ${FeedBuilder.describe(err)}")
                throw when {
                    // Only odysseus-sound's token stops the relay; Odysseus's is its own.
                    err is ServerResult.AuthRejected && !b.what.startsWith("Odysseus") -> ServerStop { PdaOutcome.AuthRejected(err.code, it) }
                    else -> ServerStop { PdaOutcome.ServerUnavailable("${b.what}: ${FeedBuilder.describe(err)}", it) }
                }
            }
        }
        feedBuilder.calendarNote?.let { calendarNote = calendarNote ?: it }
        feedBuilder.todosNote?.let { todosNote = todosNote ?: it }
        val lastVersion = memory.lastFeedVersion(deviceKey)
        val unchanged = content.hash == memory.lastFeedHash(deviceKey) && t5FeedVersion == lastVersion && lastVersion != 0L
        if (unchanged && !force) {
            log.log(Level.DEBUG, "feed unchanged")
            return
        }
        val now = clock.nowMs()
        val version = maxOf(now / 1000, lastVersion + 1, t5FeedVersion + 1) and 0xFFFFFFFFL
        val bytes = content.render(version, now)
        put(BlobKind.FEED, version, bytes) // a feed's blob id is its version (plan §3.5)
        memory.feedPushed(deviceKey, content.hash, version)
        t5FeedVersion = version
        feeds++
        listener.onFeedPushed(version, bytes.size)
        log.log(Level.INFO, "feed $version sent (${bytes.size} B)")
    }

    /**
     * §3.3 PUT: BEGIN, DATA frames of min(MTU − 3, 512) bytes (write with response is the flow control),
     * END. On `bad_crc` the blob is sent again, [MAX_PUT_TRIES] times in all.
     */
    private suspend fun put(kind: Int, blobId: Long, bytes: ByteArray) {
        if (bytes.size > PdaGatt.MAX_BLOB) throw DeviceProblem("blob $blobId is ${bytes.size} B, more than ${PdaGatt.MAX_BLOB}")
        val crc = crc32(bytes)
        val chunk = PdaGatt.putPayload(link.mtu)
        for (attempt in 1..MAX_PUT_TRIES) {
            send(PdaCommand.PutBegin(kind, blobId, bytes.size.toLong(), crc))
            when (val r = awaitFinal("PUT_BEGIN")) {
                is PdaResponse.PutReady -> if (r.kind != kind || r.blobId != blobId) {
                    throw DeviceProblem("PUT_READY for ${r.kind}/${r.blobId}, expected $kind/$blobId")
                }
                is PdaResponse.Error -> throw DeviceProblem("PUT_BEGIN of ${bytes.size} B failed: ${r.code}")
                else -> throw DeviceProblem("unexpected answer to PUT_BEGIN: $r")
            }
            var off = 0
            while (off < bytes.size) {
                val n = minOf(chunk, bytes.size - off)
                send(PdaCommand.PutData(off.toLong(), bytes.copyOfRange(off, off + n)))
                off += n
            }
            send(PdaCommand.PutEnd)
            when (val r = awaitFinal("PUT_END")) {
                is PdaResponse.PutOk -> if (r.kind == kind && r.blobId == blobId) return
                    else throw DeviceProblem("PUT_OK for ${r.kind}/${r.blobId}, expected $kind/$blobId")
                is PdaResponse.Error -> if (r.code == PdaErrorCode.BAD_CRC) {
                    log.log(Level.WARN, "PDA: bad CRC on blob $blobId, sending it again ($attempt/$MAX_PUT_TRIES)")
                } else throw DeviceProblem("PUT failed: ${r.code}")
                else -> throw DeviceProblem("unexpected answer to PUT_END: $r")
            }
        }
        throw DeviceProblem("blob $blobId failed its CRC $MAX_PUT_TRIES times")
    }

    // ------------------------------------------------------------------ small commands

    private suspend fun readStatus() {
        try {
            decodeStatus(link.readStatus())
        } catch (e: ProtocolException) {
            log.log(Level.WARN, "PDA status unreadable: ${e.message}")
        }
    }

    private fun decodeStatus(raw: ByteArray) {
        try {
            val s = PdaStatus.decode(raw)
            if (status == null) t5FeedVersion = s.feedVersion
            status = s
            listener.onStatus(s)
        } catch (e: ProtocolException) {
            log.log(Level.DEBUG, "bad PDA status: ${e.message}")
        }
    }

    private suspend fun sendTime() {
        val now = clock.nowMs()
        send(PdaCommand.Time(now, PdaTime.offsetMinutes(now, zone)))
        when (val r = awaitFinal("TIME")) {
            is PdaResponse.TimeOk -> Unit
            is PdaResponse.Error -> when (r.code) {
                PdaErrorCode.BAD_TIME -> log.log(Level.WARN, "PDA refused the phone's clock (bad_time)")
                PdaErrorCode.MTU_TOO_SMALL -> throw DeviceProblem("PDA says the MTU is too small")
                else -> throw DeviceProblem("TIME failed: ${r.code}")
            }
            else -> throw DeviceProblem("unexpected answer to TIME: $r")
        }
    }

    private suspend fun send(c: PdaCommand) {
        link.write(c.encode())
        lastCommandMs = clock.nowMs()
    }

    /** Waits for the final response of a single-response command, skipping stray frames. */
    private suspend fun awaitFinal(what: String): PdaResponse {
        while (true) {
            val r = PdaResponse.decode(link.receive(receiveTimeoutMs))
            if (r is PdaResponse.Entry || r is PdaResponse.ObEnd) {
                log.log(Level.DEBUG, "ignored during $what: $r")
                continue
            }
            return r
        }
    }

    private suspend fun bye() {
        try {
            send(PdaCommand.Bye)
            awaitFinal("BYE")
        } catch (e: LinkException) {
            // The T5 disconnects on its own after BYE or 30 s of silence.
        } catch (e: ProtocolException) {
            // Same: nothing more to do on this link.
        }
    }

    companion object {
        const val MAX_PUT_TRIES = 3
    }
}
