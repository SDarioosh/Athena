package org.barvis.sound.relay.relay

import kotlinx.coroutines.runBlocking
import org.barvis.sound.relay.net.Json
import org.barvis.sound.relay.protocol.AckResult
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.ZoneId

/** Plan §4.4: the T5's Tasks tab shows the owner's Odysseus checklists. */
class PdaTodosTest {
    private val timeline = Timeline()
    private val clock = TestClock()
    private val pda = FakePda(timeline, clock)
    private val server = FakeConvoApi(timeline)
    private val cal = FakeCalendar(timeline)
    private val todos = FakeTodos(timeline)
    private val memory = MemoryPdaMemory()
    private val zone = ZoneId.of("Europe/Berlin")

    private fun run(t: TodosApi = todos) = runBlocking {
        PdaSync(pda, server, cal, memory, "AA:BB", clock, zone, todos = t).run()
    }

    private fun toggle(note: String, i: Int, text: String, done: Boolean) =
        pda.queue(Json.write(linkedMapOf("t" to "todo_toggle", "note" to note, "i" to i, "text" to text, "done" to done)))

    private fun builder(max: Int) = FeedBuilder(server, cal, clock, zone, emptyList(), maxBytes = max, todos = todos)

    private fun build(max: Int): FeedContent = runBlocking { (builder(max).build() as FeedBuild.Ok).content }

    @Suppress("UNCHECKED_CAST")
    private fun notesOf(m: Map<String, Any?>) = (m["todos"] as List<*>).map { it as Map<String, Any?> }

    private fun doneCount(m: Map<String, Any?>) =
        notesOf(m).sumOf { n -> (n["items"] as List<*>).count { (it as Map<*, *>)["done"] == true } }

    @After fun noBusy() = assertEquals(0, pda.busyErrors)

    @Test fun feedHasChecklistsOnlyWithTheirIndexes() {
        run()
        val t = notesOf(pda.feedJson())
        assertEquals(listOf("n1", "n2"), t.map { it["id"] }) // no plain note, no empty checklist
        assertEquals("Groceries", t[0]["title"])
        assertEquals(true, t[0]["pinned"])
        assertEquals("2026-10-24", t[0]["due"])
        assertNull("an empty title is null", t[1]["title"])
        assertNull(t[1]["due"])
        assertEquals(listOf(0L, 1L, 2L), (t[0]["items"] as List<*>).map { (it as Map<*, *>)["i"] })
        assertEquals(mapOf("i" to 1L, "text" to "Bread", "done" to true), (t[0]["items"] as List<*>)[1])
        // No templates configured: the key is left out; todos come after the calendar.
        assertEquals(listOf("calendar", "todos"), pda.feedJson().keys.toList().takeLast(2))
    }

    @Test fun hubReportsTodoAndGoalNotesAsChecklists() {
        // The hub's JSON route reports notes of type checklist, todo and goal all as
        // "checklist" (plan §4.4), so the relay's type filter keeps them whatever their kind.
        todos.notes.clear()
        for (kind in listOf("checklist", "todo", "goal")) {
            todos.notes += TodoNote("n-$kind", kind, "checklist", false, null, listOf(TodoItem("Water the plants", false)))
        }
        assertEquals(listOf("n-checklist", "n-todo", "n-goal"), notesOf(build(10_000_000).sections).map { it["id"] })
    }

    @Test fun capsAtTwentyNotesAndFortyItems() {
        todos.notes.clear()
        repeat(25) { n ->
            todos.notes += TodoNote("n$n", "T".repeat(200), "checklist", false, null,
                (0 until 50).map { TodoItem("x".repeat(300) + " $it", false) })
        }
        val t = notesOf(build(10_000_000).sections)
        assertEquals(20, t.size)
        assertEquals(40, (t[0]["items"] as List<*>).size)
        assertEquals(120, (t[0]["title"] as String).length)
        assertTrue(((t[0]["items"] as List<*>)[0] as Map<*, *>)["text"].toString().length <= 160)
    }

