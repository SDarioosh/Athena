package org.athena.relay.relay

import kotlinx.coroutines.runBlocking
import org.athena.relay.protocol.AckResult
import org.athena.relay.protocol.PdaCommand
import org.athena.relay.protocol.PdaStatus
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.ZoneId

class PdaSyncTest {
    private val timeline = Timeline()
    private val clock = TestClock()
    private val pda = FakePda(timeline, clock)
    private val server = FakeConvoApi(timeline)
    private val cal = FakeCalendar(timeline)
    private val memory = MemoryPdaMemory()
    private val zone = ZoneId.of("Europe/Berlin")
    private val logs = mutableListOf<String>()

    private fun sync(calendar: CalendarApi = cal) = PdaSync(pda, server, calendar, memory, "AA:BB", clock, zone,
        templates = listOf("Call", "Meeting"), log = { level, msg -> logs += "$level $msg" })

    private fun run(calendar: CalendarApi = cal): PdaOutcome = runBlocking { sync(calendar).run() }

    @After fun noBusy() {
        assertEquals("relay wrote a command while another was running", 0, pda.busyErrors)
    }

    private fun idx(e: String) = timeline.events.indexOf(e).also { assertTrue("missing $e in ${timeline.events}", it >= 0) }

    @Test fun firstSyncSendsTimeAndFeed() {
        val out = run()
        assertTrue(out is PdaOutcome.Done)
        assertEquals(1, out.stats.feedsPushed)
        assertEquals(PdaCommand.Time(clock.ms, 120), pda.timeSet) // Berlin, summer time on 2026-10-20
        val f = pda.feedJson()
        assertEquals(1L, f["v"])
        assertEquals(pda.feedVersion, f["version"])
        assertEquals("Europe/Berlin", f["tz"])
        assertEquals(listOf("c1", "c2"), (f["conversations"] as List<*>).map { (it as Map<*, *>)["id"] })
        assertEquals(2, (f["actions"] as List<*>).size)
        assertEquals(2, (f["calendar"] as List<*>).size)
        assertEquals(listOf("Call", "Meeting"), f["templates"])
        assertEquals("pda:BYE", timeline.events.last())
        // Order: TIME, health, outbox, then the feed.
        assertTrue(idx("pda:TIME") < idx("server:health"))
        assertTrue(idx("server:health") < idx("pda:OB_LIST 0"))
        assertTrue(idx("pda:OB_LIST 0") < idx("pda:PUT 1"))
    }

    @Test fun unchangedFeedIsNotSentAgain() {
        run()
        val v = pda.feedVersion
        pda.reconnect()
        val out = run()
        assertEquals(0, out.stats.feedsPushed)
        assertEquals(v, pda.feedVersion)
        // A change on the server: sent again, with a newer version.
        server.items.removeAt(1)
        clock.ms += 60_000
        pda.reconnect()
        assertEquals(1, run().stats.feedsPushed)
        assertTrue(pda.feedVersion > v)
    }

    @Test fun aT5ThatLostItsFeedGetsItAgain() {
        run()
        pda.feed = null
        pda.feedVersion = 0
        pda.reconnect()
        assertEquals(1, run().stats.feedsPushed)
        assertNotNull(pda.feed)
    }

    @Test fun serverDownLeavesTheT5Alone() {
        pda.queue("""{"t":"item_status","id":42,"status":"done"}""")
        server.healthAnswer = ServerResult.Transient("network error")
        val out = run()
        assertTrue(out is PdaOutcome.ServerUnavailable)
        assertNull(pda.feed)
        assertEquals(1, pda.outbox.size)
        assertTrue(pda.commands.none { it is PdaCommand.ObList || it is PdaCommand.PutBegin })
        assertEquals("pda:BYE", timeline.events.last())
    }

    @Test fun itemStatusIsAckedOnlyAfterTheServerAnswered() {
        val e = pda.queue("""{"t":"item_status","id":42,"status":"done"}""")
        val out = run()
        assertEquals(1, out.stats.actionsDone)
        assertTrue(idx("server:patch 42 done") < idx("pda:OB_ACK ${e.actionId}=0"))
        assertTrue(pda.outbox.isEmpty())
        // The feed sent afterwards no longer lists the item.
        assertEquals(listOf(43L), (pda.feedJson()["actions"] as List<*>).map { (it as Map<*, *>)["id"] })
    }

