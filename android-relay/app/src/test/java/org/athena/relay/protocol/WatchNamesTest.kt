package org.athena.relay.protocol

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** docs/twatch-ble-protocol.md: the watch is `athena-tw`, older firmware `barvis-tw`. */
class WatchNamesTest {
    @Test fun bothNamesAreTheWatch() {
        assertTrue(WatchGatt.isWatchName("athena-tw"))
        assertTrue(WatchGatt.isWatchName("barvis-tw"))
    }

    @Test fun otherNamesAreNot() {
        for (name in listOf(null, "", "odysseus-pda", "Athena-TW", "barvis-tw2")) {
            assertFalse(name.toString(), WatchGatt.isWatchName(name))
        }
    }

    @Test fun hintNamesBoth() {
        assertEquals("\"athena-tw\" (older firmware: \"barvis-tw\")", WatchGatt.NAME_HINT)
    }
}
