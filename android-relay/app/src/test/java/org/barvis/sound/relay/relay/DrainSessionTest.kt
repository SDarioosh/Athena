package org.barvis.sound.relay.relay

import kotlinx.coroutines.runBlocking
import org.barvis.sound.relay.protocol.Command
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DrainSessionTest {
    private val s1 = "3f9a0c11d2e4b507"
    private val s2 = "00112233445566aa"
    private val timeline = Timeline()
    private val watch = FakeWatch(timeline)
    private val server = FakeUploader(timeline)
    private var now = 1792412000000L
    private val clock = Clock { now }
    private val anchors = MemoryAnchorStore()
    private val partials = PartialReads()
    private val logs = mutableListOf<String>()

    private fun session() = DrainSession(watch, server, clock, anchors, partials,
        log = { level, msg -> logs += "$level $msg" })

    private fun run(): DrainOutcome = runBlocking { session().run() }

    @After fun noBusy() {
        assertEquals("relay wrote a command while another was running", 0, watch.busyErrors)
    }

    private fun idx(e: String) = timeline.events.indexOf(e).also { assertTrue("missing $e in ${timeline.events}", it >= 0) }

    @Test fun drainsEverythingAcksOnlyAfterOkAndPagesList() {
        watch.listCap = 2
        val a = watch.add(s1, 0)
        val b = watch.add(s1, 1, last = true)
        val c = watch.add(s2, 0, size = 61_000)
        val out = run()
        assertEquals(DrainOutcome.Done(uploaded = 3, skipped = 0), out)
        assertTrue(watch.chunks.isEmpty())
        for ((e, key) in listOf(a to "$s1/0", b to "$s1/1", c to "$s2/0")) {
            assertTrue(idx("server:upload $key") < idx("watch:ACK ${e.chunkId}"))
        }
        assertEquals(61_000, server.uploads[2].audio.size)
        assertEquals("twatch-ultra", server.uploads[0].device)
        assertEquals(s1, server.uploads[0].sessionId)
        assertEquals(1792411200000L, server.uploads[0].timestampMs)
        assertEquals(listOf(s1), server.ends) // only s1 had LAST
        assertEquals("watch:BYE", timeline.events.last())
        // LIST paging: 0, then after b (page of 2), then after c, which returns nothing.
        val lists = watch.commands.filterIsInstance<Command.List>().map { it.afterId }
        assertEquals(listOf(0L, b.chunkId, c.chunkId), lists)
    }

    @Test fun noteChunksUploadWithKindNote() {
        watch.add(s1, 0)
        watch.add(s1, 1, last = true)
        watch.add(s2, 0, note = true)
        watch.add(s2, 1, note = true, last = true)
        assertEquals(DrainOutcome.Done(uploaded = 4, skipped = 0), run())
        assertTrue(watch.chunks.isEmpty())
        val kinds = server.uploads.associate { "${it.sessionId}/${it.sequence}" to it.kind }
        assertEquals(mapOf("$s1/0" to null, "$s1/1" to null, "$s2/0" to "note", "$s2/1" to "note"), kinds)
        assertEquals(listOf(s1, s2), server.ends)
    }

    @Test fun uploadsMatchWatchFilesAndFramesFitMtu() {
        val e = watch.add(s1, 0, size = 60_000)
        val data = watch.dataOf(e.chunkId)
        run()
        assertArrayEquals(data, server.uploads.single().audio)
        assertTrue(watch.dataFrameSizes.all { it <= 517 - 3 })
        assertEquals((60_000 + 504) / 505, watch.dataFrameSizes.size) // 505-byte payloads at MTU 517
    }

    @Test fun duplicateCountsAsOk() {
        watch.add(s1, 0)
        server.uploadAnswer = { ServerResult.Ok(duplicate = true) }
        assertEquals(DrainOutcome.Done(1, 0), run())
        assertTrue(watch.chunks.isEmpty())
    }

    @Test fun lastChunkEndsSessionBeforeAck() {
        watch.add(s1, 0)
        val last = watch.add(s1, 1, last = true)
        run()
        val up = idx("server:upload $s1/1")
        val end = idx("server:end $s1")
        val ack = idx("watch:ACK ${last.chunkId}")
        assertTrue(up < end && end < ack)
    }

    @Test fun endFailureLeavesLastChunkOnWatch() {
        val first = watch.add(s1, 0)
        val last = watch.add(s1, 1, last = true)
        server.endAnswer = { ServerResult.Transient("HTTP 503") }
        val out = run()
        assertTrue(out is DrainOutcome.ServerUnavailable)
        assertFalse(first.chunkId in watch.chunks)
        assertTrue(last.chunkId in watch.chunks)
        assertFalse(timeline.events.contains("watch:ACK ${last.chunkId}"))
        assertEquals("watch:BYE", timeline.events.last())
    }

    @Test fun serverErrorStopsWithoutAck() {
        val a = watch.add(s1, 0)
        val b = watch.add(s1, 1)
        val c = watch.add(s1, 2)
        server.uploadAnswer = { if (it.sequence == 1L) ServerResult.Transient("HTTP 502") else ServerResult.Ok() }
        val out = run()
        assertEquals(DrainOutcome.ServerUnavailable("HTTP 502", uploaded = 1), out)
        assertFalse(a.chunkId in watch.chunks)
        assertTrue(b.chunkId in watch.chunks)
        assertTrue(c.chunkId in watch.chunks)
        // Nothing after the failure except BYE.
        assertFalse(watch.commands.any { it is Command.Read && it.chunkId == c.chunkId })
        assertEquals(Command.Bye, watch.commands.last())
    }

    @Test fun networkErrorStopsWithoutAck() {
        val a = watch.add(s1, 0)
        server.uploadAnswer = { ServerResult.Transient("network error: timeout") }
        assertTrue(run() is DrainOutcome.ServerUnavailable)
        assertTrue(a.chunkId in watch.chunks)
        assertFalse(watch.commands.any { it is Command.Ack })
    }

    @Test fun authErrorStops() {
        val a = watch.add(s1, 0)
        watch.add(s1, 1)
        server.uploadAnswer = { ServerResult.AuthRejected(401) }
        assertEquals(DrainOutcome.AuthRejected(401, 0), run())
        assertTrue(a.chunkId in watch.chunks)
        assertEquals(1, server.uploads.size)
        assertFalse(watch.commands.any { it is Command.Ack })
        assertEquals(Command.Bye, watch.commands.last())
    }

    @Test fun rejectedChunkIsSkippedAndDrainContinues() {
        val bad = watch.add(s1, 0)
        val good = watch.add(s1, 1, last = true)
        server.uploadAnswer = { if (it.sequence == 0L) ServerResult.Rejected(422, "could not decode audio") else ServerResult.Ok() }
        assertEquals(DrainOutcome.Done(uploaded = 1, skipped = 1), run())
        assertTrue(bad.chunkId in watch.chunks)
        assertFalse(good.chunkId in watch.chunks)
        assertFalse(timeline.events.contains("watch:ACK ${bad.chunkId}"))
    }

    @Test fun crcMismatchIsReadAgain() {
        val e = watch.add(s1, 0)
        val original = watch.dataOf(e.chunkId).copyOf()
        watch.corruptReads[e.chunkId] = 2
        assertEquals(DrainOutcome.Done(1, 0), run())
        assertEquals(3, watch.commands.count { it is Command.Read && it.chunkId == e.chunkId })
        assertArrayEquals(original, server.uploads.single().audio)
    }

    @Test fun crcMismatchSkipsAfterThreeRereads() {
        val bad = watch.add(s1, 0)
        val good = watch.add(s1, 1)
        watch.corruptReads[bad.chunkId] = 100
        assertEquals(DrainOutcome.Done(uploaded = 1, skipped = 1), run())
        assertEquals(1 + DrainSession.MAX_REREADS, watch.commands.count { it is Command.Read && it.chunkId == bad.chunkId })
        assertEquals(listOf(1L), server.uploads.map { it.sequence })
        assertTrue(bad.chunkId in watch.chunks)
        assertFalse(good.chunkId in watch.chunks)
    }

    @Test fun readResumesWithOffsetAfterDisconnect() {
        val e = watch.add(s1, 0, size = 20_000)
        val original = watch.dataOf(e.chunkId).copyOf()
        watch.disconnectAfterBytes[e.chunkId] = 5000
        val first = run()
        assertTrue(first is DrainOutcome.LinkLost)
        assertTrue(server.uploads.isEmpty())
        assertTrue(e.chunkId in watch.chunks)

        watch.reconnect()
        assertEquals(DrainOutcome.Done(1, 0), run())
        val reads = watch.commands.filterIsInstance<Command.Read>()
        assertEquals(0L, reads[0].offset)
        assertEquals(5050L, reads[1].offset) // 10 full 505-byte frames arrived before the drop
        assertArrayEquals(original, server.uploads.single().audio)
    }

    @Test fun staleResumeBufferIsNotUsedForAnotherChunk() {
        val e = watch.add(s1, 0, size = 4000)
        watch.disconnectAfterBytes[e.chunkId] = 1000
        run()
        watch.chunks.clear() // acked elsewhere meanwhile
        val f = watch.add(s2, 0, size = 4000)
        watch.reconnect()
        assertEquals(DrainOutcome.Done(1, 0), run())
        assertEquals(0L, watch.commands.filterIsInstance<Command.Read>().last { it.chunkId == f.chunkId }.offset)
    }

    @Test fun endRejectedIsSkippedLikeOther4xx() {
        val last = watch.add(s1, 0, last = true)
        val other = watch.add(s2, 0)
        server.endAnswer = { ServerResult.Rejected(404, "session not found") }
        assertEquals(DrainOutcome.Done(uploaded = 1, skipped = 1), run())
        assertTrue(last.chunkId in watch.chunks)
        assertFalse(other.chunkId in watch.chunks)
    }

    @Test fun sdIoErrorStopsTheDrain() {
        val bad = watch.add(s1, 0)
        val next = watch.add(s1, 1)
        watch.ioErrorOnRead += bad.chunkId
        val out = run()
        assertTrue(out.toString(), out is DrainOutcome.WatchError)
        assertTrue(bad.chunkId in watch.chunks && next.chunkId in watch.chunks)
        assertFalse(watch.commands.any { it is Command.Read && it.chunkId == next.chunkId })
        assertTrue(server.uploads.isEmpty())
        assertEquals(Command.Bye, watch.commands.last())
    }

    @Test fun readEndErrorStopsTheDrainAndResumesLater() {
        val e = watch.add(s1, 0, size = 8000)
        val original = watch.dataOf(e.chunkId).copyOf()
        watch.sdFailAfterBytes[e.chunkId] = 2000
        val out = run()
        assertTrue(out.toString(), out is DrainOutcome.WatchError)
        assertEquals(Command.Bye, watch.commands.last())
        assertTrue(server.uploads.isEmpty())
        watch.reconnect()
        assertEquals(DrainOutcome.Done(1, 0), run())
        assertEquals(2020L, watch.commands.filterIsInstance<Command.Read>().last().offset)
        assertArrayEquals(original, server.uploads.single().audio)
    }

    @Test fun resumeSurvivesTimeFixingTheEntry() {
        // The entry's flags/start change when TIME converts it; session/seq/size/CRC don't.
        val e = watch.add(s1, 0, size = 6000, timeUnset = true, fixableOnTime = false, startMs = 1000)
        watch.disconnectAfterBytes[e.chunkId] = 1000
        run()
        watch.chunks.getValue(e.chunkId).fixableOnTime = true // converted on the next TIME
        watch.reconnect()
        assertEquals(DrainOutcome.Done(1, 0), run())
        assertEquals(1010L, watch.commands.filterIsInstance<Command.Read>().last().offset)
        assertEquals(now - 600_000 + 1000, server.uploads.single().timestampMs)
    }

    @Test fun timeIsSentBeforeList() {
        watch.add(s1, 0)
        run()
        val first = watch.commands.first()
        assertEquals(Command.Time(now), first)
        assertTrue(watch.commands.indexOfFirst { it is Command.List } > 0)
        assertEquals(now, watch.clockMs)
        assertTrue(idx("watch:TIME") < idx("server:health"))
        assertTrue(idx("server:health") < idx("watch:LIST 0"))
    }

    @Test fun serverUnreachablePullsNothing() {
        val e = watch.add(s1, 0)
        server.healthAnswer = ServerResult.Transient("network error: connect timed out")
        val out = run()
        assertEquals(DrainOutcome.ServerUnavailable("network error: connect timed out", 0), out)
        assertEquals(listOf(Command.Time(now), Command.Bye), watch.commands)
        assertTrue(server.uploads.isEmpty())
        assertTrue(e.chunkId in watch.chunks)
    }

    @Test fun timeUnsetChunksAreAnchored() {
        // Fixable: recorded this boot, the watch converts it on TIME.
        watch.add(s2, 0, timeUnset = true, fixableOnTime = true, startMs = 5_000)
        // Not fixable (recorded before a reboot): the relay anchors it.
        watch.add(s1, 0, timeUnset = true, startMs = 0)
        watch.add(s1, 1, timeUnset = true, startMs = 30_000)
        run()
        val bySession = server.uploads.associateBy { "${it.sessionId}/${it.sequence}" }
        assertEquals(now - 600_000 + 5_000, bySession.getValue("$s2/0").timestampMs)
        assertEquals(now + 0, bySession.getValue("$s1/0").timestampMs)
        assertEquals(now + 30_000, bySession.getValue("$s1/1").timestampMs)
        assertTrue(logs.any { it.startsWith("WARN") && it.contains("time unset") })
    }

    @Test fun anchorIsKeptAcrossConnections() {
        watch.add(s1, 0, timeUnset = true, startMs = 0)
        val b = watch.add(s1, 1, timeUnset = true, startMs = 30_000)
        server.uploadAnswer = { if (it.sequence == 1L) ServerResult.Transient("HTTP 503") else ServerResult.Ok() }
        val anchor = now
        run()
        now += 3_600_000
        server.uploadAnswer = { ServerResult.Ok() }
        watch.reconnect()
        run()
        assertFalse(b.chunkId in watch.chunks)
        assertEquals(anchor + 30_000, server.uploads.last().timestampMs)
    }

    @Test fun ackOfAlreadyDeletedChunkIsSuccess() {
        val e = watch.add(s1, 0)
        watch.forgetBeforeAck += e.chunkId
        assertEquals(DrainOutcome.Done(1, 0), run())
    }

    @Test fun ackIsIdempotentOnTheFakeWatch() = runBlocking {
        val e = watch.add(s1, 0)
        watch.write(Command.Ack(e.chunkId).encode())
        assertEquals(org.barvis.sound.relay.protocol.Response.Acked(0, e.chunkId),
            org.barvis.sound.relay.protocol.Response.decode(watch.receive(1)))
        watch.write(Command.Ack(e.chunkId).encode())
        assertEquals(org.barvis.sound.relay.protocol.Response.Acked(1, e.chunkId),
            org.barvis.sound.relay.protocol.Response.decode(watch.receive(1)))
    }

    @Test fun fakeWatchEnforcesBusyRule() = runBlocking {
        watch.add(s1, 0)
        watch.write(Command.List(0, 32).encode())
        watch.write(Command.Bye.encode()) // before LIST's answer was read
        assertEquals(1, watch.busyErrors)
        watch.busyErrors = 0 // keep @After happy: this test broke the rule on purpose
    }

    @Test fun chunkGoneBeforeReadIsSkipped() {
        val e = watch.add(s1, 0)
        // Delete it between LIST and READ, as if another relay acked it.
        val gone = object : WatchLink by watch {
            override suspend fun write(command: ByteArray) {
                if (Command.decode(command) is Command.Read) watch.chunks.remove(e.chunkId)
                watch.write(command)
            }
        }
        val out = runBlocking { DrainSession(gone, server, clock).run() }
        assertEquals(DrainOutcome.Done(0, 1), out)
        assertTrue(server.uploads.isEmpty())
    }

    @Test fun otherProtocolVersionIsRefused() {
        watch.info = "proto=2;device=twatch-ultra"
        watch.add(s1, 0)
        val out = run()
        assertTrue(out is DrainOutcome.WatchError)
        assertFalse(watch.commands.any { it is Command.List || it is Command.Time })
        assertEquals(0, server.healthCalls)
    }

    @Test fun watchRefusingPhoneClockStillDrains() {
        now = 1_000_000_000_000L // 2001: the watch answers bad_time
        watch.add(s1, 0)
        assertEquals(DrainOutcome.Done(1, 0), run())
    }

    @Test fun emptyQueueIsJustTimeListBye() {
        assertEquals(DrainOutcome.Done(0, 0), run())
        assertEquals(listOf(Command.Time(now), Command.List(0, 32), Command.Bye), watch.commands)
    }

    @Test fun backoffSchedule() {
        val b = Backoff()
        val mins = (1..7).map { b.nextDelayMs() / 60_000 }
        assertEquals(listOf(1L, 2L, 5L, 10L, 15L, 15L, 15L), mins)
        b.reset()
        assertEquals(60_000L, b.nextDelayMs())
    }

    @Test fun scanThrottleAllowsFiveStartsPer30s() {
        val t = ScanThrottle()
        repeat(5) { assertEquals(0L, t.delayBeforeStartMs(1000L + it)); t.recordStart(1000L + it) }
        assertTrue(t.delayBeforeStartMs(2000) > 0)
        assertEquals(0L, t.delayBeforeStartMs(1000 + 32_000))
    }

    @Test fun timestampsAreIsoUtcWithMillis() {
        assertEquals("2026-10-19T12:34:56.789Z", Timestamps.iso(1792413296789L))
        assertEquals("2026-10-19T12:00:00.000Z", Timestamps.iso(1792411200000L))
    }
}
