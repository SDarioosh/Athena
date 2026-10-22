package org.barvis.sound.relay.ui

import android.app.Activity
import android.app.AlertDialog
import android.content.ActivityNotFoundException
import android.content.Intent
import android.os.Bundle
import android.text.Editable
import android.text.TextWatcher
import android.view.View
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.TextView
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.MainScope
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import org.barvis.sound.relay.R
import org.barvis.sound.relay.net.HttpUploader
import org.barvis.sound.relay.net.PairingClient
import org.barvis.sound.relay.net.PairingLink
import org.barvis.sound.relay.net.Routes
import org.barvis.sound.relay.net.OdysseusCalendarClient
import org.barvis.sound.relay.net.OdysseusInsightClient
import org.barvis.sound.relay.net.OdysseusTodosClient
import org.barvis.sound.relay.net.ServerUrl
import org.barvis.sound.relay.relay.ApiResult
import org.barvis.sound.relay.relay.FeedBuilder
import org.barvis.sound.relay.relay.ServerResult
import org.barvis.sound.relay.service.RelayState
import org.barvis.sound.relay.store.Settings
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

/**
 * Pair with Odysseus (scan or paste the code), or the direct server URL and token (stored
 * encrypted, never shown); test connection, start on boot, the T5 section.
 */
class SettingsActivity : Activity() {
    private lateinit var settings: Settings
    private lateinit var url: EditText
    private lateinit var token: EditText
    private lateinit var warning: TextView
    private lateinit var result: TextView
    private lateinit var boot: CheckBox
    private lateinit var tokenState: TextView
    private val scope: CoroutineScope = MainScope()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_settings)
        settings = Settings(this)
        url = findViewById(R.id.server_url)
        token = findViewById(R.id.token)
        warning = findViewById(R.id.url_warning)
        result = findViewById(R.id.test_result)
        boot = findViewById(R.id.start_on_boot)
        tokenState = findViewById(R.id.token_state)

        url.setText(settings.serverUrl)
        tokenState.setText(if (settings.hasToken) R.string.token_saved_hint else R.string.token_hint)
        boot.isChecked = settings.startOnBoot
        updateWarning()
        url.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) = Unit
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) = Unit
            override fun afterTextChanged(s: Editable?) = updateWarning()
        })

        findViewById<Button>(R.id.save).setOnClickListener { save() }
        findViewById<Button>(R.id.test).setOnClickListener { test() }
        initPda()
        initPairing()
    }

    // ------------------------------------------------------------- pairing

    private lateinit var pairState: TextView
    private lateinit var pairLink: EditText
    private lateinit var pairResult: TextView
    private lateinit var throughOdysseus: CheckBox

    private fun initPairing() {
        pairState = findViewById(R.id.pair_state)
        pairLink = findViewById(R.id.pair_link)
        pairResult = findViewById(R.id.pair_result)
        throughOdysseus = findViewById(R.id.through_odysseus)
        throughOdysseus.isChecked = settings.throughOdysseus
        throughOdysseus.setOnCheckedChangeListener { box, checked ->
            if (checked && !settings.hasOdysseusToken && odyToken.text.isBlank()) {
                box.isChecked = false
                pairResult.setText(R.string.through_odysseus_needs_pairing)
            }
        }
        findViewById<Button>(R.id.pair_scan).setOnClickListener { scan() }
        findViewById<Button>(R.id.pair_paste).setOnClickListener { pair(pairLink.text.toString()) }
        findViewById<Button>(R.id.choose_calendar).setOnClickListener { chooseCalendar() }
        showPairState()
    }

    private fun showPairState() {
        val paired = settings.throughOdysseus && settings.hasOdysseusToken
        pairState.text = if (paired) getString(R.string.pair_state_paired, settings.odysseusUrl)
            else getString(R.string.pair_state_direct)
    }

    private fun scan() {
        val intent = Intent(this, ScanActivity::class.java)
        try {
            @Suppress("DEPRECATION") // framework Activity: no AndroidX result API here
            startActivityForResult(intent, REQ_SCAN)
        } catch (e: ActivityNotFoundException) {
            pairResult.setText(R.string.pair_scan_unavailable)
        }
    }

    @Deprecated("Framework Activity result callback")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQ_SCAN) return
        if (resultCode == RESULT_OK) data?.getStringExtra(ScanActivity.EXTRA_TEXT)?.let { pair(it) }
    }

    /** Redeems a scanned or pasted pairing link and switches to "through Odysseus". */
    private fun pair(text: String) {
        val link = when (val r = PairingLink.parse(text)) {
            is PairingLink.Result.Ok -> r.link
            is PairingLink.Result.Invalid -> {
                pairResult.text = getString(R.string.pair_failed, r.reason)
                return
            }
        }
        pairResult.text = withWarning(getString(R.string.pair_working, link.base), link.warning)
        scope.launch {
            when (val r = PairingClient().redeem(link)) {
                is PairingClient.Result.Ok -> {
                    settings.savePairing(r.paired.base, r.paired.token)
                    pairLink.text.clear()
                    odyUrl.setText(r.paired.base)
                    odyToken.text.clear()
                    odyTokenState.setText(R.string.odysseus_token_saved)
                    throughOdysseus.isChecked = true
                    pairResult.text = withWarning(getString(R.string.pair_done, r.paired.base, r.paired.name), link.warning)
                    showPairState()
                    RelayState.logger.log(org.barvis.sound.relay.relay.Level.INFO, "paired with Odysseus at ${r.paired.base}")
                }
                is PairingClient.Result.Failed -> pairResult.text = getString(R.string.pair_failed, r.reason)
            }
        }
    }

    private fun withWarning(text: String, warning: String?): String =
        if (warning == null) text else getString(R.string.two_lines, text, warning)

    private fun chooseCalendar() {
        val c = ServerUrl.check(odyUrl.text.toString())
        val typed = odyToken.text.toString().trim()
        val token = if (typed.isEmpty()) settings.odysseusToken() else typed
        if (c !is ServerUrl.Check.Ok || token.isEmpty()) {
            odyResult.setText(R.string.odysseus_not_set)
            return
        }
        odyResult.text = getString(R.string.testing)
        scope.launch {
            when (val r = OdysseusCalendarClient(c.base, { token }, log = RelayState.logger).calendars()) {
                is ApiResult.Ok -> {
                    odyResult.text = ""
                    if (r.value.isEmpty()) {
                        odyResult.setText(R.string.choose_calendar_none)
                        return@launch
                    }
                    val labels = listOf(getString(R.string.choose_calendar_default)) + r.value.map { it.label }
                    AlertDialog.Builder(this@SettingsActivity)
                        .setTitle(R.string.choose_calendar_title)
                        .setItems(labels.toTypedArray()) { _, which ->
                            odyCalendar.setText(if (which == 0) "" else r.value[which - 1].id)
                        }
                        .show()
                }
                is ApiResult.Unsupported -> odyResult.text = getString(R.string.choose_calendar_failed, r.reason)
                is ApiResult.Fail -> odyResult.text = getString(R.string.choose_calendar_failed, FeedBuilder.describe(r.error))
            }
        }
    }

    // ------------------------------------------------------------------ T5 PDA

    private lateinit var pdaEnabled: CheckBox
    private lateinit var odyUrl: EditText
    private lateinit var odyToken: EditText
    private lateinit var odyTokenState: TextView
    private lateinit var odyWarning: TextView
    private lateinit var odyCalendar: EditText
    private lateinit var templates: EditText
    private lateinit var interval: EditText
    private lateinit var odyResult: TextView

    private fun initPda() {
        pdaEnabled = findViewById(R.id.pda_enabled)
        odyUrl = findViewById(R.id.odysseus_url)
        odyToken = findViewById(R.id.odysseus_token)
        odyTokenState = findViewById(R.id.odysseus_token_state)
        odyWarning = findViewById(R.id.odysseus_url_warning)
        odyCalendar = findViewById(R.id.odysseus_calendar)
        templates = findViewById(R.id.pda_templates)
        interval = findViewById(R.id.pda_interval)
        odyResult = findViewById(R.id.odysseus_result)

        pdaEnabled.isChecked = settings.pdaEnabled
        odyUrl.setText(settings.odysseusUrl)
        odyCalendar.setText(settings.odysseusCalendarId)
        templates.setText(settings.eventTemplates.joinToString("\n"))
        interval.setText(String.format(Locale.ROOT, "%d", settings.pdaSyncMinutes))
        odyTokenState.setText(if (settings.hasOdysseusToken) R.string.odysseus_token_saved else R.string.odysseus_token_hint)
        updateOdysseusWarning()
        odyUrl.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) = Unit
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) = Unit
            override fun afterTextChanged(s: Editable?) = updateOdysseusWarning()
        })
        findViewById<Button>(R.id.test_odysseus).setOnClickListener { testOdysseus() }
    }

    private fun updateOdysseusWarning() {
        val text = when (val c = ServerUrl.check(odyUrl.text.toString())) {
            is ServerUrl.Check.Ok -> c.warning
            is ServerUrl.Check.Invalid -> if (odyUrl.text.isBlank()) null else c.reason
        }
        odyWarning.text = text ?: ""
        odyWarning.visibility = if (text == null) View.GONE else View.VISIBLE
    }

    /** Saves the PDA section; returns an error to show, or null. */
    private fun savePda(): String? {
        val raw = odyUrl.text.toString()
        val base = if (raw.isBlank()) "" else when (val c = ServerUrl.check(raw)) {
            is ServerUrl.Check.Ok -> c.base
            is ServerUrl.Check.Invalid -> return "Odysseus URL: ${c.reason}"
        }
        val t = odyToken.text.toString().trim()
        if (t.isNotEmpty() && !t.startsWith("ody_")) return getString(R.string.odysseus_token_shape)
        settings.pdaEnabled = pdaEnabled.isChecked
        settings.odysseusUrl = base
        if (t.isNotEmpty()) settings.setOdysseusToken(t)
        odyToken.text.clear()
        odyTokenState.setText(if (settings.hasOdysseusToken) R.string.odysseus_token_saved else R.string.odysseus_token_hint)
        settings.odysseusCalendarId = odyCalendar.text.toString().trim()
        settings.eventTemplates = templates.text.toString().lines()
        settings.pdaSyncMinutes = interval.text.toString().trim().toIntOrNull() ?: settings.pdaSyncMinutes
        interval.setText(String.format(Locale.ROOT, "%d", settings.pdaSyncMinutes))
        return null
    }

    private fun testOdysseus() {
        val c = ServerUrl.check(odyUrl.text.toString())
        val typed = odyToken.text.toString().trim()
        val token = if (typed.isEmpty()) settings.odysseusToken() else typed
        if (c !is ServerUrl.Check.Ok || token.isEmpty()) {
            odyResult.setText(R.string.odysseus_not_set)
            return
        }
        odyResult.text = getString(R.string.testing)
        val calendarId = odyCalendar.text.toString().trim().ifEmpty { null }
        scope.launch {
            val client = OdysseusCalendarClient(c.base, { token }, calendarId, log = RelayState.logger)
            val zone = ZoneId.systemDefault()
            val start = LocalDate.now(zone).atStartOfDay(zone)
            val f = DateTimeFormatter.ISO_OFFSET_DATE_TIME
            val cal = when (val r = client.listEvents(f.format(start), f.format(start.plusDays(8)))) {
                is ApiResult.Ok -> resources.getQuantityString(R.plurals.odysseus_ok, r.value.size, r.value.size)
                is ApiResult.Unsupported -> getString(R.string.odysseus_unsupported, r.reason)
                is ApiResult.Fail -> getString(R.string.odysseus_failed, FeedBuilder.describe(r.error))
            }
            val todos = OdysseusTodosClient(c.base, { token }, log = RelayState.logger)
            val checklists = when (val r = todos.notes()) {
                is ApiResult.Ok -> r.value.count { it.type == "checklist" && it.items.isNotEmpty() }
                    .let { n -> resources.getQuantityString(R.plurals.odysseus_todos_ok, n, n) }
                is ApiResult.Unsupported -> getString(R.string.odysseus_todos_unsupported, r.reason)
                is ApiResult.Fail -> getString(R.string.odysseus_todos_failed, FeedBuilder.describe(r.error))
            }
            val insight = when (val r = OdysseusInsightClient(c.base, { token }, log = RelayState.logger).today()) {
                is ApiResult.Ok -> getString(R.string.odysseus_insight_ok)
                is ApiResult.Unsupported -> getString(R.string.odysseus_insight_unsupported, r.reason)
                is ApiResult.Fail -> getString(R.string.odysseus_insight_failed, FeedBuilder.describe(r.error))
            }
            odyResult.text = getString(R.string.odysseus_test_result, cal, checklists, insight)
        }
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private companion object {
        const val REQ_SCAN = 41
    }

    private fun updateWarning() {
        val text = when (val c = ServerUrl.check(url.text.toString())) {
            is ServerUrl.Check.Ok -> c.warning
            is ServerUrl.Check.Invalid -> if (url.text.isBlank()) null else c.reason
        }
        warning.text = text ?: ""
        warning.visibility = if (text == null) View.GONE else View.VISIBLE
    }

    private fun save() {
        val raw = url.text.toString()
        val base = if (raw.isBlank()) "" else when (val c = ServerUrl.check(raw)) {
            is ServerUrl.Check.Ok -> c.base
            is ServerUrl.Check.Invalid -> {
                result.text = c.reason
                return
            }
        }
        if (base.isEmpty() && !throughOdysseus.isChecked) {
            result.text = ServerUrl.check(raw).let { (it as? ServerUrl.Check.Invalid)?.reason ?: "" }
            return
        }
        savePda()?.let {
            result.text = it
            return
        }
        settings.serverUrl = base
        val t = token.text.toString().trim()
        if (t.isNotEmpty()) settings.setToken(t)
        token.text.clear()
        tokenState.setText(if (settings.hasToken) R.string.token_saved_hint else R.string.token_hint)
        settings.uploadMode = if (throughOdysseus.isChecked && settings.hasOdysseusToken) Settings.MODE_ODYSSEUS
            else Settings.MODE_DIRECT
        throughOdysseus.isChecked = settings.throughOdysseus
        settings.startOnBoot = boot.isChecked
        settings.tokenRejected = false
        result.text = getString(R.string.saved)
        showPairState()
        val where = if (settings.throughOdysseus) "through Odysseus ${settings.odysseusUrl}" else "server $base"
        RelayState.logger.log(org.barvis.sound.relay.relay.Level.INFO, "settings saved ($where)")
    }

    /** Tests what the relay would use: through Odysseus when that is ticked, else direct. */
    private fun test() {
        val odysseus = throughOdysseus.isChecked
        val c = ServerUrl.check((if (odysseus) odyUrl else url).text.toString())
        if (c !is ServerUrl.Check.Ok) {
            result.text = (c as ServerUrl.Check.Invalid).reason
            return
        }
        val typed = (if (odysseus) odyToken else token).text.toString().trim()
        result.text = getString(R.string.testing)
        scope.launch {
            val stored = if (typed.isNotEmpty()) typed else if (odysseus) settings.odysseusToken() else settings.token()
            val routes = if (odysseus) Routes.ODYSSEUS else Routes.DIRECT
            val up = HttpUploader(c.base, { stored }, RelayState.logger, routes = routes)
            if (odysseus && stored.isEmpty()) {
                result.setText(R.string.test_no_token)
                return@launch
            }
            val health = up.health()
            if (health !is ServerResult.Ok) {
                result.text = getString(R.string.test_unreachable, HttpUploader.describe(health))
                return@launch
            }
            if (stored.isEmpty()) {
                result.setText(R.string.test_no_token)
                return@launch
            }
            result.text = when (val auth = up.checkToken()) {
                is ServerResult.Ok -> getString(R.string.test_ok)
                is ServerResult.AuthRejected -> getString(R.string.test_rejected, auth.code)
                else -> getString(R.string.test_failed, HttpUploader.describe(auth))
            }
        }
    }
}