    @Test fun finalRejectionIsAckedWithResult1() {
        val e = pda.queue("""{"t":"item_status","id":999,"status":"done"}""")
        server.patchAnswer = { _, _ -> ServerResult.Rejected(404, "action item not found") }
        val out = run()
        assertEquals(listOf(e.actionId to AckResult.REJECTED), pda.acks)
        assertEquals(1, out.stats.actionsRejected)
    }

    @Test fun temporaryFailureIsNeverAcked() {
        pda.queue("""{"t":"item_status","id":42,"status":"done"}""")
        server.patchAnswer = { _, _ -> ServerResult.Transient("HTTP 503") }
        val out = run()
        assertTrue(out is PdaOutcome.ServerUnavailable)
        assertTrue(pda.acks.isEmpty())
        assertEquals(1, pda.outbox.size)
        assertNull("no feed after a failed outbox", pda.feed)
    }

    @Test fun badActionsAreRejectedWithoutCallingAServer() {
        val bad = listOf(
            """not json""",
            """{"t":"item_status","id":"x","status":"done"}""",
            """{"t":"item_status","id":42,"status":"deleted"}""",
            """{"t":"launch_missiles"}""",
            """{"t":"cal_add","start":"2026-10-21T09:00"}""",
            """{"t":"fetch","ref":"conv:../../etc"}""",
        )
        bad.forEach { pda.queue(it) }
        val out = run()
        assertEquals(bad.size, out.stats.actionsRejected)
        assertTrue(pda.acks.all { it.second == AckResult.REJECTED })
        assertTrue(server.patches.isEmpty() && cal.created.isEmpty())
    }

    @Test fun outboxIsPagedInOrder() {
        repeat(20) { pda.queue("""{"t":"item_status","id":42,"status":"done"}""") }
        run()
        assertEquals((1L..20L).toList(), pda.acks.map { it.first })
        val lists = pda.commands.filterIsInstance<PdaCommand.ObList>().map { it.afterId }
        assertEquals("continues only while remaining > 0", listOf(0L, 16L), lists)
    }

    @Test fun calAddCreatesTheEventAsSent() {
        pda.queue("""{"t":"cal_add","title":"Order the lanterns","start":"2026-10-23T09:00:00+02:00","end":"2026-10-23T09:30:00+02:00","from_item":42}""")
        val out = run()
        assertEquals(1, out.stats.actionsDone)
        val ev = cal.created.single()
        assertEquals("2026-10-23T09:00:00+02:00", ev.start)
        assertEquals("2026-10-23T09:30:00+02:00", ev.end)
        assertTrue(ev.description.contains("#42"))
        // The new event is in the feed sent after the outbox, in UTC.
        val e = (pda.feedJson()["calendar"] as List<*>).map { it as Map<*, *> }.single { it["title"] == "Order the lanterns" }
        assertEquals("2026-10-23T07:00:00Z", e["start"])
    }

    @Test fun calAddAfterALostAckDoesNotCreateASecondEvent() {
        val e = pda.queue("""{"t":"cal_add","title":"Call Ben","start":"2026-10-22","all_day":true}""")
        pda.dropAfterAck += e.actionId
        // The T5 applied the ack but the answer was lost: simulate the T5 still having it.
        val out1 = run()
        assertTrue(out1 is PdaOutcome.LinkLost)
        assertEquals(1, cal.created.size)
        pda.outbox[e.actionId] = e // the ack didn't reach the T5's flash
        pda.reconnect()
        val out2 = run()
        assertTrue(out2 is PdaOutcome.Done)
        assertEquals("no second event", 1, cal.created.size)
        assertEquals(e.actionId to AckResult.DONE, pda.acks.last())
        assertTrue(pda.outbox.isEmpty())
    }

