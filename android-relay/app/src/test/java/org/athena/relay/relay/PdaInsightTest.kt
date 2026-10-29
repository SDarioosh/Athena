package org.athena.relay.relay

import kotlinx.coroutines.runBlocking
import org.athena.relay.net.Json
import org.athena.relay.net.OdysseusInsightClient
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.ZoneId

/**
 * Plan §4.1: the hub's Today insight in the T5 feed, the same cases as the PC relay's
 * test_pda_insight.py. Fictional data only.
 */
class PdaInsightTest {
    private val clock = TestClock()
    private val pda = FakePda(clock = clock)
    private val server = FakeConvoApi()
    private val cal = FakeCalendar()
    private val todos = FakeTodos()
    private val insight = FakeInsight()
    private val memory = MemoryPdaMemory()
    private val zone = ZoneId.of("Europe/Berlin")

    private fun run() = runBlocking {
        PdaSync(pda, server, cal, memory, "AA:BB", clock, zone, todos = todos, insight = insight).run()
    }

    private fun builder(max: Int = 10_000_000, calendar: CalendarApi = cal, t: TodosApi = todos) =
        FeedBuilder(server, calendar, clock, zone, emptyList(), maxBytes = max, todos = t, insight = insight)

    private fun build(max: Int = 10_000_000, calendar: CalendarApi = cal, t: TodosApi = todos): FeedContent =
        runBlocking { (builder(max, calendar, t).build() as FeedBuild.Ok).content }

    @Suppress("UNCHECKED_CAST")
    private fun insightOf(c: FeedContent) = c.sections["insight"] as Map<String, Any?>?

    private fun ok(raw: Map<String, Any?>) {
        insight.answer = ApiResult.Ok(raw)
    }

    private fun refs(vararg pairs: Pair<String, String>) = pairs.map { (r, w) -> mapOf("ref" to r, "why" to w) }

    private val expected = mapOf(
        "date" to "2026-10-20",
        "headline" to "A light day: the dentist in the morning, then the groceries.",
        "focus" to refs("todo:n1:0" to "Needed for breakfast tomorrow", "event:e1" to "Bring the referral letter"),
        "coming_up" to refs("event:e2" to "Book the train"),
        "also" to refs("todo:n2:0" to "Before the weekend"),
    )

    @After fun noBusy() = assertEquals(0, pda.busyErrors)

    // ------------------------------------------------------------------ the feed's insight

    @Test fun syncPutsTheInsightInTheFeed() {
        val out = run()
        assertTrue(out is PdaOutcome.Done)
        val feed = pda.feedJson()
        assertEquals(expected, feed["insight"]) // no `title`, no `generated`
        assertEquals(listOf("calendar", "todos", "insight"), feed.keys.toList().takeLast(3))
        assertNull(out.stats.insightNote)
        // The push hash covers the insight like every other key.
        val before = build().hash
        ok(FakeInsight.demo().also { it["headline"] = "Another day" })
        assertFalse(build().hash == before)
    }

    @Test fun refsMustResolveInTheFeed() {
        val good = listOf(FakeInsight.item("todo:n1:2", "ok"), FakeInsight.item("event:e1", "ok"))
        ok(FakeInsight.demo().also {
            it["focus"] = listOf(
                good[0],
                FakeInsight.item("todo:n1:3", "no item 3"),
                FakeInsight.item("todo:n1:x", "not an integer"),
                FakeInsight.item("todo:n1:01", "not the feed's index"),
                FakeInsight.item("todo:n1:-1", "negative"),
                FakeInsight.item("todo:n-unknown:0", "no such note"),
                FakeInsight.item("todo:n3:0", "a plain note"),
                FakeInsight.item("todo:n4:0", "an empty checklist (not in the feed)"),
                FakeInsight.item("todo:n1", "no index"),
                FakeInsight.item("event:e9", "no such event"),
                FakeInsight.item("event:", "no id"),
                FakeInsight.item("conv:c1", "not a todo or event"),
                FakeInsight.item(5L, "not a string"),
                mapOf("why" to "no ref"),
                "event:e1",
                good[1],
            )
            it["coming_up"] = listOf(FakeInsight.item("event:E1", "ids are case-sensitive"))
        })
        val out = insightOf(build())!!
        assertEquals(refs("todo:n1:2" to "ok", "event:e1" to "ok"), out["focus"])
        assertEquals(emptyList<Any?>(), out["coming_up"])
    }

    @Test fun eventRefsNeedTheCalendarAndTodoRefsTheTodos() {
        val c = build(calendar = NoCalendar, t = NoTodos)
        assertEquals(emptyList<Any?>(), c.sections["calendar"])
        assertFalse(c.sections.containsKey("todos"))
        assertEquals(expected + mapOf("focus" to emptyList<Any?>(), "coming_up" to emptyList<Any?>(), "also" to emptyList<Any?>()),
            insightOf(c))
    }

