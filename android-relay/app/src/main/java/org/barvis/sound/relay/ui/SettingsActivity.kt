package org.barvis.sound.relay.ui

import android.app.Activity
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
import org.barvis.sound.relay.net.OdysseusCalendarClient
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

/** Server URL, token (stored encrypted, never shown), test connection, start on boot. */
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
            odyResult.text = when (val r = client.listEvents(f.format(start), f.format(start.plusDays(8)))) {
                is ApiResult.Ok -> resources.getQuantityString(R.plurals.odysseus_ok, r.value.size, r.value.size)
                is ApiResult.Unsupported -> getString(R.string.odysseus_unsupported, r.reason)
                is ApiResult.Fail -> getString(R.string.odysseus_failed, FeedBuilder.describe(r.error))
            }
        }
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
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
        val c = ServerUrl.check(url.text.toString())
        if (c !is ServerUrl.Check.Ok) {
            result.text = (c as ServerUrl.Check.Invalid).reason
            return
        }
        savePda()?.let {
            result.text = it
            return
        }
        settings.serverUrl = c.base
        val t = token.text.toString().trim()
        if (t.isNotEmpty()) settings.setToken(t)
        token.text.clear()
        tokenState.setText(if (settings.hasToken) R.string.token_saved_hint else R.string.token_hint)
        settings.startOnBoot = boot.isChecked
        settings.tokenRejected = false
        result.text = getString(R.string.saved)
        RelayState.logger.log(org.barvis.sound.relay.relay.Level.INFO, "settings saved (server ${c.base})")
    }

    private fun test() {
        val c = ServerUrl.check(url.text.toString())
        if (c !is ServerUrl.Check.Ok) {
            result.text = (c as ServerUrl.Check.Invalid).reason
            return
        }
        val typed = token.text.toString().trim()
        result.text = getString(R.string.testing)
        scope.launch {
            val stored = if (typed.isEmpty()) settings.token() else typed
            val up = HttpUploader(c.base, { stored }, RelayState.logger)
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