    @Test fun reusedActionIdWithOtherContentIsNotDeduped() {
        val e = pda.queue("""{"t":"cal_add","title":"A","start":"2026-10-22","all_day":true}""", id = 5)
        run()
        pda.reconnect()
        pda.queue("""{"t":"cal_add","title":"B","start":"2026-10-22","all_day":true}""", createdMs = e.createdMs + 1, id = 5)
        run()
        assertEquals(listOf("A", "B"), cal.created.map { it.title })
    }

    @Test fun odysseusDownStopsTheSyncAtThatAction() {
        val add = pda.queue("""{"t":"cal_add","title":"Call","start":"2026-10-22T10:00:00+02:00","end":"2026-10-22T10:15:00+02:00"}""")
        pda.queue("""{"t":"item_status","id":42,"status":"done"}""")
        cal.createAnswer = ApiResult.Fail(ServerResult.Transient("Odysseus not reachable (network error)"))
        val out = run()
        assertTrue(out is PdaOutcome.ServerUnavailable)
        assertTrue("nothing after it is acked", pda.acks.isEmpty())
        assertTrue(server.patches.isEmpty())
        assertNull("no feed", pda.feed)
        assertEquals(2, pda.outbox.size)
        assertNotNull(pda.outbox[add.actionId])
        // A rejected Odysseus token stops it too, but isn't the server's token.
        cal.createAnswer = ApiResult.Fail(ServerResult.AuthRejected(403))
        pda.reconnect()
        assertTrue(run() is PdaOutcome.ServerUnavailable)
    }

    @Test fun calendarListFailureFailsTheFeed() {
        cal.listAnswer = ApiResult.Fail(ServerResult.Transient("HTTP 502"))
        assertTrue(run() is PdaOutcome.ServerUnavailable)
        assertNull(pda.feed)
    }

    @Test fun withoutOdysseusTheCalendarIsEmptyAndCalendarActionsWait() {
        pda.queue("""{"t":"cal_add","title":"Call","start":"2026-10-22T10:00:00+02:00","end":"2026-10-22T10:15:00+02:00"}""")
        pda.queue("""{"t":"cal_delete","id":"e1"}""")
        val item = pda.queue("""{"t":"item_status","id":42,"status":"done"}""")
        val out = run(NoCalendar)
        assertTrue(out is PdaOutcome.Done)
        assertEquals(emptyList<Any?>(), pda.feedJson()["calendar"])
        assertEquals(2, out.stats.actionsWaiting)
        assertEquals(listOf(item.actionId to AckResult.DONE), pda.acks)
        assertTrue(out.stats.calendarNote!!.contains("isn't set up"))
    }

    @Test fun calDeleteOfAGoneEventIsDone() {
        pda.queue("""{"t":"cal_delete","id":"e1"}""")
        pda.queue("""{"t":"cal_delete","id":"nope"}""")
        val out = run()
        assertEquals(2, out.stats.actionsDone)
        assertEquals(listOf("e1", "nope"), cal.deleted)
    }

    @Test fun fetchIsAnsweredAfterTheFeedWithADetailBlob() {
        val e = pda.queue("""{"t":"fetch","ref":"conv:c1"}""")
        val out = run()
        assertEquals(1, out.stats.detailsPushed)
        val d = pda.details.single()
        assertEquals("conv:c1", d["ref"])
        assertEquals("Garden party", d["title"])
        assertEquals(listOf(mapOf("id" to 42L, "text" to "Order the lanterns", "status" to "open", "due" to "2026-10-23")),
            d["actions"])
        // Plan §3.5: server call in outbox order, then the feed, then PUT, then the ack.
        assertTrue(idx("server:conversation c1") < idx("pda:PUT 1"))
        assertTrue(idx("pda:PUT 1") < idx("pda:PUT 2"))
        assertTrue(idx("pda:PUT 2") < idx("pda:OB_ACK ${e.actionId}=0"))
        // A detail's blob id is the fetch action's id; a feed's is its version.
        val puts = pda.commands.filterIsInstance<PdaCommand.PutBegin>()
        assertEquals(e.actionId, puts.single { it.kind == 2 }.blobId)
        assertEquals(pda.feedVersion, puts.single { it.kind == 1 }.blobId)
    }

