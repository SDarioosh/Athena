package org.barvis.sound.relay.relay

import kotlinx.coroutines.runBlocking
import org.barvis.sound.relay.net.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.ZoneId

/**
 * Plan §4.1 `weights`, §5: the hub's rule weights in the T5 feed, the same cases as the PC
 * relay's test_pda_weights.py. Fictional data only.
 */
class PdaWeightsTest {
    private val clock = TestClock()
    private val pda = FakePda(clock = clock)
    private val server = FakeConvoApi()
    private val cal = FakeCalendar()
    private val todos = FakeTodos()
    private val insight = FakeInsight()
    private val memory = MemoryPdaMemory()
    private val zone = ZoneId.of("Europe/Berlin")

    private val table = linkedMapOf<String, Any?>("version" to 1L, "date" to "2026-10-20",
        "weights" to linkedMapOf<String, Any?>("list:n2" to 2L, "list:n1" to -1L))

    private fun builder(max: Int = 10_000_000, i: InsightApi = insight, m: PdaMemory? = memory) =
        FeedBuilder(server, cal, clock, zone, emptyList(), maxBytes = max, todos = todos, insight = i, memory = m)

    private fun build(b: FeedBuilder = builder()): FeedContent = runBlocking { (b.build() as FeedBuild.Ok).content }

    private fun run() = runBlocking {
        PdaSync(pda, server, cal, memory, "AA:BB", clock, zone, todos = todos, insight = insight).run()
    }

    @Test fun syncPutsTheTableInTheFeed() {
        insight.weightsAnswer = ApiResult.Ok(table)
        assertTrue(run() is PdaOutcome.Done)
        val feed = pda.feedJson()
        assertEquals(table, feed["weights"])
        assertEquals(listOf("todos", "insight", "weights"), feed.keys.toList().takeLast(3))
        assertEquals(table, Json.parseObject(memory.weights!!)) // kept for later failures
        // The push hash covers the weights.
        val before = build().hash
        insight.weightsAnswer = ApiResult.Ok(table + mapOf("weights" to mapOf("list:n2" to 3L)))
        assertFalse(build().hash == before)
    }

    @Test fun onlyListKeysWithIntegersClampedAndCapped() {
        val raw = linkedMapOf<String, Any?>("version" to 1L, "date" to "2026-10-20", "extra" to true, "weights" to linkedMapOf(
            "list:a" to 9L, "list:b" to -12L, "list:c" to 0L, "list:d" to true, "list:e" to 1.0, "list:f" to "2",
            "list:g" to null, "note_type:checklist" to 2L, "event_type:work" to 1L, "list:" to 3L, "LIST:h" to 1L,
            "list:" + "x".repeat(151) to 1L, "list:" + "y".repeat(150) to 1L, "list:ü" to -6L))
        assertEquals(linkedMapOf("version" to 1L, "date" to "2026-10-20", "weights" to linkedMapOf(
            "list:a" to 6L, "list:b" to -6L, "list:c" to 0L, "list:" + "y".repeat(150) to 1L, "list:ü" to -6L)),
            FeedBuilder.weightsSection(raw))
        val many = FeedBuilder.weightsSection(mapOf("weights" to (0 until 40).associate { "list:n$it" to (it % 13 - 6).toLong() }))!!
        @Suppress("UNCHECKED_CAST")
        assertEquals((0 until 32).map { "list:n$it" }, (many["weights"] as Map<String, Any?>).keys.toList())
        assertFalse(many.containsKey("date"))
        for (bad in listOf(mapOf("version" to 2L, "weights" to mapOf("list:a" to 1L)), mapOf("weights" to listOf("list:a")),
            mapOf("date" to "2026-10-20"))) {
            assertNull(FeedBuilder.weightsSection(bad))
        }
    }

    @Test fun anEmptyTableReplacesTheLastOne() {
        memory.weights = Json.write(table)
        insight.weightsAnswer = ApiResult.Ok(mapOf("version" to 1L, "date" to "2026-10-21", "weights" to emptyMap<String, Any?>()))
        assertEquals(mapOf("version" to 1L, "date" to "2026-10-21", "weights" to emptyMap<String, Any?>()), build().sections["weights"])
    }

    @Test fun aFailureKeepsTheLastGoodTable() {
        // never fetched: no key
        assertFalse(build().sections.containsKey("weights"))
        memory.weights = Json.write(table)
        for (a in listOf<ApiResult<Map<String, Any?>>>(
            ApiResult.Unsupported("this Odysseus has no weights route (update the hub)"),
            ApiResult.Unsupported("token lacks insight:read"),
            ApiResult.Fail(ServerResult.Transient("HTTP 503")),
            ApiResult.Ok(mapOf("weights" to "list:n2=2")), // malformed
        )) {
            insight.weightsAnswer = a
            val c = build()
            assertEquals(table, c.sections["weights"])
            assertTrue(c.sections.containsKey("insight")) // never fails the feed
        }
        // a broken saved table isn't sent
        memory.weights = """{"weights": "bad"}"""
        assertFalse(build().sections.containsKey("weights"))
        memory.weights = "not json"
        assertFalse(build().sections.containsKey("weights"))
    }

    @Test fun withoutMemoryTheLastTableIsKeptInTheBuilder() {
        val b = builder(m = null)
        insight.weightsAnswer = ApiResult.Ok(table)
        assertEquals(table, build(b).sections["weights"])
        insight.weightsAnswer = ApiResult.Fail(ServerResult.Transient("HTTP 503"))
        assertEquals(table, build(b).sections["weights"])
    }

    @Test fun noOdysseusLeavesItOut() {
        memory.weights = Json.write(table)
        assertFalse(build(builder(i = NoInsight)).sections.containsKey("weights"))
    }

    @Test fun trimmingDropsWeightsAfterTheInsight() {
        server.conversationList.clear()
        todos.notes.clear()
        insight.weightsAnswer = ApiResult.Ok(table)
        val full = build().render(0xFFFFFFFFL, clock.nowMs()).size
        val noInsight = build(builder(max = full - 1))
        assertFalse(noInsight.sections.containsKey("insight"))
        assertTrue(noInsight.sections.containsKey("weights"))
        val smaller = noInsight.render(0xFFFFFFFFL, clock.nowMs()).size
        val neither = build(builder(max = smaller - 1))
        assertFalse(neither.sections.containsKey("insight") || neither.sections.containsKey("weights"))
        assertEquals((build().sections["actions"] as List<*>).size, (neither.sections["actions"] as List<*>).size)
    }
}