    @Test fun caps() {
        ok(linkedMapOf(
            "date" to "2026-10-20",
            "headline" to "word ".repeat(80),
            "focus" to List(3) { FakeInsight.item("event:e1", "why ".repeat(40)) } + listOf(
                mapOf("ref" to "todo:n1:0"),
                FakeInsight.item("todo:n1:1", 7L),
                FakeInsight.item("todo:n2:0", "  two   spaces "),
                FakeInsight.item("event:e1", "6th"),
            ),
            "coming_up" to "not a list",
            // "also" is missing
            "extra" to mapOf("ignored" to true),
        ))
        val out = insightOf(build())!!
        val headline = out["headline"] as String
        assertTrue(headline.length <= FeedBuilder.INSIGHT_HEADLINE_MAX && headline.endsWith("…"))
        val focus = (out["focus"] as List<*>).map { it as Map<*, *> }
        assertEquals("the first five kept", FeedBuilder.INSIGHT_ITEMS_MAX, focus.size)
        assertTrue(focus.all { (it["why"] as String).length <= FeedBuilder.INSIGHT_WHY_MAX })
        assertTrue((focus[0]["why"] as String).endsWith("…"))
        assertEquals("missing, not a string", listOf("", ""), focus.drop(3).map { it["why"] })
        assertEquals(emptyList<Any?>(), out["coming_up"])
        assertEquals(emptyList<Any?>(), out["also"])
        assertEquals(listOf("date", "headline", "focus", "coming_up", "also"), out.keys.toList())
    }

    // ------------------------------------------------------------------ left out, not a failure

    @Test fun noInsightLeavesItOut() {
        for (why in listOf(OdysseusInsightClient.NOT_YET, OdysseusInsightClient.NO_SCOPE,
            OdysseusInsightClient.OLD_HUB, OdysseusInsightClient.MALFORMED)) {
            insight.answer = ApiResult.Unsupported(why)
            pda.reconnect()
            val out = run()
            assertTrue(why, out is PdaOutcome.Done)
            val feed = pda.feedJson()
            assertFalse(why, feed.containsKey("insight"))
            assertTrue((feed["todos"] as List<*>).isNotEmpty() && (feed["calendar"] as List<*>).isNotEmpty())
            assertEquals(why, out.stats.insightNote)
        }
    }

    @Test fun malformedAnswerLeavesItOut() {
        for (raw in listOf<Map<String, Any?>>(
            mapOf("headline" to "no date"),
            mapOf("date" to "20.10.2026", "headline" to "bad date"),
            mapOf("date" to "2026-10-20T05:00:00Z", "headline" to "a time, not a date"),
            mapOf("date" to "２０２６-10-20", "headline" to "not ASCII digits"),
            mapOf("date" to 20261020L, "headline" to "a number"),
            mapOf("date" to "2026-10-20"),
            mapOf("date" to "2026-10-20", "headline" to null),
            mapOf("date" to "2026-10-20", "headline" to listOf("a list")),
        )) {
            ok(raw)
            val b = builder()
            val c = runBlocking { (b.build() as FeedBuild.Ok).content }
            assertFalse(raw.toString(), c.sections.containsKey("insight"))
            assertEquals(FeedBuilder.INSIGHT_MALFORMED, b.insightNote)
        }
        // An answer that isn't a JSON object is the client's: see PdaHttpTest.
    }

    @Test fun noOdysseusLeavesItOut() {
        val out = runBlocking { PdaSync(pda, server, NoCalendar, memory, "AA:BB", clock, zone).run() }
        assertTrue(out is PdaOutcome.Done)
        assertFalse(pda.feedJson().containsKey("insight"))
        assertTrue(out.stats.insightNote!!.contains("isn't set up"))
    }

    @Test fun temporaryFailuresFailTheFeedLikeTheCalendar() {
        insight.answer = ApiResult.Fail(ServerResult.Transient("HTTP 503"))
        assertTrue(run() is PdaOutcome.ServerUnavailable)
        assertNull(pda.feed)
        // An Odysseus 401 isn't the server's token.
        insight.answer = ApiResult.Fail(ServerResult.AuthRejected(401))
        pda.reconnect()
        assertTrue(run() is PdaOutcome.ServerUnavailable)
        assertNull(pda.feed)
    }

    // ------------------------------------------------------------------ trimming (§4.1)

    @Test fun trimmingDropsTheInsightAfterDoneItemsBeforeActions() {
        server.conversationList.clear()
        todos.notes.clear()
        todos.notes += TodoNote("n", "n", "checklist", true, null, listOf(TodoItem("x".repeat(100), false), TodoItem("x".repeat(100), true)))
        ok(linkedMapOf(
            "date" to "2026-10-20", "headline" to "h".repeat(150),
            "focus" to listOf(FakeInsight.item("todo:n:1", "done one"), FakeInsight.item("todo:n:0", "open one")),
            "coming_up" to listOf(FakeInsight.item("event:e1", "soon")), "also" to emptyList<Any?>(),
        ))
        val fullContent = build()
        val full = fullContent.render(0xFFFFFFFFL, clock.ms).size
        val item = Json.write(linkedMapOf("i" to 1, "text" to "x".repeat(100), "done" to true)).length + 1
        val ins = Json.write(fullContent.sections["insight"]).toByteArray().size + ",\"insight\":".length
        fun actions(c: FeedContent) = (c.sections["actions"] as List<*>).size
        // 1) the done item goes first; the insight stays but loses the ref to it
        val a = build(full - 1)
        @Suppress("UNCHECKED_CAST")
        assertEquals(listOf(0), ((a.sections["todos"] as List<Map<String, Any?>>)[0]["items"] as List<*>).map { (it as Map<*, *>)["i"] })
        assertEquals(refs("todo:n:0" to "open one"), insightOf(a)!!["focus"])
        assertEquals(refs("event:e1" to "soon"), insightOf(a)!!["coming_up"])
        assertEquals(2, actions(a))
        // 2) then the whole insight, before any action item
        val b = build(full - item - 10)
        assertNull(insightOf(b))
        assertEquals(2, actions(b))
        // 3) then the action items
        val c = build(full - item - ins - 10)
        assertNull(insightOf(c))
        assertEquals(1, actions(c))
    }
}
