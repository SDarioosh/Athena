package org.barvis.sound.relay.net

import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import kotlinx.coroutines.runBlocking
import org.barvis.sound.relay.relay.ChunkUpload
import org.barvis.sound.relay.relay.ServerResult
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.net.InetSocketAddress
import java.util.concurrent.CopyOnWriteArrayList

/** The real uploader against a JDK HTTP server on localhost (fictional token and audio). */
class HttpUploaderTest {
    private val token = "test-token-fictional-0123456789"
    private lateinit var server: HttpServer
    private lateinit var base: String
    private val requests = CopyOnWriteArrayList<Captured>()
    private val logs = CopyOnWriteArrayList<String>()
    private var answer: (HttpExchange) -> Pair<Int, String> = { 200 to """{"ok": true, "stop": false, "duplicate": false}""" }

    class Captured(val method: String, val path: String, val headers: Map<String, List<String>>, val body: ByteArray)

    @Before fun start() {
        server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/") { ex ->
            val body = ex.requestBody.readBytes()
            requests += Captured(ex.requestMethod, ex.requestURI.toString(),
                ex.requestHeaders.mapKeys { it.key.lowercase() }, body)
            val (code, text) = answer(ex)
            val bytes = text.toByteArray()
            ex.responseHeaders.add("Content-Type", "application/json")
            ex.sendResponseHeaders(code, if (bytes.isEmpty()) -1 else bytes.size.toLong())
            if (bytes.isNotEmpty()) ex.responseBody.use { it.write(bytes) }
            ex.close()
        }
        server.start()
        base = "http://127.0.0.1:${server.address.port}/"
    }

    @After fun stop() {
        server.stop(0)
        assertFalse("token leaked into logs", logs.any { it.contains(token) })
    }

    private fun uploader(url: String = base) = HttpUploader(url, { token }, log = { _, m -> logs += m },
        connectTimeoutMs = 2000, readTimeoutMs = 2000)

    private val audio = ByteArray(5000) { (it * 31 % 251).toByte() }
    private fun chunk() = ChunkUpload("twatch-ultra", "3f9a0c11d2e4b507", 2, 1792413296789L, audio)

    /** Minimal multipart/form-data parser: name -> (headers, bytes). */
    private fun parseMultipart(contentType: String, body: ByteArray): Map<String, Pair<String, ByteArray>> {
        val boundary = contentType.substringAfter("boundary=").trim()
        val text = String(body, Charsets.ISO_8859_1) // byte-preserving
        val parts = text.split("--$boundary")
        assertEquals("--", parts.last().trim())
        return parts.drop(1).dropLast(1).associate { raw ->
            val p = raw.removePrefix("\r\n").removeSuffix("\r\n")
            val head = p.substringBefore("\r\n\r\n")
            val content = p.substringAfter("\r\n\r\n")
            val name = Regex("name=\"([^\"]+)\"").find(head)!!.groupValues[1]
            name to (head to content.toByteArray(Charsets.ISO_8859_1))
        }
    }

    @Test fun uploadSendsMultipartWithBearer() = runBlocking {
        val r = uploader().upload(chunk())
        assertEquals(ServerResult.Ok(duplicate = false), r)
        val req = requests.single()
        assertEquals("POST", req.method)
        assertEquals("/v1/ingest/chunk", req.path)
        assertEquals(listOf("Bearer $token"), req.headers["authorization"])
        val ct = req.headers.getValue("content-type").single()
        assertTrue(ct.startsWith("multipart/form-data; boundary="))
        val parts = parseMultipart(ct, req.body)
        assertEquals(setOf("audio", "device", "session_id", "sequence", "timestamp", "codec"), parts.keys)
        fun field(n: String) = String(parts.getValue(n).second, Charsets.UTF_8)
        assertEquals("twatch-ultra", field("device"))
        assertEquals("3f9a0c11d2e4b507", field("session_id"))
        assertEquals("2", field("sequence"))
        assertEquals("2026-10-19T12:34:56.789Z", field("timestamp"))
        assertEquals("ogg", field("codec"))
        val (audioHead, audioBytes) = parts.getValue("audio")
        assertTrue(audioHead.contains("filename=\"3f9a0c11d2e4b507-2.opus\""))
        assertTrue(audioHead.contains("Content-Type: audio/ogg"))
        assertArrayEquals(audio, audioBytes)
        assertTrue(logs.any { it.contains("/v1/ingest/chunk") && it.contains("3f9a0c11d2e4b507") })
    }

    @Test fun noteUploadAddsKindField() = runBlocking {
        val note = ChunkUpload("twatch-ultra", "3f9a0c11d2e4b507", 0, 1792413296789L, audio, kind = "note")
        assertEquals(ServerResult.Ok(duplicate = false), uploader().upload(note))
        val req = requests.single()
        val parts = parseMultipart(req.headers.getValue("content-type").single(), req.body)
        assertEquals(setOf("audio", "device", "session_id", "sequence", "timestamp", "codec", "kind"), parts.keys)
        assertEquals("note", String(parts.getValue("kind").second, Charsets.UTF_8))
        assertArrayEquals(audio, parts.getValue("audio").second)
    }

    private fun throughOdysseus() = HttpUploader(base, { token }, log = { _, m -> logs += m },
        connectTimeoutMs = 2000, readTimeoutMs = 2000, routes = Routes.ODYSSEUS)

    @Test fun throughOdysseusUsesTheHubRoutes() = runBlocking {
        val up = throughOdysseus()
        assertEquals(ServerResult.Ok(duplicate = false), up.upload(chunk()))
        answer = { 200 to """{"ok": true, "stop": true}""" }
        assertEquals(ServerResult.Ok(), up.endSession("3f9a0c11d2e4b507"))
        answer = { 200 to """{"ok": true}""" }
        assertEquals(ServerResult.Ok(), up.health())
        assertEquals(ServerResult.Ok(), up.checkToken())
        assertEquals(listOf("/api/codex/sound/chunk", "/api/codex/sound/sessions/3f9a0c11d2e4b507/end",
            "/api/codex/sound/health", "/api/codex/sound/health"), requests.map { it.path })
        // Every call carries the token there, health included (it checks the token and the module).
        assertTrue(requests.all { it.headers["authorization"] == listOf("Bearer $token") })
        val parts = parseMultipart(requests[0].headers.getValue("content-type").single(), requests[0].body)
        assertEquals("ogg", String(parts.getValue("codec").second, Charsets.UTF_8))
        assertArrayEquals(audio, parts.getValue("audio").second)
    }

    @Test fun throughOdysseusKeepsTheErrorRules() = runBlocking {
        val up = throughOdysseus()
        answer = { 403 to """{"detail": "API token missing required scope: sound:ingest"}""" }
        assertEquals(ServerResult.AuthRejected(403), up.upload(chunk()))
        // Odysseus can't reach Athena: retry later, never a token problem.
        answer = { 502 to """{"detail": "Athena is not reachable"}""" }
        assertTrue(up.upload(chunk()) is ServerResult.Transient)
        answer = { 400 to """{"detail": "codec must be one of ['ogg', 'opus']"}""" }
        val r = up.upload(chunk())
        assertTrue(r is ServerResult.Rejected && r.code == 400 && r.detail.contains("codec"))
    }

    @Test fun duplicateIsOk() = runBlocking {
        answer = { 200 to """{"ok": true, "stop": false, "duplicate": true}""" }
        assertEquals(ServerResult.Ok(duplicate = true), uploader().upload(chunk()))
    }

    @Test fun endSession() = runBlocking {
        answer = { 200 to """{"ok": true, "stop": true}""" }
        assertEquals(ServerResult.Ok(), uploader().endSession("3f9a0c11d2e4b507"))
        val req = requests.single()
        assertEquals("POST", req.method)
        assertEquals("/v1/sessions/3f9a0c11d2e4b507/end", req.path)
        assertEquals(listOf("Bearer $token"), req.headers["authorization"])
    }

    @Test fun healthHasNoToken() = runBlocking {
        answer = { 200 to """{"ok": true}""" }
        assertEquals(ServerResult.Ok(), uploader().health())
        assertEquals("/health", requests.single().path)
        assertNull(requests.single().headers["authorization"])
    }

    @Test fun checkTokenUsesAuthenticatedRead() = runBlocking {
        answer = { 200 to """{"sessions": []}""" }
        assertEquals(ServerResult.Ok(), uploader().checkToken())
        assertEquals("/v1/sessions?limit=1", requests.single().path)
        assertEquals(listOf("Bearer $token"), requests.single().headers["authorization"])
    }

    @Test fun errorClasses() = runBlocking {
        val cases = mapOf(
            401 to ServerResult.AuthRejected(401),
            403 to ServerResult.AuthRejected(403),
            500 to ServerResult.Transient("HTTP 500"),
            503 to ServerResult.Transient("HTTP 503"),
            408 to ServerResult.Transient("HTTP 408"),
            429 to ServerResult.Transient("HTTP 429"),
            422 to ServerResult.Rejected(422, "could not decode audio: x"),
            413 to ServerResult.Rejected(413, "could not decode audio: x"),
            400 to ServerResult.Rejected(400, "could not decode audio: x"),
        )
        for ((code, expected) in cases) {
            answer = { code to """{"detail": "could not decode audio: x"}""" }
            assertEquals("HTTP $code", expected, uploader().upload(chunk()))
        }
    }

    @Test fun unauthorizedWithChallengeIsAuthRejected() = runBlocking {
        // What FastAPI sends for a bad token.
        answer = { ex -> ex.responseHeaders.add("WWW-Authenticate", "Bearer"); 401 to "{\"detail\": \"invalid or missing bearer token\"}" }
        assertEquals(ServerResult.AuthRejected(401), uploader().upload(chunk()))
        assertEquals(ServerResult.AuthRejected(401), uploader().endSession("3f9a0c11d2e4b507"))
        assertEquals(ServerResult.AuthRejected(401), uploader().checkToken())
    }

    @Test fun redirectIsNotFollowed() = runBlocking {
        answer = { ex -> ex.responseHeaders.add("Location", "http://127.0.0.1:1/elsewhere"); 302 to "" }
        assertTrue(uploader().upload(chunk()) is ServerResult.Transient)
        assertEquals(1, requests.size)
    }

    @Test fun okWithoutOkFieldIsNotTrusted() = runBlocking {
        answer = { 200 to "<html>captive portal</html>" }
        assertTrue(uploader().health() is ServerResult.Transient)
    }

    @Test fun unreachableServerIsTransient() = runBlocking {
        server.stop(0)
        val r = uploader().health()
        assertTrue(r.toString(), r is ServerResult.Transient)
    }

    @Test fun baseUrlWithPathPrefix() = runBlocking {
        answer = { 200 to """{"ok": true}""" }
        uploader(base + "barvis/").health()
        assertEquals("/barvis/health", requests.single().path)
    }
}
