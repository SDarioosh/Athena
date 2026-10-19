package org.barvis.sound.relay.service

import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import org.barvis.sound.relay.protocol.WatchInfo
import org.barvis.sound.relay.protocol.WatchStatus
import org.barvis.sound.relay.relay.Level
import org.barvis.sound.relay.relay.RelayLog
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

enum class Phase(val label: String) {
    STOPPED("Stopped"),
    SCANNING("Scanning for the watch"),
    CONNECTING("Connecting"),
    DRAINING("Uploading"),
    IDLE("Idle"),
    WAITING("Waiting to retry"),
    ERROR("Needs attention"),
}

data class RelaySnapshot(
    val phase: Phase = Phase.STOPPED,
    val detail: String = "",
    val info: WatchInfo? = null,
    val status: WatchStatus? = null,
    val statusAtMs: Long = 0,
    val uploadedThisRun: Int = 0,
    val skippedThisRun: Int = 0,
    val lastError: String? = null,
)

/** Process-wide state the service writes and the UI reads. Holds no token and no audio. */
object RelayState {
    private val _state = MutableStateFlow(RelaySnapshot())
    val state: StateFlow<RelaySnapshot> = _state

    private const val MAX_LOG = 300
    private val _log = MutableStateFlow<List<String>>(emptyList())
    val log: StateFlow<List<String>> = _log

    private val timeFmt = DateTimeFormatter.ofPattern("MM-dd HH:mm:ss").withZone(ZoneId.systemDefault())

    fun update(f: (RelaySnapshot) -> RelaySnapshot) = _state.update(f)

    /** The app's [RelayLog]: logcat plus the in-app log view. */
    val logger = RelayLog { level, message ->
        val priority = when (level) {
            Level.DEBUG -> Log.DEBUG
            Level.INFO -> Log.INFO
            Level.WARN -> Log.WARN
            Level.ERROR -> Log.ERROR
        }
        Log.println(priority, "BarvisRelay", message)
        if (level != Level.DEBUG) {
            val line = "${timeFmt.format(Instant.now())} ${level.name.first()} $message"
            _log.update { (it + line).takeLast(MAX_LOG) }
        }
    }
}