    @Test fun aServerErrorOnAFetchStopsBeforeTheFeed() {
        pda.queue("""{"t":"fetch","ref":"transcript:c1"}""")
        server.transcripts["c1"] = emptyMap() // unused: the call fails first
        val api = object : ConvoApi by server {
            override suspend fun transcript(id: String): ApiResult<Map<String, Any?>> =
                ApiResult.Fail(ServerResult.Transient("HTTP 503"))
        }
        val out = runBlocking { PdaSync(pda, api, cal, memory, "AA:BB", clock, zone).run() }
        assertTrue(out is PdaOutcome.ServerUnavailable)
        assertNull(pda.feed)
        assertTrue(pda.acks.isEmpty())
    }

    @Test fun fetchOfAMissingConversationIsRejected() {
        pda.queue("""{"t":"fetch","ref":"transcript:zzz"}""")
        val out = run()
        assertEquals(1, out.stats.actionsRejected)
        assertTrue(pda.details.isEmpty())
    }

    @Test fun digestRefreshMakesTheDigestAndForcesAFeed() {
        run()
        pda.reconnect()
        pda.queue("""{"t":"digest_refresh","date":"2026-10-20"}""")
        val out = run()
        assertEquals(listOf("2026-10-20"), server.digestPosts)
        assertEquals(1, out.stats.feedsPushed)
        assertEquals("2026-10-20", (pda.feedJson()["briefing"] as Map<*, *>)["date"])
    }

    @Test fun badCrcIsRetriedThenGivesUp() {
        pda.badCrc = 1
        assertEquals(1, run().stats.feedsPushed)
        assertEquals(2, pda.commands.count { it is PdaCommand.PutBegin })
        pda.reconnect()
        pda.feedVersion = 0
        pda.badCrc = PdaSync.MAX_PUT_TRIES
        assertTrue(run() is PdaOutcome.DeviceError)
    }

    @Test fun putFramesFitTheMtu() {
        pda.mtu = 420
        server.conversationList.clear()
        repeat(20) { i -> server.conversationList += FakeConvoApi.conv("c$i", "Talk $i", "2026-10-19T1${i % 10}:00:00Z", "x".repeat(900)) }
        run()
        val data = pda.commands.filterIsInstance<PdaCommand.PutData>()
        assertTrue(data.size > 1)
        assertTrue(data.all { it.payload.size <= 420 - 3 - 5 })
        assertTrue(pda.feed!!.size <= FeedBuilder.MAX_FEED_BYTES)
    }

    @Test fun putWritesNeverExceed512BytesAtMtu517() {
        pda.mtu = 517
        server.conversationList.clear()
        repeat(20) { i -> server.conversationList += FakeConvoApi.conv("c$i", "Talk $i", "2026-10-19T1${i % 10}:00:00Z", "x".repeat(900)) }
        run()
        val data = pda.commands.filterIsInstance<PdaCommand.PutData>()
        assertTrue(data.size > 1)
        assertTrue(data.all { it.encode().size <= 512 })
        assertEquals(507, data.first().payload.size)
    }

    @Test fun smallMtuAndWrongProtocolAreDeviceErrors() {
        pda.mtu = 247
        assertTrue(run() is PdaOutcome.DeviceError)
        pda.mtu = 517
        pda.info = "proto=2;device=t5-pda"
        pda.reconnect()
        assertTrue(run() is PdaOutcome.DeviceError)
    }

    @Test fun authRejectedStops() {
        server.conversationsFail = ServerResult.AuthRejected(401)
        assertEquals(PdaOutcome.AuthRejected(401, PdaStats()), run())
    }

