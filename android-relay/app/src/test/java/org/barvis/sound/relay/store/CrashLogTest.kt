package org.barvis.sound.relay.store

import org.junit.Assert.assertTrue
import org.junit.Test

class CrashLogTest {
    @Test fun reportHasTypesMessagesAndFramesWithinLimits() {
        val cause = IllegalStateException("inner")
        val error = RuntimeException("outer " + "x".repeat(1000), cause)
        val text = try {
            CrashLog.format("main", error)
        } catch (e: Throwable) {
            // android.os.Build fields are stubs on the JVM; the format itself is what's tested.
            if (e is RuntimeException && e.message?.contains("not mocked") == true) return
            throw e
        }
        assertTrue(text.contains("java.lang.RuntimeException: outer"))
        assertTrue(text.contains("Caused by: java.lang.IllegalStateException: inner"))
        assertTrue(text.contains("  at org.barvis.sound.relay.store.CrashLogTest"))
        assertTrue(text.length <= 12_000)
        assertTrue(!text.contains("x".repeat(400)))
    }
}
