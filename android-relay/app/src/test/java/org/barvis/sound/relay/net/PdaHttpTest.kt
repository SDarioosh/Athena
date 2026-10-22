package org.barvis.sound.relay.net

import com.sun.net.httpserver.HttpServer
import kotlinx.coroutines.runBlocking
import org.barvis.sound.relay.relay.ApiResult
import org.barvis.sound.relay.relay.NewEvent
import org.barvis.sound.relay.relay.ServerResult
import org.barvis.sound.relay.relay.TodoItem
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException
import java.net.InetSocketAddress

class PdaHttpTest {
    private val token = "ody_fictional-token-0123456789"
    private val logs = mutableListOf<String>()

    /** Records requests; answers with [answer]. */
    private class FakeTransport(var answer: (String, String) -> HttpAnswer) : HttpTransport {
        val calls = mutableListOf<Triple<String, String, Map<String, String>>>()
        val bodies = mutableListOf<String?>()
        override suspend fun exchange(method: String, url: String, headers: Map<String, String>, body: ByteArray?): HttpAnswer {
            calls += Triple(method, url, headers)
            bodies += body?.toString(Charsets.UTF_8)
            return answer(method, url)
        }
    }

    private fun noLeak() = assertFalse("token leaked into logs", logs.any { it.contains(token) })

    @Test fun jsonRoundTrip() {
        val text = """{"a":1,"b":[true,false,null,-2.5e3,"x\"y\\z\n\u00e9"],"c":{},"d":[]}"""
        val v = Json.parse(text) as Map<*, *>
        assertEquals(1L, v["a"])
        assertEquals(listOf(true, false, null, -2500.0, "x\"y\\z\né"), v["b"])
        assertEquals(v, Json.parse(Json.write(v)))
        assertEquals("\"\\u0001\"", Json.write("\u0001"))
        for (bad in listOf("", "{", "[1,]", "{\"a\" 1}", "01x", "\"abc", "nul", "{}x")) {
            try {
                Json.parse(bad)
                throw AssertionError("parsed bad JSON: $bad")
            } catch (e: Json.ParseException) {
                // expected
            }
        }
    }

    @Test fun convoApiThroughOdysseus() = runBlocking {
        val t = FakeTransport { m, url ->
            when {
                url.endsWith("/api/codex/sound/health") -> HttpAnswer(200, """{"ok": true}""")
                url.contains("/api/codex/sound/conversations?") -> HttpAnswer(200, """{"conversations": []}""")
                url.contains("/api/codex/sound/action-items?") -> HttpAnswer(200, """{"action_items": []}""")
                url.contains("/api/codex/sound/digests/") && m == "GET" -> HttpAnswer(200, """{"date": "2026-10-19"}""")
                m == "PATCH" || m == "POST" -> HttpAnswer(200, """{"ok": true}""")
                else -> HttpAnswer(404, """{"detail": "Not Found"}""")
            }
        }
        val api = ConvoHttpApi("http://hub:7000", { token }, t, { _, msg -> logs += msg }, Routes.ODYSSEUS)
        assertEquals(ServerResult.Ok(), api.health())
        assertTrue(api.conversations(100, 5) is ApiResult.Ok)
        assertTrue(api.openActionItems() is ApiResult.Ok)
        assertTrue(api.digest("2026-10-19") is ApiResult.Ok)
        assertTrue(api.conversation("c 1") is ApiResult.Fail)
        assertEquals(ServerResult.Ok(), api.setActionItemStatus(4, "done"))
        assertEquals(ServerResult.Ok(), api.makeDigest("2026-10-19"))
        assertEquals(listOf(
            "GET http://hub:7000/api/codex/sound/health",
            "GET http://hub:7000/api/codex/sound/conversations?since=100&limit=5",
            "GET http://hub:7000/api/codex/sound/action-items?status=open",
            "GET http://hub:7000/api/codex/sound/digests/2026-10-19",
            "GET http://hub:7000/api/codex/sound/conversations/c%201",
            "PATCH http://hub:7000/api/codex/sound/action-items/4",
            "POST http://hub:7000/api/codex/sound/digests/2026-10-19",
        ), t.calls.map { "${it.first} ${it.second}" })
        // Through Odysseus even health carries the token.
        assertTrue(t.calls.all { it.third["Authorization"] == "Bearer $token" })
        noLeak()
    }

