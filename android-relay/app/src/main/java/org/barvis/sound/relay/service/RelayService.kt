package org.barvis.sound.relay.service

import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.barvis.sound.relay.R
import org.barvis.sound.relay.ble.BleWatchLink
import org.barvis.sound.relay.ble.Bonding
import org.barvis.sound.relay.ble.GattClient
import org.barvis.sound.relay.ble.GattStatusException
import org.barvis.sound.relay.ble.Scanner
import org.barvis.sound.relay.net.HttpUploader
import org.barvis.sound.relay.net.ServerUrl
import org.barvis.sound.relay.protocol.ChunkEntry
import org.barvis.sound.relay.protocol.PdaGatt
import org.barvis.sound.relay.protocol.ProtocolException
import org.barvis.sound.relay.protocol.WatchGatt
import org.barvis.sound.relay.protocol.WatchInfo
import org.barvis.sound.relay.protocol.WatchStatus
import org.barvis.sound.relay.relay.Backoff
import org.barvis.sound.relay.relay.DrainListener
import org.barvis.sound.relay.relay.DrainOutcome
import org.barvis.sound.relay.relay.DrainSession
import org.barvis.sound.relay.relay.Level
import org.barvis.sound.relay.relay.LinkException
import org.barvis.sound.relay.relay.PartialReads
import org.barvis.sound.relay.relay.ScanThrottle
import org.barvis.sound.relay.store.Settings
import org.barvis.sound.relay.ui.MainActivity
import java.text.DateFormat
import java.util.Date
import kotlin.coroutines.cancellation.CancellationException

/**
 * Foreground service (type connectedDevice) that runs the relay loop:
 * scan -> connect -> bond -> MTU/PHY/priority -> subscribe -> [DrainSession] -> back to scanning
 * (docs/twatch-ble-protocol.md §6).
 */
