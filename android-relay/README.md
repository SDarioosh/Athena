# barvis-sound Android relay

Android app that carries recordings from the LilyGO T-Watch Ultra to the owner's
barvis-sound server:

```
watch (BLE, protocol v1) → this app → POST /v1/ingest/chunk on your server (LAN / Tailscale)
```

The watch has no Wi-Fi. This app is the only thing that holds the server token. It
implements [docs/twatch-ble-protocol.md](../docs/twatch-ble-protocol.md) (the contract),
the same algorithm as the PC relay in `firmware/recorder/tools/`.

- A foreground service scans for the watch's service UUID (low-power, filtered), connects,
  pairs, asks for MTU 517, 2M PHY and high connection priority, subscribes to `data` and
  `status`, reads `info`, sends `TIME`, checks `GET /health`, then drains: `LIST`, `READ`
  (offset, size and CRC-32 checks, up to 3 re-reads, resume after a disconnect), upload,
  `/end` for a session's last chunk, and only then `ACK`. Then `BYE`, disconnect, scan again.
- A chunk is deleted on the watch only after the server answered `ok` (§1). Network
  errors, 5xx, 408, 429 and redirects stop the drain and retry after 1, 2, 5, 10, 15 min;
  401/403 stop the relay until the token is fixed; other 4xx skip that chunk (it stays on
  the watch; drop it there with `queue drop <id>` if it's bad).
- The app keeps no copy of the audio. The only audio it holds is the chunk in flight (in
  memory), plus the first part of a chunk whose read was cut off, kept in memory to resume.

## Build

Needs JDK 17+ (21 tested) and the Android SDK (platform 35, build-tools 35.0.0). Point
Gradle at the SDK with `ANDROID_HOME` or `sdk.dir` in `local.properties` (git-ignored).

```bash
cd android-relay
./gradlew assembleDebug test      # APK + JVM unit tests
./gradlew lint                    # optional
```

The debug APK is signed with the SDK's default debug key. A release build needs your own
signing key; never commit a keystore.

Versions: AGP 9.4.1 (built-in Kotlin, Kotlin 2.2.10), Gradle 9.6.1 (wrapper, checksum
pinned), compileSdk/targetSdk 35, minSdk 26 (Android 8.0: first version with
`setPreferredPhy`, notification channels and `startForegroundService`).

Runtime dependencies: `kotlinx-coroutines-android` and the Kotlin standard library,
nothing else. The UI uses framework views (no AppCompat/Material); HTTP uses
`HttpURLConnection`. Tests: JUnit 4 only.

## Install

```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

## First-time setup

1. Open **Barvis Relay → Settings**. Enter the server URL, for example
   `https://myserver.<tailnet>.ts.net` (Tailscale HTTPS certificate) or
   `http://100.x.y.z:8000` / `http://192.168.x.y:8000`, and the server's
   `CONVO_INGEST_TOKEN`. Tap **Test connection** (checks `/health`, then the token with an
   authenticated `GET /v1/sessions?limit=1`), then **Save**.
2. On the watch, **long-press BOOT** to open the 2-minute sync window. New bonds are only
   accepted then.
3. Tap **Start relay** and grant *Nearby devices* (Android 12+) or *Location* (Android 8–11:
   BLE scans need it there, and Location must be switched on), and notifications.
   When Android asks for a pairing code, type the 6 digits shown on the watch screen.
4. Tap **Let the relay run in the background** and allow it. On phones with aggressive
   battery managers (Xiaomi, Huawei, OnePlus, Samsung "sleeping apps"…) also exclude the
   app there; see <https://dontkillmyapp.com> for your model.
5. Optional: in Settings, turn on **Start the relay after the phone restarts**.

If the phone was paired while the watch's sync window was closed, the watch drops the bond
but Android keeps it, and connections then fail with an authentication error. Remove
`barvis-tw` in Android's Bluetooth settings, open the sync window and start again.

## Permissions and why

| Permission | Why |
|---|---|
| `BLUETOOTH_SCAN` (`neverForLocation`), `BLUETOOTH_CONNECT` | Find and connect to the watch (Android 12+) |
| `ACCESS_FINE_LOCATION` (Android ≤ 11 only) | Android 8–11 only return BLE scan results with it; not used for location |
| `BLUETOOTH`, `BLUETOOTH_ADMIN` (Android ≤ 11 only) | Legacy Bluetooth permissions |
| `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_CONNECTED_DEVICE` | The relay runs as a `connectedDevice` foreground service with a status notification |
| `POST_NOTIFICATIONS` | Show that notification (Android 13+; optional) |
| `INTERNET` | Upload to your server |
| `RECEIVE_BOOT_COMPLETED` | Restart after a reboot, only if you turned that on |
| `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` | Lets the app open the system "don't optimise" dialog; you decide |

## Privacy

- Data goes only from the watch to the one server URL you set. No other host, no
  fallback, no analytics, no crash reporting, no Firebase or Google Play services. HTTP
  redirects are not followed, so the token can't be sent elsewhere.
- The token is encrypted with an AES-256-GCM key in the Android Keystore, never shown
  again, never logged. `allowBackup="false"` and the data-extraction rules exclude
  everything from cloud backup and device transfer.
- Logs (logcat and the in-app event list) contain chunk ids, sizes, session ids and server
  answers, never the token or audio.
- Cleartext `http://` is allowed because Tailscale and LAN addresses can't be listed by
  range in Android's network security config. Settings shows a warning when an `http://`
  URL isn't RFC 1918, Tailscale (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`), `*.ts.net`,
  `*.local` or loopback. Prefer `https://` with a Tailscale certificate.

## Code layout

```
app/src/main/java/org/barvis/sound/relay/
  protocol/   pure Kotlin: GATT UUIDs, command/response/entry/status/info codec (§3–§5)
  relay/      pure Kotlin: DrainSession (§6) over WatchLink + Uploader, Backoff, ScanThrottle,
              PartialReads (resume), TIME_UNSET anchors, timestamps
  net/        HttpUploader (HttpURLConnection, error classes §6.5), ServerUrl (http warning)
  ble/        Android BLE glue: GattClient (one GATT op at a time), BleWatchLink, Scanner, Bonding
  service/    RelayService (foreground loop), RelayState, Permissions, BootReceiver
  store/      Settings (SharedPreferences), TokenCipher (Keystore AES-GCM)
  ui/         MainActivity (status + log), SettingsActivity
```

Unit tests (`app/src/test`) run on the JVM: the §8 test vectors byte for byte; a fake
watch (real CRC-32s, MTU-sized `DATA` frames, `LIST` paging, idempotent `ACK`, the busy
rule, injected CRC errors, disconnects and SD errors) with a fake server for the drain
rules; and the real `HttpUploader` against a JDK `HttpServer` on localhost (multipart
fields, bearer header, error classes, no redirects, token never in logs).

## Not verified on hardware

Everything BLE-side runs only in tests against a fake watch so far. Still to check on a
real phone and watch:

- Pairing during the sync window (passkey entry with the code from the watch screen,
  `createBond()` on an open link), and
  the stale-bond case above.
- Throughput with MTU 517 / 2M PHY / high priority (expected: a 60 KB chunk in 1–2 s).
- Scanning and connecting with the screen off, in Doze, and after hours in the background;
  OEM battery killers.
- The 30 s watch idle timeout during the first pairing prompt (if it hits, the next
  connection carries on, already bonded).
- Start on boot (Android 14/15 foreground-service rules for `connectedDevice`).
