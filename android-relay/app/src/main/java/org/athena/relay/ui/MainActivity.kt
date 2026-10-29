package org.athena.relay.ui

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings as AndroidSettings
import android.view.View
import android.widget.Button
import android.widget.CheckBox
import android.widget.TextView
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.MainScope
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import org.athena.relay.R
import org.athena.relay.service.Permissions
import org.athena.relay.service.Phase
import org.athena.relay.service.RelaySnapshot
import org.athena.relay.service.RelayService
import org.athena.relay.service.RelayState
import org.athena.relay.store.Settings
import java.text.DateFormat
import java.util.Date

/** Status screen: BLE state, watch info and status, counters, last error, recent log. */
class MainActivity : Activity() {
    private lateinit var settings: Settings
    private var scope: CoroutineScope? = null

    private lateinit var phase: TextView
    private lateinit var watch: TextView
    private lateinit var pda: TextView
    private lateinit var counters: TextView
    private lateinit var lastError: TextView
    private lateinit var logView: TextView
    private lateinit var toggle: Button
    private lateinit var battery: Button
    private lateinit var pdaSync: Button
    private lateinit var pdaAuto: CheckBox
    private lateinit var permissionNote: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        settings = Settings(this)
        phase = findViewById(R.id.phase)
        watch = findViewById(R.id.watch)
        pda = findViewById(R.id.pda)
        counters = findViewById(R.id.counters)
        lastError = findViewById(R.id.last_error)
        showCrash()
        logView = findViewById(R.id.log)
        toggle = findViewById(R.id.toggle)
        battery = findViewById(R.id.battery)
        pdaSync = findViewById(R.id.pda_sync)
        pdaAuto = findViewById(R.id.pda_auto)
        permissionNote = findViewById(R.id.permission_note)

