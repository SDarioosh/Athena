package org.barvis.sound.relay.relay

/**
 * When the next PDA sync is due (docs/t5-pda-plan.md §3.4 steps 3 and 6). The PDA's service
 * UUID is only in the scan filter while a sync is due, so a T5 that advertises all the time
 * doesn't keep the BLE stack from the watch.
 *
 * Two modes, chosen by [auto]:
 * - **automatic** (the app's default): due every [intervalMs], with backoff after failures,
 *   and at once after [request].
 * - **manual** (auto sync off, e.g. the T5 is off or away): a sync is due only after
 *   [request] ("Sync T5 now"), until it runs or the request expires. Nothing is retried on
 *   its own.
 */
class PdaSchedule(
    private val intervalMs: () -> Long,
    private val auto: () -> Boolean = { true },
    private val backoff: Backoff = Backoff(),
    private val linkRetryMs: Long = 60_000,
) {
    /** Automatic mode: due at once after the relay starts. */
    var nextAtMs = 0L
        private set

    /** A manual request is pending until this time (0 = none). */
    private var requestedUntilMs = 0L

    val isAuto: Boolean get() = auto()

    fun isDue(nowMs: Long) = nowMs < requestedUntilMs || (auto() && nowMs >= nextAtMs)

    fun msUntilDue(nowMs: Long): Long = when {
        isDue(nowMs) -> 0L
        auto() -> nextAtMs - nowMs
        else -> Long.MAX_VALUE
    }

    /** "Sync T5 now": due until the T5 is found or [windowMs] has passed. */
    fun request(nowMs: Long, windowMs: Long) {
        requestedUntilMs = nowMs + windowMs
        backoff.reset()
    }

    /** True once when a request expired without a sync (the T5 wasn't found); clears it. */
    fun requestExpired(nowMs: Long): Boolean {
        if (requestedUntilMs == 0L || nowMs < requestedUntilMs) return false
        requestedUntilMs = 0L
        return true
    }

    /** Records how a sync ended; returns the delay until the next automatic one (0 in manual mode). */
    fun after(outcome: PdaOutcome, nowMs: Long): Long {
        requestedUntilMs = 0L
        if (!auto()) return 0L
        val d = when (outcome) {
            is PdaOutcome.Done -> {
                backoff.reset()
                intervalMs()
            }
            is PdaOutcome.ServerUnavailable, is PdaOutcome.DeviceError -> backoff.nextDelayMs()
            is PdaOutcome.LinkLost -> linkRetryMs
            is PdaOutcome.AuthRejected -> intervalMs()
        }
        nextAtMs = nowMs + d
        return d
    }

    /** E.g. the settings changed: sync again at the next sighting (automatic mode). */
    fun dueNow() {
        nextAtMs = 0
        backoff.reset()
    }
}