    @Test fun calendarList() = runBlocking {
        val t = FakeTransport { _, _ ->
            HttpAnswer(200, """{"calendars": [{"name": "Personal", "href": "cal-1", "source": "caldav"},
                {"name": "Local", "href": "cal-2", "source": "local"}, {"name": "no id"}]}""")
        }
        val r = OdysseusCalendarClient("http://hub:7000", { token }, transport = t).calendars()
        assertEquals(ApiResult.Ok(listOf(CalendarChoice("cal-1", "Personal", "caldav"),
            CalendarChoice("cal-2", "Local", "local"))), r)
        assertEquals("Personal (CalDAV)", (r as ApiResult.Ok).value[0].label)
        assertEquals("http://hub:7000/api/codex/calendars", t.calls.single().second)
        t.answer = { _, _ -> HttpAnswer(404, """{"detail": "Not Found"}""") }
        assertTrue(OdysseusCalendarClient("http://hub:7000", { token }, transport = t).calendars() is ApiResult.Unsupported)
        t.answer = { _, _ -> HttpAnswer(403, """{"detail": "API token missing required scope"}""") }
        val denied = OdysseusCalendarClient("http://hub:7000", { token }, transport = t).calendars()
        assertTrue(denied is ApiResult.Fail && denied.error is ServerResult.AuthRejected)
    }

    @Test fun convoApiPathsAndPatch() = runBlocking {
        val t = FakeTransport { m, url ->
            when {
                url.endsWith("/health") -> HttpAnswer(200, """{"ok": true}""")
                url.contains("/v1/conversations?") -> HttpAnswer(200, """{"conversations": [{"id": "c1"}]}""")
                url.contains("/v1/action-items?") -> HttpAnswer(200, """{"action_items": []}""")
                url.contains("/v1/digests/") && m == "GET" -> HttpAnswer(404, """{"detail": "no digest for that date"}""")
                url.contains("/v1/digests/") -> HttpAnswer(200, """{"date": "2026-10-19", "summary": "s"}""")
                m == "PATCH" -> HttpAnswer(200, """{"ok": true}""")
                else -> HttpAnswer(500, "")
            }
        }
        val api = ConvoHttpApi("http://100.64.0.1:8000/", { token }, t, log = { _, msg -> logs += msg })
        assertEquals(ServerResult.Ok(), api.health())
        assertNull("no token on /health", t.calls[0].third["Authorization"])
        val convs = api.conversations(1792310400, 20) as ApiResult.Ok
        assertEquals("c1", convs.value[0]["id"])
        assertEquals("http://100.64.0.1:8000/v1/conversations?since=1792310400&limit=20", t.calls[1].second)
        assertEquals("Bearer $token", t.calls[1].third["Authorization"])
        assertEquals(ApiResult.Ok(emptyList<Map<String, Any?>>()), api.openActionItems())
        assertEquals(ApiResult.Fail(ServerResult.Rejected(404, "no digest for that date")), api.digest("2026-10-20"))
        assertEquals(ServerResult.Ok(), api.setActionItemStatus(42, "done"))
        assertEquals("PATCH" to "http://100.64.0.1:8000/v1/action-items/42", t.calls.last().first to t.calls.last().second)
        assertEquals("""{"status":"done"}""", t.bodies.last())
        assertEquals("application/json", t.calls.last().third["Content-Type"])
        assertEquals(ServerResult.Ok(), api.makeDigest("2026-10-19"))
        t.answer = { _, _ -> throw IOException("connection refused") }
        assertTrue((api.conversation("c1") as ApiResult.Fail).error is ServerResult.Transient)
        t.answer = { _, _ -> HttpAnswer(200, "<html>proxy</html>") }
        assertTrue((api.openActionItems() as ApiResult.Fail).error is ServerResult.Transient)
        t.answer = { _, _ -> HttpAnswer(401, """{"detail": "bad token"}""") }
        assertEquals(ApiResult.Fail(ServerResult.AuthRejected(401)), api.transcript("c1"))
        noLeak()
    }

