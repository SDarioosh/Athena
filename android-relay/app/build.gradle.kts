// Athena Android relay: watch (BLE) -> owner's server (LAN / Tailscale).
// Kotlin is compiled by AGP 9's built-in Kotlin support (no separate Kotlin plugin).
plugins {
    id("com.android.application")
}

android {
    namespace = "org.athena.relay"
    compileSdk = 35

    defaultConfig {
        // The app's identity on the phone: keep it unchanged (it differs from the Kotlin
        // namespace on purpose), so new builds update the installed app in place and keep its
        // pairing, token and settings.
        applicationId = "org.barvis.sound.relay"
        minSdk = 26
        targetSdk = 35
        versionCode = 3
        versionName = "0.2.1"
    }

    buildTypes {
        release {
            // No signing config here: the owner signs release builds with their own key,
            // which is never committed. Debug builds use the SDK's default debug key.
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        buildConfig = true
    }

    testOptions {
        unitTests.isReturnDefaultValues = false
    }

    // The app is sideloaded: don't embed the (Google Play) dependency metadata block.
    dependenciesInfo {
        includeInApk = false
        includeInBundle = false
    }

    lint {
        abortOnError = true
        warningsAsErrors = false
        checkReleaseBuilds = false
        // The app is sideloaded, never published on Google Play.
        disable += setOf("GradleDependency", "NewerVersionAvailable", "AndroidGradlePluginVersion",
            "OldTargetApi", "BatteryLife")
    }
}

dependencies {
    // Deliberately tiny: framework UI (no AppCompat/Material), coroutines, ZXing, nothing else.
    // No analytics, crash reporting, Firebase or Google Play services.
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    // QR scanning for "Pair with Odysseus": ZXing (Apache-2.0), decoded on the phone. No
    // Google Play services, no ML Kit model downloads, no network. Its CaptureActivity uses the
    // framework Camera API, so this pulls in no AndroidX.
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
    implementation("com.google.zxing:core:3.5.3")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.10.2")
}
