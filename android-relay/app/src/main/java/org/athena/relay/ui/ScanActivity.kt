package org.athena.relay.ui

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
import org.athena.relay.R

/**
 * Scans the Odysseus pairing QR code on the phone and returns its text as [EXTRA_TEXT].
 *
 * ZXing's own `CaptureActivity` can't be used: its `CaptureManager` calls AndroidX
 * (`ContextCompat`, `ActivityCompat`) for the camera permission, which zxing-android-embedded
 * doesn't declare and this app doesn't have, so it crashed on open. This screen uses only
 * ZXing's camera view (no AndroidX) and the framework permission API.
 */
class ScanActivity : Activity() {
    private var scanner: DecoratedBarcodeView? = null
    private var granted = false

    private val callback = BarcodeCallback { result ->
        val text = result.text ?: return@BarcodeCallback
        setResult(RESULT_OK, Intent().putExtra(EXTRA_TEXT, text))
        finish()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Ask for the camera before building the camera view, so a problem in the view can
        // never hide the permission prompt.
        if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            granted = true
        } else {
            requestPermissions(arrayOf(Manifest.permission.CAMERA), REQ_CAMERA)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQ_CAMERA) return
        if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) {
            granted = true // onResume, which follows the permission dialog, starts the camera
        } else {
            Toast.makeText(this, R.string.pair_camera_denied, Toast.LENGTH_LONG).show()
            setResult(RESULT_CANCELED)
            finish()
        }
    }

    override fun onResume() {
        super.onResume()
        if (!granted || isFinishing) return
        try {
            val view = scanner ?: DecoratedBarcodeView(this).also {
                it.decoderFactory = DefaultDecoderFactory(listOf(BarcodeFormat.QR_CODE))
                it.setStatusText(getString(R.string.pair_scan_hint))
                setContentView(it)
                scanner = it
            }
            view.decodeSingle(callback)
            view.resume()
        } catch (e: Throwable) {
            fail(e)
        }
    }

    override fun onPause() {
        super.onPause()
        try {
            scanner?.pause()
        } catch (e: Throwable) {
            // Closing anyway; nothing to show.
        }
    }

    /** Back to Settings with what went wrong, instead of crashing the app. */
    private fun fail(e: Throwable) {
        val what = e.javaClass.simpleName + (e.message?.let { ": ${it.take(200)}" } ?: "")
        setResult(RESULT_CANCELED, Intent().putExtra(EXTRA_ERROR, what))
        finish()
    }

    companion object {
        const val EXTRA_TEXT = "scan_text"
        const val EXTRA_ERROR = "scan_error"
        private const val REQ_CAMERA = 7
    }
}