    @Test fun trimmingDropsDoneItemsBeforeActionsAndUnpinnedListsBeforeTheCalendar() {
        server.conversationList.clear()
        todos.notes.clear()
        todos.notes += TodoNote("p", "Pinned", "checklist", true, null, (0 until 30).map { TodoItem("pinned item $it", it % 2 == 0) })
        repeat(5) { n ->
            todos.notes += TodoNote("u$n", "List $n", "checklist", false, null, (0 until 30).map { TodoItem("item $it of $n", it % 2 == 0) })
        }
        val fullSize = build(10_000_000).render(1, clock.ms).size
        // Just too big: some done items go, the action items stay.
        val a = build(fullSize - 200).sections
        assertTrue(doneCount(a) < 90)
        assertEquals(2, (a["actions"] as List<*>).size)
        // Much smaller: all done items, then the action items, then unpinned lists; pinned and calendar last.
        val small = build(1_600)
        val b = small.sections
        assertEquals(0, doneCount(b))
        assertEquals(0, (b["actions"] as List<*>).size)
        val lists = notesOf(b).map { it["id"] }
        assertEquals("p", lists.first())
        assertTrue(lists.size < 6)
        assertEquals(2, (b["calendar"] as List<*>).size)
        assertTrue(small.render(1, clock.ms).size <= 1_600)
    }

    @Test fun noTodosForA403OrAnOldHubButTheFeedIsSent() {
        todos.notesAnswer = ApiResult.Unsupported("add the Todos and Todos write permissions to the Odysseus token")
        val out = run()
        assertTrue(out is PdaOutcome.Done)
        assertFalse(pda.feedJson().containsKey("todos"))
        assertTrue(out.stats.todosNote!!.contains("Todos"))
    }

    @Test fun otherTodoFailuresFailTheFeed() {
        todos.notesAnswer = ApiResult.Fail(ServerResult.Transient("HTTP 502"))
        assertTrue(run() is PdaOutcome.ServerUnavailable)
        assertNull(pda.feed)
        // An Odysseus 401 isn't the server's token.
        todos.notesAnswer = ApiResult.Fail(ServerResult.AuthRejected(401))
        pda.reconnect()
        assertTrue(run() is PdaOutcome.ServerUnavailable)
    }

    @Test fun toggleByIndex() {
        val e = toggle("n1", 0, "Milk", true)
        val out = run()
        assertEquals(listOf(Triple("n1", 0, true)), todos.toggles)
        assertEquals(listOf(e.actionId to AckResult.DONE), pda.acks)
        assertEquals(1, out.stats.actionsDone)
        // The feed sent afterwards shows it done.
        val items = notesOf(pda.feedJson())[0]["items"] as List<*>
        assertEquals(true, (items[0] as Map<*, *>)["done"])
    }

    @Test fun indexShiftFindsTheUniqueItemByText() {
        // The website added an item at the top since the last feed: "Eggs" moved from 2 to 3.
        todos.notes[0] = todos.notes[0].copy(items = listOf(TodoItem("Butter", false)) + todos.notes[0].items)
        toggle("n1", 2, "Eggs (free  range)", true) // whitespace is normalised on both sides
        run()
        assertEquals(listOf(Triple("n1", 3, true)), todos.toggles)
    }

    @Test fun ambiguousOrMissingItemIsResult1() {
        todos.notes[0] = todos.notes[0].copy(items = listOf(TodoItem("Milk 1 l", false), TodoItem("Milk 2 l", false)))
        val a = toggle("n1", 5, "Milk", true) // index out of range, two items start with "Milk"
        val b = toggle("n1", 0, "Cheese", true)
        val c = toggle("gone", 0, "Milk", true)
        run()
        assertTrue(todos.toggles.isEmpty())
        assertEquals(listOf(a.actionId, b.actionId, c.actionId).map { it to AckResult.REJECTED }, pda.acks)
    }

    @Test fun blankTextTogglesNothing() {
        // " " normalises to "", which every item starts with: it must not pick an item.
        todos.notes[0] = todos.notes[0].copy(items = listOf(TodoItem("Milk", false)))
        val a = toggle("n1", 0, " ", true)
        val b = toggle("n1", 7, "  ", true)
        run()
        assertTrue(todos.toggles.isEmpty())
        assertEquals(listOf(a.actionId, b.actionId).map { it to AckResult.REJECTED }, pda.acks)
    }

