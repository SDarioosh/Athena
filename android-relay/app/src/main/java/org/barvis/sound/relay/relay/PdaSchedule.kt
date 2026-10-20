package org.barvis.sound.relay.relay

/**
 * When the next PDA sync is due (docs/t5-pda-plan.md §3.4 steps 3 and 6). The PDA's service
 * UUID is only in the scan filter while a sync is due, so a T5 that advertises all the time
 * doesn't keep the BLE stack from the watch.
 */
class PdaSchedule(
    private val intervalMs: () -> Long,
    private val backoff: Backoff = Backoff(),
    private val linkRetryMs: Long = 60_000,
) {
    /** Due at once after the relay starts. */
    var nextAtMs = 0L
        private set

    fun isDue(nowMs: Long) = nowMs >= nextAtMs

    fun msUntilDue(nowMs: Long) = maxOf(0L, nextAtMs - nowMs)

    /** Records how a sync ended; returns the delay until the next one. */
    fun after(outcome: PdaOutcome, nowMs: Long): Long {
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

    /** E.g. the settings changed: sync again at the next sighting. */
    fun dueNow() {
        nextAtMs = 0
        backoff.reset()
    }
}
