package org.barvis.sound.relay.net

import com.sun.net.httpserver.HttpServer
import kotlinx.coroutines.runBlocking
import org.barvis.sound.relay.relay.ApiResult
import org.barvis.sound.relay.relay.NewEvent
import org.barvis.sound.relay.relay.ServerResult
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