    @Test fun odysseusCalendarClient() = runBlocking {
        val t = FakeTransport { m, url ->
            when (m) {
                "GET" -> HttpAnswer(200, """{"events": [{"uid": "u1", "summary": "Dentist", "dtstart": "2026-10-21T08:00:00Z",
                    "dtend": "2026-10-21T08:30:00Z", "all_day": false, "location": "Main St", "is_utc": true}]}""")
                "POST" -> HttpAnswer(200, """{"ok": true, "uid": "u2", "event": {}, "reminder": null}""")
                "DELETE" -> if (url.endsWith("/gone")) HttpAnswer(404, """{"detail": "Event not found"}""")
                    else HttpAnswer(200, """{"ok": true}""")
                else -> HttpAnswer(500, "")
            }
        }
        val c = OdysseusCalendarClient("http://hub.example.ts.net:7000", { token }, "cal-caldav-1", t,
            log = { _, msg -> logs += msg })
        val list = c.listEvents("2026-10-20T00:00:00+02:00", "2026-10-28T00:00:00+01:00") as ApiResult.Ok
        assertEquals("Dentist", list.value.single().title)
        assertEquals("http://hub.example.ts.net:7000/api/codex/calendar/events?start=2026-10-20T00%3A00%3A00%2B02%3A00" +
            "&end=2026-10-28T00%3A00%3A00%2B01%3A00&calendar=cal-caldav-1", t.calls[0].second)
        assertEquals("Bearer $token", t.calls[0].third["Authorization"])

        assertEquals(ApiResult.Ok("u2"), c.createEvent(NewEvent("Call Ben", "2026-10-22", null, true, "Added on the T5 PDA")))
        val body = Json.parseObject(t.bodies.last()!!)
        assertEquals(mapOf("summary" to "Call Ben", "dtstart" to "2026-10-22", "all_day" to true,
            "description" to "Added on the T5 PDA", "calendar_href" to "cal-caldav-1"), body)

        assertEquals(ApiResult.Ok(Unit), c.deleteEvent("u1::2026-10-21"))
        assertTrue(t.calls.last().second.endsWith("/api/codex/calendar/events/u1%3A%3A2026-10-21"))
        assertEquals(ApiResult.Fail(ServerResult.Rejected(404, "Event not found")), c.deleteEvent("gone"))

        // An older hub without the codex routes, or without a calendar: "not supported", no guessing.
        t.answer = { _, _ -> HttpAnswer(404, """{"detail": "Not Found"}""") }
        assertTrue(c.listEvents("a", "b") is ApiResult.Unsupported)
        assertTrue(c.createEvent(NewEvent("x", "2026-10-22", null, true)) is ApiResult.Unsupported)
        assertTrue(c.deleteEvent("u1") is ApiResult.Unsupported)
        t.answer = { _, _ -> HttpAnswer(503, """{"detail": "Calendar integration is not available"}""") }
        assertTrue(c.listEvents("a", "b") is ApiResult.Unsupported)
        t.answer = { _, _ -> HttpAnswer(403, """{"detail": "API token missing required scope: calendar:write"}""") }
        assertEquals(ApiResult.Fail(ServerResult.AuthRejected(403)), c.createEvent(NewEvent("x", "2026-10-22", null, true)))
        noLeak()
    }

