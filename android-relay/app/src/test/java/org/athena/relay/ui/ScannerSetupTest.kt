package org.athena.relay.ui

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * ZXing's ready-made scanner (`CaptureActivity`, `CaptureManager`, `ScanOptions`/`ScanContract`,
 * `IntentIntegrator`) calls AndroidX, which this app doesn't ship: opening it crashed the app.
 * The pairing scanner is [ScanActivity] over ZXing's camera view; keep it that way.
 */
class ScannerSetupTest {
    private val main = File("src/main")

    @Test fun captureActivityIsRemovedFromTheManifest() {
        val manifest = File(main, "AndroidManifest.xml").readText()
        assertTrue(Regex("""CaptureActivity"\s+tools:node="remove"""").containsMatchIn(manifest))
        assertTrue(manifest.contains("android:name=\".ui.ScanActivity\""))
    }

    @Test fun noCodeStartsZxingsAndroidXScanner() {
        val banned = listOf("ScanOptions", "ScanContract", "ScanIntentResult", "IntentIntegrator", "CaptureManager",
            "barcodescanner.CaptureActivity")
        File(main, "java").walk().filter { it.extension == "kt" }.forEach { f ->
            val code = f.readLines().filterNot { it.trimStart().startsWith("*") || it.trimStart().startsWith("//") }
                .joinToString("\n")
            for (name in banned) assertFalse("${f.name} uses $name", code.contains(name))
        }
    }
}