    @Test fun alreadyInTheWantedStateIsResult0WithoutACall() {
        val e = toggle("n1", 1, "Bread", true)
        run()
        assertTrue(todos.toggles.isEmpty())
        assertEquals(listOf(e.actionId to AckResult.DONE), pda.acks)
    }

    @Test fun exitCode1IsResult1() {
        todos.toggleAnswer = ApiResult.Ok(false)
        val e = toggle("n1", 0, "Milk", true)
        run()
        assertEquals(listOf(e.actionId to AckResult.REJECTED), pda.acks)
    }

    @Test fun serverErrorOnToggleStopsTheSync() {
        todos.toggleAnswer = ApiResult.Fail(ServerResult.Transient("HTTP 503"))
        toggle("n1", 0, "Milk", true)
        pda.queue("""{"t":"item_status","id":42,"status":"done"}""")
        assertTrue(run() is PdaOutcome.ServerUnavailable)
        assertTrue(pda.acks.isEmpty())
        assertTrue(server.patches.isEmpty())
        assertNull(pda.feed)
    }

    @Test fun notesAreReadOncePerSyncAndTwoTogglesSeeEachOther() {
        toggle("n1", 0, "Milk", true)
        toggle("n1", 0, "Milk", false)
        run()
        assertEquals(listOf(Triple("n1", 0, true), Triple("n1", 0, false)), todos.toggles)
        assertEquals("one read for the actions, one for the feed", 2, todos.reads)
    }

    @Test fun withoutOdysseusToggleStaysQueued() {
        val e = toggle("n1", 0, "Milk", true)
        val item = pda.queue("""{"t":"item_status","id":42,"status":"done"}""")
        val out = run(NoTodos)
        assertTrue(out is PdaOutcome.Done)
        assertEquals(listOf(item.actionId to AckResult.DONE), pda.acks)
        assertTrue(e.actionId in pda.outbox)
        assertEquals(1, out.stats.actionsWaiting)
    }

    @Test fun strictToggleChecks() {
        fun p(s: String) = PdaAction.parse(s.toByteArray())
        assertEquals(PdaAction.TodoToggle("n1", 2, "Eggs", true), p("""{"t":"todo_toggle","note":"n1","i":2,"text":"Eggs","done":true}"""))
        val longNote = "n".repeat(65)
        val longText = "e".repeat(81)
        for (bad in listOf(
            """{"t":"todo_toggle","note":"n1","i":2,"text":"Eggs"}""",
            """{"t":"todo_toggle","note":"n1","i":2,"text":"Eggs","done":true,"x":1}""",
            """{"t":"todo_toggle","note":"","i":2,"text":"Eggs","done":true}""",
            """{"t":"todo_toggle","note":"$longNote","i":2,"text":"Eggs","done":true}""",
            """{"t":"todo_toggle","note":"n1","i":-1,"text":"Eggs","done":true}""",
            """{"t":"todo_toggle","note":"n1","i":2.0,"text":"Eggs","done":true}""",
            """{"t":"todo_toggle","note":"n1","i":2,"text":"","done":true}""",
            """{"t":"todo_toggle","note":"n1","i":2,"text":"$longText","done":true}""",
            """{"t":"todo_toggle","note":"n1","i":2,"text":"Eggs","done":"yes"}""",
            """{"t":"todo_toggle","note":7,"i":2,"text":"Eggs","done":true}""",
            // Plan §4.4: i is 0-9999, and neither note nor text may be only whitespace.
            """{"t":"todo_toggle","note":"n1","i":10000,"text":"Eggs","done":true}""",
            """{"t":"todo_toggle","note":"n1","i":2,"text":" ","done":true}""",
            """{"t":"todo_toggle","note":"n1","i":2,"text":"\t \n","done":true}""",
            """{"t":"todo_toggle","note":" ","i":2,"text":"Eggs","done":true}""",
        )) assertTrue(bad, p(bad) is PdaAction.Invalid)
        assertEquals(PdaAction.TodoToggle("n1", 9999, "Eggs", true), p("""{"t":"todo_toggle","note":"n1","i":9999,"text":"Eggs","done":true}"""))
    }
}