    @Test fun odysseusTodosClient() = runBlocking {
        val t = FakeTransport { m, _ ->
            when (m) {
                "GET" -> HttpAnswer(200, """{"notes": [{"id": "n1", "title": "Groceries", "type": "checklist", "label": null,
                    "pinned": true, "due_date": "2026-10-24", "updated_at": "2026-10-20T07:00:00Z",
                    "items": [{"text": "Milk", "done": false}, {"text": "Bread", "done": true}], "content": null},
                    {"id": "n2", "title": "Ideas", "type": "note", "pinned": false, "due_date": null, "items": [],
                    "content": "text"}]}""")
                else -> HttpAnswer(200, """{"exit_code": 0, "output": "ok"}""")
            }
        }
        val c = OdysseusTodosClient("http://hub.example.ts.net:7000/", { token }, t, log = { _, msg -> logs += msg })
        val notes = (c.notes() as ApiResult.Ok).value
        assertEquals("http://hub.example.ts.net:7000/api/codex/todos?format=json", t.calls[0].second)
        assertEquals("Bearer $token", t.calls[0].third["Authorization"])
        assertEquals(listOf("n1", "n2"), notes.map { it.id })
        assertEquals(listOf(TodoItem("Milk", false), TodoItem("Bread", true)), notes[0].items)
        assertEquals("2026-10-24", notes[0].dueDate)
        assertTrue(notes[0].pinned)

        assertEquals(ApiResult.Ok(true), c.toggle("n1", 1, false))
        assertEquals("POST" to "http://hub.example.ts.net:7000/api/codex/todos", t.calls.last().first to t.calls.last().second)
        assertEquals(mapOf("action" to "toggle_item", "note_id" to "n1", "index" to 1L, "done" to false),
            Json.parseObject(t.bodies.last()!!))
        t.answer = { _, _ -> HttpAnswer(200, """{"exit_code": 1, "error": "index out of range"}""") }
        assertEquals(ApiResult.Ok(false), c.toggle("n1", 9, true))

        // "No todos": a hub without format=json answers text; a token without todos:read gets 403.
        t.answer = { _, _ -> HttpAnswer(200, "You have 2 notes:\n- Groceries (checklist)") }
        assertEquals(ApiResult.Unsupported(OdysseusTodosClient.OLD_HUB), c.notes())
        t.answer = { _, _ -> HttpAnswer(403, """{"detail": "API token missing required scope: todos:read or todos:write"}""") }
        assertEquals(ApiResult.Unsupported(OdysseusTodosClient.NO_SCOPE), c.notes())
        assertTrue(c.toggle("n1", 0, true) is ApiResult.Unsupported)
        // Other failures are failures.
        t.answer = { _, _ -> HttpAnswer(502, "") }
        assertTrue((c.notes() as ApiResult.Fail).error is ServerResult.Transient)
        assertTrue((c.toggle("n1", 0, true) as ApiResult.Fail).error is ServerResult.Transient)
        t.answer = { _, _ -> throw IOException("connection refused") }
        assertTrue((c.notes() as ApiResult.Fail).error is ServerResult.Transient)
        t.answer = { _, _ -> HttpAnswer(401, """{"error": "Invalid API token"}""") }
        assertEquals(ApiResult.Fail(ServerResult.AuthRejected(401)), c.notes())
        noLeak()
    }

    @Test fun odysseusInsightClient() = runBlocking {
        val t = FakeTransport { _, _ ->
            HttpAnswer(200, """{"date": "2026-10-20", "generated": "2026-10-20T05:30:00Z", "headline": "A light day.",
                "focus": [{"ref": "event:e1", "why": "Bring the letter", "title": "Dentist"}], "coming_up": [], "also": []}""")
        }
        val c = OdysseusInsightClient("http://hub.example.ts.net:7000/", { token }, t, log = { _, msg -> logs += msg })
        val raw = (c.today() as ApiResult.Ok).value
        assertEquals("A light day.", raw["headline"])
        assertEquals("GET" to "http://hub.example.ts.net:7000/api/codex/insight/today", t.calls[0].first to t.calls[0].second)
        assertEquals("Bearer $token", t.calls[0].third["Authorization"])

        // "No insight": left out of the feed, with the PC relay's words.
        val none = listOf(
            HttpAnswer(404, """{"error": "no insight yet"}""") to OdysseusInsightClient.NOT_YET,
            HttpAnswer(404, """{"detail": "Not Found"}""") to OdysseusInsightClient.OLD_HUB, // a hub without the route
            HttpAnswer(405, """{"detail": "Method Not Allowed"}""") to OdysseusInsightClient.OLD_HUB,
            HttpAnswer(403, """{"detail": "API token missing required scope: insight:read"}""") to "token lacks insight:read",
            HttpAnswer(200, "Today you have a dentist appointment.") to OdysseusInsightClient.MALFORMED,
            HttpAnswer(200, """["not", "an", "object"]""") to OdysseusInsightClient.MALFORMED,
            HttpAnswer(400, """{"detail": "bad request"}""") to "Odysseus insight: HTTP 400",
        )
        for ((answer, why) in none) {
            t.answer = { _, _ -> answer }
            assertEquals(ApiResult.Unsupported(why), c.today())
        }
        // Failures fail the feed, like the calendar's.
        t.answer = { _, _ -> HttpAnswer(503, "") }
        assertTrue((c.today() as ApiResult.Fail).error is ServerResult.Transient)
        t.answer = { _, _ -> throw IOException("connection refused") }
        assertTrue((c.today() as ApiResult.Fail).error is ServerResult.Transient)
        t.answer = { _, _ -> HttpAnswer(401, """{"error": "Invalid API token"}""") }
        assertEquals(ApiResult.Fail(ServerResult.AuthRejected(401)), c.today())
        noLeak()
    }

