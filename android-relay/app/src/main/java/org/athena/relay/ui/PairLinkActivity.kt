package org.athena.relay.ui

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import org.athena.relay.net.PairingLink

/**
 * Opens `odysseus-pair:` links, e.g. from the phone's own camera app scanning the pairing
 * QR code. It never pairs by itself: any app can send such a link, so Settings only fills
 * it in and shows which Odysseus it is for; the owner taps Pair.
 */
class PairLinkActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val link = intent?.dataString?.take(MAX_LINK)
        if (link != null && link.startsWith(PairingLink.PREFIX)) {
            startActivity(Intent(this, SettingsActivity::class.java)
                .putExtra(SettingsActivity.EXTRA_PAIR_LINK, link)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP))
        }
        finish()
    }

    private companion object {
        const val MAX_LINK = 600
    }
}
