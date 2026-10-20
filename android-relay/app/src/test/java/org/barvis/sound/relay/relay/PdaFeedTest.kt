package org.barvis.sound.relay.relay

import kotlinx.coroutines.runBlocking
import org.barvis.sound.relay.net.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.ZoneId

/** Feed (§4.1), details (§4.3), actions (§4.2) and the PDA schedule. */
class PdaFeedTest {
    private val clock = TestClock()
    private val zone = ZoneId.of("Europe/Berlin")
    private val server = FakeConvoApi()
    private val cal = FakeCalendar()
    private val memory = MemoryPdaMemory()

    private fun build(maxBytes: Int = FeedBuilder.MAX_FEED_BYTES, calendar: CalendarApi = cal): FeedContent {
        val r = runBlocking { FeedBuilder(server, calendar, memory, clock, zone, listOf("Call"), maxBytes).build() }
        return (r as FeedBuild.Ok).content
    }

    @Suppress("UNCHECKED_CAST")
    private fun parsed(c: FeedContent) = Json.parseObject(c.render(1729411200L, clock.ms).toString(Charsets.UTF_8))

    @Test fun sectionsFollowThePlan() {
        val f = parsed(build())
        assertEquals(listOf("v", "version", "generated", "tz", "briefing", "conversations", "actions", "calendar", "templates"),
            f.keys.toList())
        assertEquals("2026-10-20T08:00:00Z", f["generated"])
        val b = f["briefing"] as Map<*, *>
        assertEquals("2026-10-19", b["date"]) // no digest for today yet: yesterday's
        assertEquals("Anna and you planned the garden party.", b["text"])
        val c = (f["conversations"] as List<*>)[0] as Map<*, *>
        assertEquals("2026-10-19T17:00:00+02:00", c["at"])
        assertEquals(listOf("Point one", "Point two"), c["points"])
        val a = (f["actions"] as List<*>).map { it as Map<*, *> }
        assertEquals(mapOf("id" to 42L, "text" to "Order the lanterns", "owner" to "me", "due" to "2026-10-23", "conv" to "c1"), a[0])
        assertEquals("", a[1]["due"])
        val e = (f["calendar"] as List<*>).map { it as Map<*, *> }
        assertEquals("2026-10-21T10:00:00+02:00", e[0]["start"])
        assertEquals("2026-10-24", e[1]["start"])
        assertEquals(true, e[1]["all_day"])
        // Today 00:00 local to +8 days.
        assertEquals("2026-10-20T00:00:00+02:00" to "2026-10-28T00:00:00+01:00", cal.ranges.single())
    }

    @Test fun hashIgnoresVersionAndGenerated() {
        val a = build()
        clock.ms += 3_600_000
        val b = build()
        assertEquals(a.hash, b.hash)
        server.items.removeAt(0)
        assertFalse(build().hash == a.hash)
    }

    @Test fun bigFeedsAreCutSummariesFirstThenOldConversations() {
        server.conversationList.clear()
        repeat(20) { i ->
            server.conversationList += FakeConvoApi.conv("c$i", "Talk $i", "2026-10-19T%02d:00:00Z".format(20 - i), "s".repeat(3000))
        }
        val small = parsed(build(maxBytes = 12_000))
        val convs = (small["conversations"] as List<*>).map { it as Map<*, *> }
        assertTrue(build(maxBytes = 12_000).render(1, clock.ms).size <= 12_000)
        assertTrue(convs.all { (it["summary"] as String).length <= 300 })
        val big = parsed(build())
        assertEquals(20, (big["conversations"] as List<*>).size)
        assertTrue(((big["conversations"] as List<*>)[0] as Map<*, *>)["summary"].toString().endsWith("…"))

        val tiny = parsed(build(maxBytes = 1_500))
        val kept = (tiny["conversations"] as List<*>).map { (it as Map<*, *>)["id"] }
        assertTrue(kept.size < 20)
        assertEquals("newest kept", "c0", kept.firstOrNull() ?: "c0")
    }

    @Test fun serverFailureFailsTheBuild() {
        server.conversationsFail = ServerResult.Transient("HTTP 502")
        val r = runBlocking { FeedBuilder(server, cal, memory, clock, zone, emptyList()).build() }
        assertTrue(r is FeedBuild.Fail)
    }

    @Test fun conversationDetail() {
        val d = runBlocking { DetailBuilder(server, zone).build(DetailRef.Conv("c2")) }
        val m = Json.parseObject(((d as ApiResult.Ok).value).toString(Charsets.UTF_8))
        assertEquals("conv:c2", m["ref"])
        assertEquals("Bike repair", m["title"])
        assertEquals(listOf(43L), (m["actions"] as List<*>).map { (it as Map<*, *>)["id"] })
    }