    @Test fun odysseusInsightWeights() = runBlocking {
        val t = FakeTransport { _, _ ->
            HttpAnswer(200, """{"version": 1, "date": "2026-10-20", "weights": {"list:n1": 2, "list:n7": -1}}""")
        }
        val c = OdysseusInsightClient("http://hub.example.ts.net:7000/", { token }, t, log = { _, msg -> logs += msg })
        val raw = (c.weights() as ApiResult.Ok).value
        assertEquals(mapOf("list:n1" to 2L, "list:n7" to -1L), raw["weights"])
        assertEquals("GET" to "http://hub.example.ts.net:7000/api/codex/insight/weights", t.calls[0].first to t.calls[0].second)
        assertEquals("Bearer $token", t.calls[0].third["Authorization"])
        // Anything else keeps the last good table (the feed builder's job): never Ok.
        val kept = listOf(
            HttpAnswer(404, """{"detail": "Not Found"}""") to OdysseusInsightClient.WEIGHTS_OLD_HUB,
            HttpAnswer(405, "") to OdysseusInsightClient.WEIGHTS_OLD_HUB,
            HttpAnswer(403, """{"detail": "API token missing required scope: insight:read"}""") to OdysseusInsightClient.NO_SCOPE,
            HttpAnswer(200, "weights") to OdysseusInsightClient.WEIGHTS_MALFORMED,
        )
        for ((answer, why) in kept) {
            t.answer = { _, _ -> answer }
            assertEquals(ApiResult.Unsupported(why), c.weights())
        }
        t.answer = { _, _ -> HttpAnswer(503, "") }
        assertTrue(c.weights() is ApiResult.Fail)
        t.answer = { _, _ -> throw IOException("connection refused") }
        assertTrue(c.weights() is ApiResult.Fail)
        noLeak()
    }

    /** The real transport against a JDK server: method, headers, body, no redirects. */
    @Test fun urlConnectionTransport() = runBlocking {
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        val seen = mutableListOf<String>()
        server.createContext("/") { ex ->
            seen += "${ex.requestMethod} ${ex.requestURI} ${ex.requestHeaders.getFirst("Authorization")} " +
                ex.requestBody.readBytes().toString(Charsets.UTF_8)
            if (ex.requestURI.path == "/moved") {
                ex.responseHeaders.add("Location", "http://example.invalid/")
                ex.sendResponseHeaders(302, -1)
            } else {
                val b = """{"ok": true}""".toByteArray()
                ex.sendResponseHeaders(200, b.size.toLong())
                ex.responseBody.use { it.write(b) }
            }
            ex.close()
        }
        server.start()
        try {
            val base = "http://127.0.0.1:${server.address.port}"
            val tr = UrlConnectionTransport(2000, 2000)
            val a = tr.exchange("POST", "$base/api/codex/calendar/events", mapOf("Authorization" to "Bearer $token",
                "Content-Type" to "application/json"), """{"summary":"x"}""".toByteArray())
            assertEquals(200, a.code)
            assertEquals("POST /api/codex/calendar/events Bearer $token {\"summary\":\"x\"}", seen.last())
            assertEquals(200, tr.exchange("DELETE", "$base/api/codex/calendar/events/u1", emptyMap(), null).code)
            assertEquals(302, tr.exchange("GET", "$base/moved", emptyMap(), null).code)
            assertTrue(HttpUploader.classify(302, "") is ServerResult.Transient)
        } finally {
            server.stop(0)
        }
    }
}
