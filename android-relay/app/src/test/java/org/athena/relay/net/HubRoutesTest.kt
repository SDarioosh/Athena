package org.athena.relay.net

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** The fallback rules of [HubRoutes], with a fake hub per base URL (status per path). */
class HubRoutesTest {
    private val sent = mutableListOf<String>()

    private suspend fun HubRoutes.send(base: String, path: String, answer: (String) -> Int?): Int? =
        call(base, path, { it }) { p -> sent += "$base$p"; answer(p) }

    private val oldHub: (String) -> Int? = { p -> if (p.startsWith("/api/codex/athena/")) 404 else 200 }
    private val newHub: (String) -> Int? = { 200 }

    @Test fun eachHubIsRememberedOnItsOwn() = runBlocking {
        val hub = HubRoutes()
        assertEquals(200, hub.send("http://old:7000", "/api/codex/athena/health", oldHub))
        assertEquals(200, hub.send("http://new:7000", "/api/codex/athena/health", newHub))
        assertEquals(200, hub.send("http://old:7000", "/api/codex/athena/chunk", oldHub))
        assertEquals(200, hub.send("http://new:7000", "/api/codex/athena/chunk", newHub))
        assertEquals(listOf(
            "http://old:7000/api/codex/athena/health",
            "http://old:7000/api/codex/sound/health",
            "http://new:7000/api/codex/athena/health",
            "http://old:7000/api/codex/sound/chunk",
            "http://new:7000/api/codex/athena/chunk",
        ), sent)
        assertEquals(true, hub.usesOld("http://old:7000"))
        assertEquals(false, hub.usesOld("http://new:7000/"))
    }

    @Test fun undecidedAnswersAreRetriedLater() = runBlocking {
        val hub = HubRoutes()
        for (code in listOf(null, 401, 408, 429, 500, 503)) {
            assertEquals(code, hub.send("http://h", "/api/codex/athena/health") { code })
            assertNull("after $code", hub.usesOld("http://h"))
        }
        assertEquals(6, sent.size) // none of them was sent twice
        // a 404 there, then an old route that is down too: still undecided
        assertEquals(503, hub.send("http://h", "/api/codex/athena/health") { p -> if ("athena" in p) 404 else 503 })
        assertNull(hub.usesOld("http://h"))
        // a 404 on both: the answer is the old route's 404, nothing remembered
        assertEquals(404, hub.send("http://h", "/api/codex/athena/conversations/x") { 404 })
        assertNull(hub.usesOld("http://h"))
    }

    @Test fun answersThatShowTheRoute() = runBlocking {
        for (code in listOf(200, 204, 400, 403, 405, 409, 422)) {
            val hub = HubRoutes()
            hub.send("http://h", "/api/codex/athena/health") { code }
            assertEquals("after $code", false, hub.usesOld("http://h"))
        }
    }

    @Test fun otherPathsAreSentAsTheyAre() = runBlocking {
        val hub = HubRoutes()
        for (p in listOf("/v1/ingest/chunk", "/health", "/api/codex/sound/health", "/api/codex/todos",
                "/api/codex/athena")) {
            assertEquals(404, hub.send("http://h", p) { 404 })
        }
        assertEquals(listOf("http://h/v1/ingest/chunk", "http://h/health", "http://h/api/codex/sound/health",
            "http://h/api/codex/todos", "http://h/api/codex/athena"), sent)
        assertNull(hub.usesOld("http://h"))
    }
}
