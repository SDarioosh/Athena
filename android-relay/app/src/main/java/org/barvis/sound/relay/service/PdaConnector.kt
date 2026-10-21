package org.barvis.sound.relay.service

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.content.Context
import org.barvis.sound.relay.ble.BlePdaLink
import org.barvis.sound.relay.ble.Bonding
import org.barvis.sound.relay.ble.GattClient
import org.barvis.sound.relay.ble.GattStatusException
import org.barvis.sound.relay.net.ConvoHttpApi
import org.barvis.sound.relay.net.OdysseusCalendarClient
import org.barvis.sound.relay.net.OdysseusInsightClient
import org.barvis.sound.relay.net.OdysseusTodosClient
import org.barvis.sound.relay.net.ServerUrl
import org.barvis.sound.relay.protocol.PdaGatt
import org.barvis.sound.relay.protocol.PdaStatus
import org.barvis.sound.relay.protocol.ProtocolException
import org.barvis.sound.relay.protocol.WatchInfo
import org.barvis.sound.relay.relay.CalendarApi
import org.barvis.sound.relay.relay.InsightApi
import org.barvis.sound.relay.relay.Level
import org.barvis.sound.relay.relay.LinkException
import org.barvis.sound.relay.relay.NoCalendar
import org.barvis.sound.relay.relay.NoInsight
import org.barvis.sound.relay.relay.NoTodos
import org.barvis.sound.relay.relay.PdaListener
import org.barvis.sound.relay.relay.PdaOutcome
import org.barvis.sound.relay.relay.PdaSchedule
import org.barvis.sound.relay.relay.PdaStats
import org.barvis.sound.relay.relay.PdaSync
import org.barvis.sound.relay.relay.TodosApi
import org.barvis.sound.relay.store.Settings
import java.text.DateFormat
import java.util.Date

/**
 * The PDA job of [RelayService] (docs/t5-pda-plan.md §3.4): connect, bond, MTU, subscribe,
 * then [PdaSync]. It runs on the same loop as the watch drain, so the two never use the BLE
 * stack at the same time.
 */
@SuppressLint("MissingPermission")
class PdaConnector(private val context: Context, private val settings: Settings) {
    private val log = RelayState.logger
    val schedule = PdaSchedule({ settings.pdaSyncMinutes * 60_000L }, { settings.pdaAutoSync })

    /** One sync with [device]; returns how long the relay loop should pause afterwards. */
    suspend fun sync(device: BluetoothDevice, serverBase: String): Long {
        RelayState.update { it.copy(phase = Phase.PDA_SYNC, detail = "", pdaLine = "Syncing…") }
        val outcome = connectAndSync(device, serverBase)
        val now = System.currentTimeMillis()
        val next = schedule.after(outcome, now)
        fun retry(text: String) = if (schedule.isAuto) ". Retry in $text" else ". Tap Sync T5 now to try again"
        val line = when (outcome) {
            is PdaOutcome.Done -> {
                settings.lastPdaSyncMs = now
                "Synced ${fmt(now)}" + summary(outcome.stats)
            }
            is PdaOutcome.ServerUnavailable -> "Server not reachable: ${outcome.detail}" + retry("${next / 60_000} min")
            is PdaOutcome.AuthRejected -> {
                // The same token as the watch's: the relay loop stops and asks for Settings.
                settings.tokenRejected = true
                "Token rejected (HTTP ${outcome.code})"
            }
            is PdaOutcome.DeviceError -> "PDA problem: ${outcome.detail}" + retry("${next / 60_000} min")
            is PdaOutcome.LinkLost -> "Connection lost: ${outcome.detail}" + retry("${next / 1000} s")
        }
        val err = if (outcome is PdaOutcome.Done) null else "PDA: $line"
        RelayState.update { s -> s.copy(pdaLine = line, lastError = err ?: s.lastError) }
        return PAUSE_AFTER_MS
    }

    private fun summary(s: PdaStats): String {
        val parts = ArrayList<String>()
        if (s.actionsDone + s.actionsRejected > 0) parts += "${s.actionsDone + s.actionsRejected} action(s)"
        if (s.actionsWaiting > 0) parts += "${s.actionsWaiting} waiting"
        if (s.feedsPushed > 0) parts += "feed sent" else parts += "feed unchanged"
        s.calendarNote?.let { parts += "calendar: $it" }
        s.todosNote?.let { parts += "todos: $it" }
        s.insightNote?.let { parts += "insight: $it" }
        return " · " + parts.joinToString(" · ")
    }