@SuppressLint("MissingPermission")
class RelayService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var loop: Job? = null
    private lateinit var settings: Settings
    private val log = RelayState.logger
    private val partials = PartialReads()
    private val pda by lazy { PdaConnector(this, settings) }
    /** "Sync T5 now" was tapped: cut the running scan short so the next one looks for the T5. */
    private val pdaWake = Channel<Unit>(Channel.CONFLATED)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        settings = Settings(this)
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            settings.relayEnabled = false
            stopRelay()
            return START_NOT_STICKY
        }
        // Started with startForegroundService(): startForeground() must come first, even when
        // we are about to stop again.
        try {
            startInForeground()
        } catch (e: RuntimeException) {
            // SecurityException / ForegroundServiceStartNotAllowedException.
            log.log(Level.ERROR, "can't start in the foreground: ${e.javaClass.simpleName}")
            RelayState.update { it.copy(phase = Phase.ERROR, lastError = "Android didn't allow the relay to start; open the app") }
            stopSelf()
            return START_NOT_STICKY
        }
        if (!settings.relayEnabled || !Permissions.hasAll(this)) {
            // E.g. a sticky restart after the owner turned the relay off.
            stopRelay()
            return START_NOT_STICKY
        }
        if (loop?.isActive != true) {
            RelayState.update { RelaySnapshot(phase = Phase.SCANNING) }
            loop = scope.launch { runLoop() }
            scope.launch { RelayState.state.collectLatest { updateNotification(it) } }
        }
        if (intent?.action == ACTION_PDA_SYNC && settings.pdaEnabled) {
            pda.schedule.request(System.currentTimeMillis(), PDA_REQUEST_WINDOW_MS)
            RelayState.update { it.copy(pdaLine = "Looking for the T5…") }
            pdaWake.trySend(Unit)
        }
        return START_STICKY
    }

    override fun onDestroy() {
        scope.cancel()
        RelayState.update { if (it.phase == Phase.ERROR) it else it.copy(phase = Phase.STOPPED, detail = "") }
        super.onDestroy()
    }

    private fun stopRelay() {
        loop?.cancel()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    // ------------------------------------------------------------------ the loop

    /** Runs [scan], or returns null as soon as "Sync T5 now" is tapped. */
    private suspend fun scanUnlessWoken(scan: suspend () -> Scanner.Hit?): Scanner.Hit? = coroutineScope {
        val job = async { scan() }
        val wake = launch {
            pdaWake.receive()
            job.cancel()
        }
        try {
            job.await()
        } catch (e: CancellationException) {
            if (!isActive) throw e // the service is stopping
            null
        } finally {
            wake.cancel()
        }
    }

    private suspend fun runLoop() {
        val backoff = Backoff()
        val throttle = ScanThrottle()
        var linkFailures = 0
        log.log(Level.INFO, "relay started")
        while (scope.isActive) {
            val url = ServerUrl.check(settings.serverUrl)
            if (url !is ServerUrl.Check.Ok || !settings.hasToken) {
                fail("Set the server URL and token in Settings")
                return
            }
            if (settings.tokenRejected) {
                fail("The server rejected the token. Fix it in Settings.")
                return
            }
            val adapter = (getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager).adapter
            if (adapter == null || !adapter.isEnabled) {
                setPhase(Phase.WAITING, "Bluetooth is off")
                delay(15_000)
                continue
            }

            setPhase(Phase.SCANNING, "")
            val now = System.currentTimeMillis()
            if (pda.schedule.requestExpired(now)) {
                RelayState.update {
                    it.copy(pdaLine = "T5 not found. Is it nearby and awake (tap its screen)? Then tap Sync T5 now again")
                }
            }
            // Manual mode (default): the T5 is looked for only after "Sync T5 now", and then
            // alone, until the request expires. Automatic mode: it is taken when its sync is
            // due, or at once when its scan response says the outbox has entries (t5-pda-plan
            // §3.4 step 6, §3.5); otherwise it is ignored.
            val pdaOn = settings.pdaEnabled
            val auto = pda.schedule.isAuto
            val pdaDue = pdaOn && pda.schedule.isDue(now)
            val services = when {
                !pdaOn -> listOf(WatchGatt.SERVICE)
                !auto && pdaDue -> listOf(PdaGatt.SERVICE)
                !auto -> listOf(WatchGatt.SERVICE)
                else -> listOf(WatchGatt.SERVICE, PdaGatt.SERVICE)
            }
            val window = when {
                pdaOn && !auto && pdaDue -> PDA_REQUEST_WINDOW_MS
                pdaOn && auto && !pdaDue -> pda.schedule.msUntilDue(now).coerceIn(5_000L, SCAN_WINDOW_MS)
                else -> SCAN_WINDOW_MS
            }
            val hit = try {
                scanUnlessWoken {
                    Scanner(adapter, throttle, log).scanFor(window, services) { h ->
                        h.service != PdaGatt.SERVICE || pdaDue || (auto && PdaGatt.outboxWaiting(h.serviceData))
                    }
                }
            } catch (e: Scanner.ScanFailed) {
                log.log(Level.WARN, e.message ?: "scan failed")
                RelayState.update { it.copy(lastError = e.message) }
                delay(30_000)
                null
            } ?: continue
            if (hit.service == PdaGatt.SERVICE) {
                delay(pda.sync(hit.device, url.base))
                continue
            }
            val device = hit.device

            val uploader = HttpUploader(url.base, { settings.token() }, log)
            val outcome = connectAndDrain(device, uploader)
            val pause: Long = when (outcome) {
                is DrainOutcome.Done -> {
                    backoff.reset()
                    linkFailures = 0
                    settings.lastSyncMs = System.currentTimeMillis()
                    if (outcome.skipped > 0) {
                        // Skipped chunks stay on the watch, which keeps advertising for them.
                        // Don't reconnect every few seconds just to skip them again.
                        setPhase(Phase.IDLE, "${outcome.skipped} chunk(s) skipped; next try in 15 min")
                        SKIPPED_COOLDOWN_MS
                    } else {
                        setPhase(Phase.IDLE, "Synced")
                        DONE_COOLDOWN_MS
                    }
                }
                is DrainOutcome.ServerUnavailable -> {
                    linkFailures = 0
                    val d = backoff.nextDelayMs()
                    val msg = "Server not reachable: ${outcome.detail}"
                    RelayState.update { it.copy(lastError = msg) }
                    setPhase(Phase.WAITING, "$msg. Retry in ${d / 60_000} min")
                    d
                }
                is DrainOutcome.AuthRejected -> {
                    settings.tokenRejected = true
                    fail("Token rejected (HTTP ${outcome.code}). Fix it in Settings.")
                    return
                }
                is DrainOutcome.WatchError -> {
                    val d = backoff.nextDelayMs()
                    RelayState.update { it.copy(lastError = "Watch: ${outcome.detail}") }
                    setPhase(Phase.WAITING, "Watch problem. Retry in ${d / 60_000} min")
                    d
                }
                is DrainOutcome.LinkLost -> {
                    linkFailures++
                    RelayState.update { it.copy(lastError = "Bluetooth: ${outcome.detail}") }
                    val d = minOf(5_000L shl minOf(linkFailures - 1, 6), 5 * 60_000L)
                    setPhase(Phase.WAITING, "Connection lost. Retry in ${d / 1000} s")
                    d
                }
            }
            delay(pause)
        }
    }

    private suspend fun connectAndDrain(device: BluetoothDevice, uploader: HttpUploader): DrainOutcome {
        setPhase(Phase.CONNECTING, "")
        val client = GattClient(this, device, log)
        client.onStatus = { bytes ->
            try {
                val s = WatchStatus.decode(bytes)
                RelayState.update { it.copy(status = s, statusAtMs = System.currentTimeMillis()) }
            } catch (e: ProtocolException) {
                log.log(Level.DEBUG, "bad status notification")
            }
        }
        try {
            client.connect()
            if (!Bonding.ensureBonded(this, device)) {
                return DrainOutcome.LinkLost("pairing failed. Long-press BOOT on the watch to open " +
                    "its sync window, then try again", 0)
            }
            client.discoverServices()
            if (!client.hasWatchService()) return DrainOutcome.WatchError("no odysseus-sound service", 0)
            val mtu = client.requestMtu(WatchGatt.REQUESTED_MTU)
            if (mtu < WatchGatt.MIN_MTU) return DrainOutcome.WatchError("MTU $mtu is below ${WatchGatt.MIN_MTU}", 0)
            client.preferPhy2M()
            client.highPriority(true)
            client.enableNotifications(WatchGatt.DATA)
            client.enableNotifications(WatchGatt.STATUS)
            log.log(Level.INFO, "connected, MTU $mtu")

            val session = DrainSession(
                link = BleWatchLink(client),
                uploader = uploader,
                anchors = settings.anchors,
                partials = partials,
                log = log,
                listener = listener,
            )
            return session.run().also { client.highPriority(false) }
        } catch (e: GattStatusException) {
            val hint = if (e.status == 5 || e.status == 15 || e.status == 137) {
                " (pairing problem: if the watch was paired outside its sync window, remove " +
                    "\"barvis-tw\" in Android's Bluetooth settings and pair again during the window)"
            } else ""
            log.log(Level.WARN, (e.message ?: "GATT error") + hint)
            return DrainOutcome.LinkLost((e.message ?: "GATT error") + hint, 0)
        } catch (e: LinkException) {
            log.log(Level.WARN, "link: ${e.message}")
            return DrainOutcome.LinkLost(e.message ?: "link lost", 0)
        } finally {
            client.close()
        }
    }

    private val listener = object : DrainListener {
        override fun onInfo(info: WatchInfo) = RelayState.update { it.copy(info = info) }
        override fun onStatus(status: WatchStatus) =
            RelayState.update { it.copy(status = status, statusAtMs = System.currentTimeMillis()) }
        override fun onDraining() = setPhase(Phase.DRAINING, "")
        override fun onUploaded(entry: ChunkEntry, duplicate: Boolean) {
            settings.totalUploaded = settings.totalUploaded + 1
            RelayState.update { it.copy(uploadedThisRun = it.uploadedThisRun + 1) }
        }
        override fun onSkipped(entry: ChunkEntry, reason: String) = RelayState.update {
            it.copy(skippedThisRun = it.skippedThisRun + 1, lastError = "Chunk ${entry.chunkId} skipped: $reason")
        }
    }

    private fun setPhase(phase: Phase, detail: String) = RelayState.update { it.copy(phase = phase, detail = detail) }

    private fun fail(message: String) {
        log.log(Level.ERROR, message)
        RelayState.update { it.copy(phase = Phase.ERROR, detail = message, lastError = message) }
        // Leave a normal (dismissable) notification behind, then stop.
        val nm = getSystemService(NotificationManager::class.java)
        nm.notify(NOTIFY_ERROR, buildNotification(message, ongoing = false))
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    // ------------------------------------------------------------------ notification

    private fun createChannel() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, getString(R.string.channel_name), NotificationManager.IMPORTANCE_LOW).apply {
                description = getString(R.string.channel_description)
                setShowBadge(false)
            }
        )
    }

    private fun buildNotification(text: String, ongoing: Boolean = true): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val b = Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(ongoing)
            .setOnlyAlertOnce(true)
            .setShowWhen(false)
        if (ongoing) {
            val stop = PendingIntent.getService(this, 1, Intent(this, RelayService::class.java).setAction(ACTION_STOP),
                PendingIntent.FLAG_IMMUTABLE)
            b.addAction(Notification.Action.Builder(null, getString(R.string.stop_relay), stop).build())
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            b.setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE)
        }
        return b.build()
    }

    private fun notificationText(s: RelaySnapshot): String {
        val parts = mutableListOf(s.phase.label)
        if (s.detail.isNotEmpty()) parts += s.detail
        s.status?.let { parts += "watch ${it.queuedChunks} queued" }
        val last = settings.lastSyncMs
        if (last > 0) parts += "last sync " + DateFormat.getTimeInstance(DateFormat.SHORT).format(Date(last))
        return parts.joinToString(" · ")
    }

    private fun startInForeground() {
        val n = buildNotification(notificationText(RelayState.state.value))
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFY_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        } else {
            startForeground(NOTIFY_ID, n)
        }
    }

    private fun updateNotification(s: RelaySnapshot) {
        if (s.phase == Phase.STOPPED || s.phase == Phase.ERROR) return
        getSystemService(NotificationManager::class.java).notify(NOTIFY_ID, buildNotification(notificationText(s)))
    }

    companion object {
        private const val CHANNEL = "relay"
        private const val NOTIFY_ID = 1
        private const val NOTIFY_ERROR = 2
        private const val ACTION_STOP = "org.barvis.sound.relay.STOP"
        private const val ACTION_PDA_SYNC = "org.barvis.sound.relay.PDA_SYNC"

        /** How long "Sync T5 now" looks for the T5 before giving up. */
        private const val PDA_REQUEST_WINDOW_MS = 60_000L

        /** One scan runs at most this long, then it is restarted (Android may degrade long scans). */
        private const val SCAN_WINDOW_MS = 10 * 60_000L
        private const val DONE_COOLDOWN_MS = 20_000L
        private const val SKIPPED_COOLDOWN_MS = 15 * 60_000L

        fun start(context: Context) {
            context.startForegroundService(Intent(context, RelayService::class.java))
        }

        /** "Sync T5 now": look for the T5 and sync it once (the relay must be running). */
        fun syncPda(context: Context) {
            context.startForegroundService(Intent(context, RelayService::class.java).setAction(ACTION_PDA_SYNC))
        }

        fun stop(context: Context) {
            Settings(context).relayEnabled = false
            context.stopService(Intent(context, RelayService::class.java))
        }
    }
}
