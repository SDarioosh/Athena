package org.barvis.sound.relay.ui

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.widget.Toast
import com.google.zxing.BarcodeFormat
import com.journeyapps.barcodescanner.BarcodeCallback
import com.journeyapps.barcodescanner.DecoratedBarcodeView
import com.journeyapps.barcodescanner.DefaultDecoderFactory
import org.barvis.sound.relay.R

/**
 * Scans the Odysseus pairing QR code on the phone and returns its text as [EXTRA_TEXT].
 *
 * ZXing's own `CaptureActivity` can't be used: its `CaptureManager` calls AndroidX
 * (`ContextCompat`, `ActivityCompat`) for the camera permission, which zxing-android-embedded
 * doesn't declare and this app doesn't have, so it crashed on open. This screen uses only
 * ZXing's camera view (no AndroidX) and the framework permission API.
 */
class ScanActivity : Activity() {
    private lateinit var scanner: DecoratedBarcodeView
    private var started = false

    private val callback = BarcodeCallback { result ->
        val text = result.text ?: return@BarcodeCallback
        setResult(RESULT_OK, Intent().putExtra(EXTRA_TEXT, text))
        finish()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        scanner = DecoratedBarcodeView(this)
        scanner.decoderFactory = DefaultDecoderFactory(listOf(BarcodeFormat.QR_CODE))
        scanner.setStatusText(getString(R.string.pair_scan_hint))
        setContentView(scanner)
        if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            started = true
        } else {
            requestPermissions(arrayOf(Manifest.permission.CAMERA), REQ_CAMERA)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQ_CAMERA) return
        if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) {
            started = true // onResume, which follows the permission dialog, starts the camera
        } else {
            Toast.makeText(this, R.string.pair_camera_denied, Toast.LENGTH_LONG).show()
            setResult(RESULT_CANCELED)
            finish()
        }
    }

    override fun onResume() {
        super.onResume()
        if (started) startScanning()
    }

    private fun startScanning() {
        scanner.decodeSingle(callback)
        scanner.resume()
    }

    override fun onPause() {
        super.onPause()
        scanner.pause()
    }

    companion object {
        const val EXTRA_TEXT = "scan_text"
        private const val REQ_CAMERA = 7
    }
}