    private fun todos(): TodosApi {
        val url = ServerUrl.check(settings.odysseusUrl)
        if (url !is ServerUrl.Check.Ok || !settings.hasOdysseusToken) return NoTodos
        return OdysseusTodosClient(url.base, { settings.odysseusToken() }, log = log)
    }

    private fun insight(): InsightApi {
        val url = ServerUrl.check(settings.odysseusUrl)
        if (url !is ServerUrl.Check.Ok || !settings.hasOdysseusToken) return NoInsight
        return OdysseusInsightClient(url.base, { settings.odysseusToken() }, log = log)
    }

    private fun calendar(): CalendarApi {
        val url = ServerUrl.check(settings.odysseusUrl)
        if (url !is ServerUrl.Check.Ok || !settings.hasOdysseusToken) return NoCalendar
        return OdysseusCalendarClient(url.base, { settings.odysseusToken() },
            settings.odysseusCalendarId.ifBlank { null }, log = log)
    }

    private suspend fun connectAndSync(device: BluetoothDevice, serverBase: String): PdaOutcome {
        val client = GattClient(context, device, log, PdaGatt.PROFILE)
        try {
            client.connect()
            if (!Bonding.ensureBonded(context, device)) {
                return PdaOutcome.LinkLost("pairing failed. Open the pairing window on the T5's Status " +
                    "screen, then try again", PdaStats())
            }
            client.discoverServices()
            if (!client.hasService()) return PdaOutcome.DeviceError("no PDA service", PdaStats())
            val mtu = client.requestMtu(PdaGatt.REQUESTED_MTU)
            client.highPriority(true)
            val link = BlePdaLink(client) { bytes -> publishStatus(bytes) }
            client.enableNotifications(PdaGatt.DATA)
            client.enableNotifications(PdaGatt.STATUS)
            log.log(Level.INFO, "PDA connected, MTU $mtu")
            val sync = PdaSync(
                link = link,
                api = ConvoHttpApi(serverBase, { settings.token() }, log = log),
                calendar = calendar(),
                memory = settings.pdaMemory,
                deviceKey = device.address,
                templates = settings.eventTemplates,
                log = log,
                listener = listener,
                // Manual sync: one pass and BYE, so the watch gets the BLE stack back at once.
                maxStayMs = if (settings.pdaAutoSync) 10 * 60_000L else 0L,
                todos = todos(),
                insight = insight(),
            )
            return sync.run().also { client.highPriority(false) }
        } catch (e: GattStatusException) {
            val hint = if (e.status == 5 || e.status == 15 || e.status == 137) {
                " (pairing problem: remove \"odysseus-pda\" in Android's Bluetooth settings and pair " +
                    "again while the T5's pairing window is open)"
            } else ""
            log.log(Level.WARN, "PDA: " + (e.message ?: "GATT error") + hint)
            return PdaOutcome.LinkLost((e.message ?: "GATT error") + hint, PdaStats())
        } catch (e: LinkException) {
            log.log(Level.WARN, "PDA link: ${e.message}")
            return PdaOutcome.LinkLost(e.message ?: "link lost", PdaStats())
        } finally {
            client.close()
        }
    }

    private fun publishStatus(bytes: ByteArray) {
        try {
            val s = PdaStatus.decode(bytes)
            RelayState.update { it.copy(pdaStatus = s) }
        } catch (e: ProtocolException) {
            log.log(Level.DEBUG, "bad PDA status notification")
        }
    }

    private val listener = object : PdaListener {
        override fun onInfo(info: WatchInfo) = RelayState.update { it.copy(pdaInfo = info) }
        override fun onStatus(status: PdaStatus) = RelayState.update { it.copy(pdaStatus = status) }
        override fun onFeedPushed(version: Long, bytes: Int) =
            RelayState.update { it.copy(pdaLine = "Feed sent (${bytes / 1024} KB)") }
        override fun onAction(type: String, result: String) =
            RelayState.update { it.copy(pdaLine = "Action $type: $result") }
    }

    private fun fmt(ms: Long): String = DateFormat.getTimeInstance(DateFormat.SHORT).format(Date(ms))

    companion object {
        private const val PAUSE_AFTER_MS = 5_000L
    }
}