    @Test fun staysWhileTheUserIsActiveAndAnswersNewActions() {
        pda.flags = 0x01 or PdaStatus.FLAG_USER_ACTIVE
        var queued = false
        pda.onAwaitStatus = {
            if (!queued && clock.ms > 1792483200000L + 30_000) {
                queued = true
                pda.queue("""{"t":"item_status","id":43,"status":"done"}""")
                pda.statusScript.addLast(pda.status())
            }
        }
        // After a few minutes the user puts the T5 down.
        pda.statusScript.addAll(listOf(null, null, null))
        val stayer = PdaSync(pda, server, cal, memory, "AA:BB", clock, zone, log = { _, m -> logs += m },
            maxStayMs = 3 * 60_000L)
        val out = runBlocking { stayer.run() }
        assertTrue(out is PdaOutcome.Done)
        assertEquals(listOf(43L to "done"), server.patches)
        assertTrue(pda.outbox.isEmpty())
        // Keepalive: no gap between commands reached the T5's 30 s idle limit.
        assertTrue(pda.commands.count { it is PdaCommand.Time } >= 3)
        assertEquals("pda:BYE", timeline.events.last())
    }

    @Test fun anActionQueuedRightAfterTheSyncIsNotMistakenForTheDrainedOnes() {
        // One action is drained by the first sync; the user then queues one more, so the
        // outbox count is 1 again, as it was before the sync.
        pda.flags = 0x01 or PdaStatus.FLAG_USER_ACTIVE
        pda.queue("""{"t":"item_status","id":42,"status":"done"}""")
        var queued = false
        pda.onAwaitStatus = {
            if (!queued) {
                queued = true
                pda.queue("""{"t":"item_status","id":43,"status":"done"}""")
            }
        }
        val stayer = PdaSync(pda, server, cal, memory, "AA:BB", clock, zone, maxStayMs = 2 * 60_000L)
        assertTrue(runBlocking { stayer.run() } is PdaOutcome.Done)
        assertEquals(listOf(42L to "done", 43L to "done"), server.patches)
        assertTrue(pda.outbox.isEmpty())
    }

    @Test fun actionsLeftWaitingDoNotResyncEveryStatus() {
        pda.flags = 0x01 or PdaStatus.FLAG_USER_ACTIVE
        pda.queue("""{"t":"cal_add","title":"Call","start":"2026-10-22","all_day":true}""")
        val stayer = PdaSync(pda, server, NoCalendar, memory, "AA:BB", clock, zone, maxStayMs = 2 * 60_000L)
        assertTrue(runBlocking { stayer.run() } is PdaOutcome.Done)
        assertEquals("one OB_LIST: the waiting action isn't new", 1, pda.commands.count { it is PdaCommand.ObList })
    }

    @Test fun outboxListThatDoesNotAdvanceIsADeviceError() {
        // A firmware that ignores after_id, with actions that stay queued (no Odysseus).
        repeat(17) { pda.queue("""{"t":"cal_add","title":"Call $it","start":"2026-10-22","all_day":true}""") }
        pda.listFrom = { 0L }
        val out = run(NoCalendar)
        assertTrue(out.toString(), out is PdaOutcome.DeviceError)
        assertEquals(2, pda.commands.count { it is PdaCommand.ObList })
        assertEquals("pda:BYE", timeline.events.last())
    }

    @Test fun feedVersionStaysAU32() {
        // A T5 reporting the largest u32: the next version must not wrap to 0 ("no feed").
        pda.feedVersion = 0xFFFFFFFFL
        assertTrue(run() is PdaOutcome.Done)
        assertEquals(0xFFFFFFFFL, pda.feedVersion)
        assertEquals(0xFFFFFFFFL, pda.feedJson()["version"])
    }

    @Test fun staysOnlyUntilTheUserLeaves() {
        pda.flags = 0x01 or PdaStatus.FLAG_USER_ACTIVE
        pda.statusScript.addLast(pda.status().copy(flags = 0x01))
        val out = run()
        assertTrue(out is PdaOutcome.Done)
        assertTrue(clock.ms - 1792483200000L < 10_000)
    }

    @Test fun logsHoldNoContent() {
        pda.queue("""{"t":"cal_add","title":"Secret meeting","start":"2026-10-22","all_day":true}""")
        run()
        assertTrue(logs.none { it.contains("Secret") || it.contains("Garden") || it.contains("Anna") })
    }
}
