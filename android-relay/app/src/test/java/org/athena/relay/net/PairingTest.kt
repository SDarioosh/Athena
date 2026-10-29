package org.athena.relay.net

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

/** Pairing links and redeeming them (fictional codes and tokens). */
class PairingTest {
    private val code = "fHcinhjJod87oT-107TJ"
    private val link = "odysseus-pair:v1?url=https%3A%2F%2Fody.tail1234.ts.net&code=$code"

    private class FakeTransport(var answer: () -> HttpAnswer) : HttpTransport {
        val calls = mutableListOf<Triple<String, String, String?>>()
        override suspend fun exchange(method: String, url: String, headers: Map<String, String>, body: ByteArray?): HttpAnswer {
            calls += Triple(method, url, body?.toString(Charsets.UTF_8))
            return answer()
        }
    }

    @Test fun parsesTheHubsLink() {
        val r = PairingLink.parse("  $link\n")
        assertTrue(r is PairingLink.Result.Ok)
        val p = (r as PairingLink.Result.Ok).link
        assertEquals("https://ody.tail1234.ts.net", p.base)
        assertEquals(code, p.code)
        assertNull(p.warning)
    }

    @Test fun tailscaleHttpHasNoWarningButPublicHttpDoes() {
        val ts = PairingLink.parse("odysseus-pair:v1?url=http%3A%2F%2F100.64.0.10%3A7010&code=$code")
        assertNull((ts as PairingLink.Result.Ok).link.warning)
        assertEquals("http://100.64.0.10:7010", ts.link.base)
        val pub = PairingLink.parse("odysseus-pair:v1?url=http%3A%2F%2Fexample.com&code=$code")
        assertTrue((pub as PairingLink.Result.Ok).link.warning != null)
    }

    @Test fun rejectsOtherText() {
        for (bad in listOf(
            "", "https://ody.ts.net/?code=$code", "odysseus-pair:v2?url=x&code=$code",
            "odysseus-pair:v1?url=https%3A%2F%2Fody.ts.net",               // no code
            "odysseus-pair:v1?url=https%3A%2F%2Fody.ts.net&code=short",    // too short
            "odysseus-pair:v1?url=https%3A%2F%2Fody.ts.net&code=bad%20code1234",
            "odysseus-pair:v1?url=ftp%3A%2F%2Fody.ts.net&code=$code",
            "odysseus-pair:v1?url=https%3A%2F%2Fu%3Ap%40ody.ts.net&code=$code", // user info
            "odysseus-pair:v1?url=%ZZ&code=$code",
        )) {
            assertTrue(bad, PairingLink.parse(bad) is PairingLink.Result.Invalid)
        }
    }

    private fun parsed() = (PairingLink.parse(link) as PairingLink.Result.Ok).link

    @Test fun redeemPostsTheCodeOnceAndReturnsTheToken() = runBlocking {
        val t = FakeTransport {
            HttpAnswer(200, """{"url": "https://ody.tail1234.ts.net", "token": "ody_fictional-0123456789",
                "name": "Phone relay 2026-10-22 10:00", "scopes": ["sound:ingest"]}""")
        }
        val r = PairingClient(t).redeem(parsed())
        assertTrue(r is PairingClient.Result.Ok)
        val paired = (r as PairingClient.Result.Ok).paired
        assertEquals("ody_fictional-0123456789", paired.token)
        assertEquals("https://ody.tail1234.ts.net", paired.base)
        assertEquals("Phone relay 2026-10-22 10:00", paired.name)
        assertFalse(paired.toString().contains("ody_"))
        val (method, url, body) = t.calls.single()
        assertEquals("POST", method)
        assertEquals("https://ody.tail1234.ts.net/api/pair/redeem", url)
        assertEquals("""{"code":"$code"}""", body)
    }

    @Test fun redeemFailures() = runBlocking {
        val t = FakeTransport { HttpAnswer(400, """{"detail": "This pairing code is not valid any more"}""") }
        val client = PairingClient(t)
        fun reason(r: PairingClient.Result) = (r as PairingClient.Result.Failed).reason
        assertTrue(reason(client.redeem(parsed())).contains("not valid any more"))
        t.answer = { HttpAnswer(429, """{"detail": "Too many pairing attempts"}""") }
        assertTrue(reason(client.redeem(parsed())).contains("Too many"))
        t.answer = { HttpAnswer(302, "") }
        assertTrue(reason(client.redeem(parsed())).contains("redirect"))
        t.answer = { HttpAnswer(404, """{"detail": "Not Found"}""") }
        assertTrue(reason(client.redeem(parsed())).contains("update the hub"))
        t.answer = { HttpAnswer(200, """{"token": "not-an-ody-token"}""") }
        assertTrue(reason(client.redeem(parsed())).contains("without a token"))
        t.answer = { HttpAnswer(200, "<html>") }
        assertTrue(reason(client.redeem(parsed())).contains("without JSON"))
        t.answer = { throw IOException("timeout") }
        assertTrue(reason(client.redeem(parsed())).contains("not reachable"))
    }
}
