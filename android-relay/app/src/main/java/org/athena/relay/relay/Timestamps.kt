package org.athena.relay.relay

import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

object Timestamps {
    private val ISO_MS: DateTimeFormatter =
        DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'").withZone(ZoneOffset.UTC)

    /** UTC epoch ms -> `2026-10-19T12:34:56.789Z` (§6 step 6.2). */
    fun iso(epochMs: Long): String = ISO_MS.format(Instant.ofEpochMilli(epochMs))
}