        toggle.setOnClickListener { onToggle() }
        findViewById<Button>(R.id.open_settings).setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        battery.setOnClickListener { askBatteryExemption() }
        pdaSync.setOnClickListener { RelayService.syncPda(this) }
        pdaAuto.isChecked = settings.pdaAutoSync
        pdaAuto.setOnCheckedChangeListener { _, on ->
            if (on == settings.pdaAutoSync) return@setOnCheckedChangeListener
            settings.pdaAutoSync = on
            // Takes effect at once: the running scan is restarted with or without the T5.
            if (running(RelayState.state.value)) RelayService.pdaAutoChanged(this)
        }
    }

    override fun onStart() {
        super.onStart()
        val s = MainScope()
        scope = s
        s.launch { RelayState.state.collect { render(it) } }
        s.launch { RelayState.log.collect { logView.text = it.asReversed().joinToString("\n") } }
        refreshChecks()
    }

    override fun onStop() {
        scope?.cancel()
        scope = null
        super.onStop()
    }

    private fun refreshChecks() {
        val pm = getSystemService(PowerManager::class.java)
        battery.visibility = if (pm.isIgnoringBatteryOptimizations(packageName)) View.GONE else View.VISIBLE
        val missing = Permissions.missing(this)
        permissionNote.visibility = if (missing.isEmpty()) View.GONE else View.VISIBLE
        render(RelayState.state.value)
    }

    private fun running(s: RelaySnapshot) = settings.relayEnabled && s.phase != Phase.STOPPED && s.phase != Phase.ERROR

    private fun onToggle() {
        if (running(RelayState.state.value)) {
            RelayService.stop(this)
            return
        }
        if (settings.target() == null) {
            startActivity(Intent(this, SettingsActivity::class.java))
            return
        }
        val missing = Permissions.missing(this)
        if (missing.isNotEmpty()) {
            requestPermissions((missing + Permissions.optional()).toTypedArray(), REQ_PERMS)
            return
        }
        Permissions.optional().filter { checkSelfPermission(it) != android.content.pm.PackageManager.PERMISSION_GRANTED }
            .takeIf { it.isNotEmpty() }?.let { requestPermissions(it.toTypedArray(), REQ_OPTIONAL) }
        settings.relayEnabled = true
        RelayService.start(this)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        refreshChecks()
        if (requestCode == REQ_PERMS && Permissions.hasAll(this)) {
            settings.relayEnabled = true
            RelayService.start(this)
        }
    }

    private fun askBatteryExemption() {
        // The owner decides; we only open the system dialog (or the settings list as a fallback).
        val direct = Intent(AndroidSettings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName"))
        try {
            startActivity(direct)
        } catch (e: android.content.ActivityNotFoundException) {
            startActivity(Intent(AndroidSettings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
    }

    /** The last crash, if any, to copy and send (store/CrashLog.kt). */
    private fun showCrash() {
        val report = org.athena.relay.store.CrashLog.take(this) ?: return
        android.app.AlertDialog.Builder(this)
            .setTitle(R.string.crash_title)
            .setMessage(report)
            .setPositiveButton(R.string.crash_copy) { _, _ ->
                val cm = getSystemService(android.content.ClipboardManager::class.java)
                cm.setPrimaryClip(android.content.ClipData.newPlainText("crash", report))
            }
            .setNegativeButton(R.string.crash_close, null)
            .show()
    }

    private fun render(s: RelaySnapshot) {
        val enabled = running(s)
        toggle.setText(if (enabled) R.string.stop_relay else R.string.start_relay)
        phase.text = if (s.detail.isEmpty()) s.phase.label else "${s.phase.label}\n${s.detail}"

        val w = StringBuilder()
        s.info?.let {
            w.append("${it.device ?: "?"} · firmware ${it.fw ?: "?"}")
            it.reset?.let { r -> w.append(" · last reset $r") }
            w.append("\n")
        }
        s.status?.let { st ->
            val battery = if (st.batteryPercent >= 0) "${st.batteryPercent} %" else "unknown"
            val state = when {
                st.muted -> "muted"
                st.recording -> "recording"
                else -> "not recording"
            }
            w.append("Battery $battery${if (st.charging) " (charging)" else ""} · $state\n")
            w.append("Queued on watch: ${st.queuedChunks} chunks, ${"%.1f".format(st.queuedMs / 60_000.0)} min\n")
            if (!st.sdOk) w.append("SD card problem on the watch\n")
            if (s.statusAtMs > 0) w.append("as of ${fmt(s.statusAtMs)}")
        }
        watch.text = if (w.isEmpty()) getString(R.string.no_watch_yet) else w.toString().trimEnd()
        pda.text = pdaText(s)
        // Manual T5 sync needs the running relay (it owns the BLE stack, so it can't race the watch).
        pdaSync.visibility = if (settings.pdaEnabled) View.VISIBLE else View.GONE
        pdaAuto.visibility = pdaSync.visibility
        pdaSync.isEnabled = enabled && s.phase != Phase.PDA_SYNC

        val last = settings.lastSyncMs
        counters.text = getString(R.string.counters, s.uploadedThisRun, settings.totalUploaded,
            if (last > 0) fmt(last) else getString(R.string.never))
        lastError.text = s.lastError ?: getString(R.string.none)
    }

    /** One line for the T5 PDA (docs/t5-pda-plan.md), plus its battery and outbox when known. */
    private fun pdaText(s: RelaySnapshot): String {
        if (!settings.pdaEnabled) return getString(R.string.pda_off)
        val lines = ArrayList<String>()
        s.pdaInfo?.let { lines += "${it.device ?: "?"} · firmware ${it.fw ?: "?"}" }
        s.pdaStatus?.let { st ->
            val battery = if (st.batteryPercent >= 0) "${st.batteryPercent} %" else "unknown"
            lines += "Battery $battery${if (st.charging) " (charging)" else ""} · outbox ${st.outboxWaiting}"
        }
        lines += s.pdaLine.ifEmpty {
            val last = settings.lastPdaSyncMs
            if (last > 0) getString(R.string.pda_last_sync, fmt(last)) else getString(R.string.pda_not_seen)
        }
        return lines.joinToString("\n")
    }

    private fun fmt(ms: Long): String = DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.SHORT).format(Date(ms))

    companion object {
        private const val REQ_PERMS = 1
        private const val REQ_OPTIONAL = 2
    }
}
