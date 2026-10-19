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
import org.barvis.sound.relay.net.ServerUrl
import org.barvis.sound.relay.relay.ServerResult
import org.barvis.sound.relay.service.RelayState
import org.barvis.sound.relay.store.Settings

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
