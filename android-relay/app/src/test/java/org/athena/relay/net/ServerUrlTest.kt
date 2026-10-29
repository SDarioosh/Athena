package org.athena.relay.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ServerUrlTest {
    private fun ok(u: String) = ServerUrl.check(u) as ServerUrl.Check.Ok

    @Test fun privateHosts() {
        for (h in listOf("10.0.0.5", "172.16.1.1", "172.31.255.255", "192.168.1.20", "100.64.0.1",
                "100.101.102.103", "100.127.255.255", "127.0.0.1", "box.tail1234.ts.net", "server.local",
                "localhost", "[fd7a:115c:a1e0::1]", "fe80::1", "::1")) {
            assertTrue(h, ServerUrl.isPrivateHost(h))
        }
        for (h in listOf("8.8.8.8", "172.32.0.1", "100.128.0.1", "100.63.255.255", "example.com",
                "ts.net.example.com", "192.169.0.1", "2001:db8::1", "10.0.0")) {
            assertFalse(h, ServerUrl.isPrivateHost(h))
        }
    }

    @Test fun httpsNeverWarns() {
        assertNull(ok("https://example.com:8443/").warning)
        assertEquals("https://example.com:8443", ok("https://example.com:8443/").base)
    }

    @Test fun httpWarnsOnlyForPublicHosts() {
        assertNull(ok("http://100.100.1.2:8000").warning)
        assertNull(ok("http://192.168.1.5:8000/").warning)
        assertNotNull(ok("http://example.com:8000").warning)
    }

    @Test fun invalidUrls() {
        for (u in listOf("", "ftp://x", "example.com", "http://user:pw@10.0.0.1", "http://10.0.0.1/?a=b", "http://")) {
            assertTrue(u, ServerUrl.check(u) is ServerUrl.Check.Invalid)
        }
    }

    @Test fun keepsPathPrefix() {
        assertEquals("https://box.tail1234.ts.net/barvis", ok(" https://box.tail1234.ts.net/barvis/ ").base)
    }
}
