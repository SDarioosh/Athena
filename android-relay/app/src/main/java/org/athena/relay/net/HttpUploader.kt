package org.athena.relay.net

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.athena.relay.relay.ChunkUpload
import org.athena.relay.relay.Level
import org.athena.relay.relay.RelayLog
import org.athena.relay.relay.ServerResult
import org.athena.relay.relay.Timestamps
import org.athena.relay.relay.Uploader
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.net.HttpRetryException
import java.net.HttpURLConnection
import java.net.URL
import java.security.SecureRandom

/**
 * Talks to the owner's Athena server (server/convo_server/api.py), directly or through
 * Odysseus ([routes]), with [HttpURLConnection]. Only [baseUrl] is ever contacted; redirects are not followed so the
 * token can't be sent anywhere else. The token is read from [token] per request and never
 * logged. Timeouts stay below the watch's 30 s idle disconnect (§7.2): an upload that is
 * cut off just gets uploaded again next time and answered `duplicate`.
 */
class HttpUploader(
    baseUrl: String,
    private val token: () -> String,
    private val log: RelayLog = RelayLog.NONE,
    private val connectTimeoutMs: Int = 10_000,
    private val readTimeoutMs: Int = 25_000,
    private val routes: Routes = Routes.DIRECT,
) : Uploader {
    private val base = baseUrl.trimEnd('/')

    override suspend fun health(): ServerResult = request("GET", routes.health, auth = routes.healthNeedsToken).also {
        log.log(Level.INFO, "GET ${routes.health} -> ${describe(it)}")
    }

    /** Settings "test connection": validates the token with a cheap authenticated read. */
    suspend fun checkToken(): ServerResult =
        request("GET", routes.tokenCheck, auth = true, needOkField = false).also {
            log.log(Level.INFO, "GET ${routes.tokenCheck} -> ${describe(it)}")
        }

    override suspend fun upload(chunk: ChunkUpload): ServerResult {
        val boundary = "----barvis" + ByteArray(12).also { SecureRandom().nextBytes(it) }
            .joinToString("") { "%02x".format(it.toInt() and 0xFF) }
        val body = multipart(boundary, chunk)
        val r = request("POST", routes.chunk, auth = true,
            body = body, contentType = "multipart/form-data; boundary=$boundary")
        log.log(Level.INFO, "POST ${routes.chunk} session ${chunk.sessionId} seq ${chunk.sequence}" +
            (if (chunk.kind != null) " kind ${chunk.kind}" else "") + " " +
            "(${chunk.audio.size} B) -> ${describe(r)}")
        return r
    }

    override suspend fun endSession(sessionId: String): ServerResult {
        require(SESSION_ID.matches(sessionId)) { "bad session id" }
        val path = routes.end(sessionId)
        val r = request("POST", path, auth = true, body = ByteArray(0))
        log.log(Level.INFO, "POST $path -> ${describe(r)}")
        return r
    }

    private fun multipart(boundary: String, c: ChunkUpload): ByteArray {
        val out = ByteArrayOutputStream(c.audio.size + 1024)
        fun line(s: String) = out.write((s + "\r\n").toByteArray(Charsets.UTF_8))
        fun field(name: String, value: String) {
            line("--$boundary")
            line("Content-Disposition: form-data; name=\"$name\"")
            line("")
            line(value)
        }
        field("device", c.device)
        field("session_id", c.sessionId)
        field("sequence", c.sequence.toString())
        field("timestamp", Timestamps.iso(c.timestampMs))
        field("codec", "ogg")
        // Omitted for ordinary chunks so their uploads stay byte-identical to before.
        c.kind?.let { field("kind", it) }
        line("--$boundary")
        line("Content-Disposition: form-data; name=\"audio\"; filename=\"${c.sessionId}-${c.sequence}.opus\"")
        line("Content-Type: audio/ogg")
        line("")
        out.write(c.audio)
        line("")
        line("--$boundary--")
        return out.toByteArray()
    }

    private suspend fun request(
        method: String,
        path: String,
        auth: Boolean,
        body: ByteArray? = null,
        contentType: String? = null,
        needOkField: Boolean = true,
    ): ServerResult = withContext(Dispatchers.IO) {
        var conn: HttpURLConnection? = null
        try {
            conn = (URL(base + path).openConnection() as HttpURLConnection).apply {
                requestMethod = method
                connectTimeout = connectTimeoutMs
                readTimeout = readTimeoutMs
                instanceFollowRedirects = false
                useCaches = false
                setRequestProperty("Accept", "application/json")
                setRequestProperty("User-Agent", "barvis-relay")
                if (auth) setRequestProperty("Authorization", "Bearer ${token()}")
                if (body != null) {
                    doOutput = true
                    if (contentType != null) setRequestProperty("Content-Type", contentType)
                    setFixedLengthStreamingMode(body.size)
                }
            }
            if (body != null) conn.outputStream.use { it.write(body) }
            val code = conn.responseCode
            val text = readSmall(if (code < 400) conn.inputStream else conn.errorStream)
            classify(code, text, needOkField)
        } catch (e: HttpRetryException) {
            // Some HttpURLConnection implementations throw this for a 401 in streaming mode.
            classify(e.responseCode(), "", needOkField)
        } catch (e: IOException) {
            ServerResult.Transient(("network error: " + (e.message ?: e.javaClass.simpleName)).take(200))
        } finally {
            conn?.disconnect()
        }
    }

    companion object {
        private val SESSION_ID = Regex("[A-Za-z0-9._:-]{1,128}")
        private val OK_TRUE = Regex("\"ok\"\\s*:\\s*true")
        private val DUP_TRUE = Regex("\"duplicate\"\\s*:\\s*true")
        private val DETAIL = Regex("\"detail\"\\s*:\\s*\"((?:[^\"\\\\]|\\\\.)*)\"")

        /** §6 step 6.5 error classes. */
        fun classify(code: Int, body: String, needOkField: Boolean = true): ServerResult = when {
            code in 200..299 ->
                if (!needOkField || OK_TRUE.containsMatchIn(body)) ServerResult.Ok(DUP_TRUE.containsMatchIn(body))
                else ServerResult.Transient("HTTP $code without \"ok\": true (is this the Athena server?)")
            code == 401 || code == 403 -> ServerResult.AuthRejected(code)
            code == 408 || code == 429 || code >= 500 -> ServerResult.Transient("HTTP $code")
            code in 300..399 -> ServerResult.Transient("HTTP $code redirect (not followed; check the URL)")
            code in 400..499 -> ServerResult.Rejected(code, DETAIL.find(body)?.groupValues?.get(1)?.take(200) ?: "")
            else -> ServerResult.Transient("HTTP $code")
        }

        fun describe(r: ServerResult): String = when (r) {
            is ServerResult.Ok -> if (r.duplicate) "ok (duplicate)" else "ok"
            is ServerResult.Transient -> r.detail
            is ServerResult.AuthRejected -> "HTTP ${r.code} token rejected"
            is ServerResult.Rejected -> "HTTP ${r.code} ${r.detail}".trim()
        }

        private fun readSmall(stream: InputStream?): String {
            if (stream == null) return ""
            return stream.use { s ->
                val buf = ByteArray(16 * 1024)
                var n = 0
                while (n < buf.size) {
                    val r = s.read(buf, n, buf.size - n)
                    if (r < 0) break
                    n += r
                }
                String(buf, 0, n, Charsets.UTF_8)
            }
        }
    }
}
