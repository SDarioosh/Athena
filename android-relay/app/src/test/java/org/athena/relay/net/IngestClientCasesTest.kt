package org.athena.relay.net

import com.sun.net.httpserver.HttpServer
import kotlinx.coroutines.runBlocking
import org.athena.relay.relay.ChunkUpload
import org.athena.relay.relay.ServerResult
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.net.InetSocketAddress
import java.net.ServerSocket

/**
 * The real uploader against every row of docs/api/ingest-client-cases.json, the table of
 * server answers shared by every uploader (docs/api/ingest-client.md). Each row is served by
 * a JDK HTTP server on localhost as the answer to one chunk upload. [ServerResult] maps onto
 * the table's classes: Ok = ok, Transient = retry, AuthRejected = stop_uploads, Rejected =
 * drop (the relay leaves the chunk on the watch unacknowledged and skips it for this
 * connection). Status 0 (no HTTP answer) is a port nothing listens on. Fictional data only.
 */
class IngestClientCasesTest {
    private val token = "test-token-fictional-cases-4f2a"
    private lateinit var server: HttpServer
    @Volatile private var status = 200
    @Volatile private var body: Any? = null

    @Before fun start() {
        server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/") { ex ->
            ex.requestBody.readBytes()
            val b = body
            val text = when (b) {
                null -> ""
                is String -> b
                else -> Json.write(b)
            }
            if (status in 300..399) ex.responseHeaders.add("Location", "http://127.0.0.1:1/elsewhere")
            if (b != null) ex.responseHeaders.add("Content-Type", if (b is String) "text/html" else "application/json")
            val bytes = text.toByteArray()
            ex.sendResponseHeaders(status, if (bytes.isEmpty()) -1 else bytes.size.toLong())
            if (bytes.isNotEmpty()) ex.responseBody.use { it.write(bytes) }
            ex.close()
        }
        server.start()
    }

    @After fun stop() {
        server.stop(0)
    }

    private fun uploader(port: Int) = HttpUploader("http://127.0.0.1:$port/", { token },
        connectTimeoutMs = 2000, readTimeoutMs = 2000)

    private val chunk = ChunkUpload("twatch-ultra", "3f9a0c11d2e4b507", 2, 1792413296789L, ByteArray(64))

    private fun expectOf(r: ServerResult): String = when (r) {
        is ServerResult.Ok -> "ok"
        is ServerResult.Transient -> "retry"
        is ServerResult.AuthRejected -> "stop_uploads"
        is ServerResult.Rejected -> "drop"
    }

    @Suppress("UNCHECKED_CAST")
    private fun cases(): List<Map<String, Any?>> {
        // Unit tests run in the module directory (android-relay/app).
        val file = File("../../docs/api/ingest-client-cases.json")
        assertTrue("missing ${file.absolutePath}", file.isFile)
        return Json.parse(file.readText()) as List<Map<String, Any?>>
    }

    @Test fun everyCase() = runBlocking {
        val rows = cases()
        assertEquals(setOf("ok", "retry", "drop", "stop_uploads"), rows.map { it["expect"] }.toSet())
        for (row in rows) {
            val code = (row["status"] as Long).toInt()
            val port = if (code == 0) {
                ServerSocket(0).use { it.localPort } // closed again: nothing answers there
            } else {
                status = code
                body = row["body"]
                server.address.port
            }
            val r = uploader(port).upload(chunk)
            assertEquals("HTTP $code ${row["body"]} -> $r", row["expect"], expectOf(r))
        }
    }
}
