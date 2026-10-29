package org.athena.relay.relay

/** Retry delays after the server couldn't be reached: 1, 2, 5, 10, 15 min, then 15 (§6 step 5). */
class Backoff(private val stepsMs: List<Long> = DEFAULT_STEPS_MS) {
    var failures = 0
        private set

    fun nextDelayMs(): Long = stepsMs[minOf(failures, stepsMs.size - 1)].also { failures++ }

    fun reset() {
        failures = 0
    }

    companion object {
        private const val MIN = 60_000L
        val DEFAULT_STEPS_MS = listOf(1 * MIN, 2 * MIN, 5 * MIN, 10 * MIN, 15 * MIN)
    }
}

/**
 * Android silently ignores scans started more than 5 times in 30 s. This keeps us under that
 * (with a little margin on the window).
 */
class ScanThrottle(private val maxStarts: Int = 5, private val windowMs: Long = 32_000) {
    private val starts = ArrayDeque<Long>()

    /** How long to wait before the next start is allowed. */
    @Synchronized
    fun delayBeforeStartMs(nowMs: Long): Long {
        while (starts.isNotEmpty() && nowMs - starts.first() >= windowMs) starts.removeFirst()
        return if (starts.size < maxStarts) 0 else starts.first() + windowMs - nowMs
    }

    @Synchronized
    fun recordStart(nowMs: Long) {
        starts.addLast(nowMs)
    }
}