    @Test fun transcriptsArePaged() {
        server.transcripts["c1"] = mapOf("title" to "Garden party", "segments" to (0 until 120).map { i ->
            mapOf("offset" to i * 10.0, "speaker" to if (i % 2 == 0) "Anna" else "Me", "is_owner" to (i % 2 == 1),
                "text" to "Line $i about lanterns.")
        })
        val b = DetailBuilder(server, zone)
        val p1 = Json.parseObject(String((runBlocking { b.build(DetailRef.Transcript("c1")) } as ApiResult.Ok).value))
        assertEquals(60, (p1["segments"] as List<*>).size)
        assertEquals("transcript:c1@600-1200", p1["next"])
        val seg = (p1["segments"] as List<*>)[1] as Map<*, *>
        assertEquals(mapOf("o" to 10.0, "s" to "Me", "me" to true, "x" to "Line 1 about lanterns."), seg)
        val last = Json.parseObject(String((runBlocking { b.build(DetailRef.Transcript("c1", 600, 1200)) } as ApiResult.Ok).value))
        assertNull(last["next"])

        // A page over the size limit stops early; next starts at the first segment left out.
        val small = DetailBuilder(server, zone, maxBytes = 1500)
        val p = Json.parseObject(String((runBlocking { small.build(DetailRef.Transcript("c1")) } as ApiResult.Ok).value))
        val n = (p["segments"] as List<*>).size
        assertTrue(n in 2..59)
        assertEquals("transcript:c1@${n * 10}-${n * 10 + 600}", p["next"])
    }

    @Test fun actionParsing() {
        fun p(s: String) = PdaAction.parse(s.toByteArray(), zone)
        assertEquals(PdaAction.ItemStatus(42, "dismissed"), p("""{"t":"item_status","id":42,"status":"dismissed"}"""))
        assertEquals(PdaAction.Fetch(DetailRef.Transcript("c1", 0, 600)), p("""{"t":"fetch","ref":"transcript:c1@0-600"}"""))
        assertEquals(PdaAction.Fetch(DetailRef.Conv("ab12")), p("""{"t":"fetch","ref":"conv:ab12"}"""))
        assertEquals(PdaAction.DigestRefresh("2026-10-19"), p("""{"t":"digest_refresh","date":"2026-10-19"}"""))
        assertEquals(PdaAction.CalDelete("u1::20261020"), p("""{"t":"cal_delete","id":"u1::20261020"}"""))
        val add = p("""{"t":"cal_add","title":" Call\u0007 Ben ","start":"2026-10-22T10:00:00Z","end":"2026-10-22T10:15:00Z"}""")
        assertEquals(NewEvent("Call Ben", "2026-10-22T10:00:00Z", "2026-10-22T10:15:00Z", false, "Added on the T5 PDA"),
            (add as PdaAction.CalAdd).event)
        val allDay = p("""{"t":"cal_add","title":"Trip","start":"2026-10-24","end":"2026-10-26","all_day":true}""")
        assertEquals("2026-10-26", (allDay as PdaAction.CalAdd).event.end)
        for (bad in listOf(
            """{"t":"digest_refresh","date":"2026-02-30"}""",
            """{"t":"fetch","ref":"transcript:c1@600-600"}""",
            """{"t":"cal_add","title":"x","start":"2026-10-22T10:00","end":"2026-10-22T09:00"}""",
            """{"t":"cal_add","title":"x","start":"tomorrow"}""",
            """{"t":"cal_delete","id":""}""",
            """[1,2]""",
            "{\"t\":\"item_status\",\"id\":1,\"status\":\"done\",\"pad\":\"${"x".repeat(400)}\"}",
        )) assertTrue(bad, p(bad) is PdaAction.Invalid)
    }

    @Test fun scheduleBacksOffAndKeepsTheInterval() {
        val s = PdaSchedule({ 15 * 60_000L })
        assertTrue(s.isDue(0))
        assertEquals(15 * 60_000L, s.after(PdaOutcome.Done(PdaStats()), 1000))
        assertFalse(s.isDue(1000 + 14 * 60_000L))
        assertTrue(s.isDue(1000 + 15 * 60_000L))
        assertEquals(60_000L, s.after(PdaOutcome.ServerUnavailable("x", PdaStats()), 0))
        assertEquals(120_000L, s.after(PdaOutcome.ServerUnavailable("x", PdaStats()), 0))
        assertEquals(60_000L, s.after(PdaOutcome.LinkLost("x", PdaStats()), 0))
        s.after(PdaOutcome.Done(PdaStats()), 0)
        assertEquals(60_000L, s.after(PdaOutcome.DeviceError("x", PdaStats()), 0))
    }
}
