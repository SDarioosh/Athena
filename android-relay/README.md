# odysseus-sound Android relay

Android app that carries recordings from the LilyGO T-Watch Ultra to the owner's
odysseus-sound server:

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

Or download a built APK. Every CI run uploads `android-relay-debug-apk` as an artifact,
and pushing a `relay-v<version>` tag (e.g. `relay-v0.1.0`, matching `versionName`) makes
`.github/workflows/android-relay-release.yml` attach
`barvis-sound-relay-v<version>-debug.apk` and its SHA-256 to a GitHub Release. You can also
run that workflow by hand from the Actions tab with a tag name; a missing tag is created
on the branch you run it from. These are debug-signed builds for sideloading.

## First-time setup

1. Open **Odysseus Sound Relay → Settings**. Enter the server URL, for example
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

## T5 E-Paper PDA (optional)

The same app also syncs a LilyGO T5 E-Paper Pro used as a pocket PDA
([docs/t5-pda-plan.md](../docs/t5-pda-plan.md), "glance" protocol §3). The T5 holds no
token: the phone builds a compact feed and sends it over BLE, and carries the T5's taps back
to the servers.

```
odysseus-sound /v1/digests, /v1/conversations, /v1/action-items ┐
Odysseus /api/codex/calendar/events (ody_ token, calendar scopes) ┴→ this app → BLE → T5
```

- **One BLE job at a time, manual by default.** The T5 is synced only when you tap **Sync T5
  now** on the main screen (the relay must be running). The app then scans for the T5 alone
  for up to 60 s, syncs once and sends `BYE` (no stay-connected loop), and goes straight
  back to the watch; if the T5 isn't found it says so. The watch drain is unchanged.
  **Settings → PDA → Sync the T5 automatically** brings back the old behaviour: the scan
  filter holds both service UUIDs, a T5 is taken when its sync is due (every *N* minutes,
  default 15) or at once when its scan response says the outbox has entries (plan §3.5),
  and the app stays connected while the T5 is in use (below).
- **Per connection** (`PdaSync`, plan §3.4 and §3.5, the same rules as the PC relay): read
  `info` (`proto=1`), MTU ≥ 420, `TIME` with the zone's UTC offset at that moment,
  `GET /health` (on failure: `BYE`, the T5 keeps its feed, back off 1, 2, 5, 10, 15 min).
  Then the **outbox**, in order: each action is checked strictly (exact fields, times with
  an offset, 400 bytes at most; anything else is acked with result 1 and never forwarded)
  and sent to its server. Result 0 on 2xx, 1 on a final 4xx. A 5xx, network error or
  rejected token (of either server) stops the sync at that action: nothing after it is
  acked and no feed is built. `cal_add` is deduped per action, because the hub's create
  isn't idempotent. Then the **feed** (≤ 32 KB), sent only when its content changed or the
  T5 holds another version; then each `fetch` answer is `PUT` (blob id = the action's id)
  and acked after `PUT_OK`. In automatic mode, while the T5 says the user is active the app stays connected
  for up to 10 min: new actions are handled within seconds, the feed is refreshed every
  5 min, and a `TIME` every 20 s keeps the link inside the T5's 30 s idle limit.
- **Calendar.** Odysseus is optional. Without it (or with a hub that lacks the calendar
  routes) the feed has `"calendar": []` and `cal_add` / `cal_delete` stay queued on the T5
  ("not supported" in the status line) without blocking the other actions. If Odysseus
  can't be reached, the sync stops and retries like a server outage.

Setup:

1. In Odysseus (Settings → API tokens), create a token with only **Calendar read** and
   **Calendar write**. Copy the `ody_…` value; it is shown once.
2. In the relay's **Settings → PDA**: turn the sync on, enter the Odysseus URL (the same
   Tailscale host as the server, port 7000 on the owner's install, for example
   `http://myserver.<tailnet>.ts.net:7000`), the token, and the **calendar id** of your
   CalDAV calendar. Events go back to CalDAV only when they land in a CalDAV calendar;
   without an id Odysseus picks your first calendar. To find the id, open
   `<Odysseus URL>/api/calendar/calendars` in a browser that is logged in to Odysseus and
   copy the `href` of the calendar whose `source` is `caldav`. Edit the event templates (one per
   line; the T5 offers them in its "+" picker), and, for automatic sync, the interval. **Test Odysseus
   calendar** lists the next 7 days. **Save**.
3. Open the pairing window on the T5's Status screen and let the relay find it; type the
   code shown on the T5 when Android asks.

The main screen shows a **PDA** section: the T5's firmware, battery and outbox, and one
line on the last sync (actions handled or waiting, feed sent or unchanged, calendar notes).

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
  answers, never the token or audio. PDA logs hold action ids and types, feed sizes and
  versions, never summaries, titles or event names.
- The Odysseus token is a second token, encrypted the same way. Give it only the calendar
  scopes. The app remembers the last feed hash per T5 and the `cal_add` dedupe table
  (30 days) in private, non-backed-up storage.
- Cleartext `http://` is allowed because Tailscale and LAN addresses can't be listed by
  range in Android's network security config. Settings shows a warning when an `http://`
  URL isn't RFC 1918, Tailscale (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`), `*.ts.net`,
  `*.local` or loopback. Prefer `https://` with a Tailscale certificate.

## Code layout

```
app/src/main/java/org/barvis/sound/relay/
  protocol/   pure Kotlin: GATT UUIDs, command/response/entry/status/info codec (§3–§5);
              Glance.kt: the T5 PDA's glance protocol (t5-pda-plan §3)
  relay/      pure Kotlin: DrainSession (§6) over WatchLink + Uploader, Backoff, ScanThrottle,
              PartialReads (resume), TIME_UNSET anchors, timestamps;
              PdaSync (plan §3.4) over PdaLink + ConvoApi + CalendarApi, FeedBuilder (§4.1),
              DetailBuilder (§4.3), PdaAction (§4.2 validation), PdaSchedule
  net/        HttpUploader (HttpURLConnection, error classes §6.5), ServerUrl (http warning);
              Json (small reader/writer), PdaHttp: ConvoHttpApi, OdysseusCalendarClient
  ble/        Android BLE glue: GattClient (one GATT op at a time, watch or PDA profile),
              BleWatchLink, BlePdaLink, Scanner (one or both service UUIDs), Bonding
  service/    RelayService (foreground loop), PdaConnector (the PDA job), RelayState,
              Permissions, BootReceiver
  store/      Settings (SharedPreferences), TokenCipher (Keystore AES-GCM), PdaPrefs
  ui/         MainActivity (status + log), SettingsActivity
```

Unit tests (`app/src/test`) run on the JVM: the §8 test vectors byte for byte; a fake
watch (real CRC-32s, MTU-sized `DATA` frames, `LIST` paging, idempotent `ACK`, the busy
rule, injected CRC errors, disconnects and SD errors) with a fake server for the drain
rules; and the real `HttpUploader` against a JDK `HttpServer` on localhost (multipart
fields, bearer header, error classes, no redirects, token never in logs).

For the PDA: glance protocol vectors; a fake T5 (outbox with paging, `PUT` with CRC and
size checks, MTU-sized frames, injected bad CRCs, a lost `OB_ACKED`, `status`
notifications for the "user is active" loop) with a fake odysseus-sound API (fictional
data) and a fake Odysseus calendar; feed shape, cutting and hashing; transcript paging;
action validation; and the HTTP clients against a fake transport (paths, bodies, bearer
header, "not supported" for a hub without the codex routes) plus the real transport
against a JDK `HttpServer`.

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

The T5 PDA side is unit-tested only; no T5 firmware speaks the glance protocol yet. To
check with a real phone and T5 (plan milestone 4):

- Watch and T5 bonded to one phone: both found by the two-UUID scan, served one at a
  time, the watch drained between PDA syncs while the T5 isn't due.
- Pairing with the T5's on-screen code; `info`, MTU ≥ 420 (Android asks for 517), `TIME`
  offset shown right on the T5.
- `PUT` of a ~30 KB feed at MTU 517 (time), the feed redrawn and surviving a reboot.
- `PATCH /v1/action-items/{id}` from Android's `HttpURLConnection` (the JDK's has no
  PATCH, so tests can't cover the real call).
- `cal_add` from an action item: the event appears in Odysseus and on the CalDAV server
  (with the calendar id set); a lost `OB_ACKED` (walk out of range right after the tap)
  doesn't create a second event.
- The stay-connected loop: outbox replies within seconds while the T5 is awake, `BYE`
  when it sleeps, and the 30 s idle limit never hit.
- The "outbox waiting" flag in the T5's scan response (Service Data, plan §3.5) read by
  Android's `ScanRecord`, so a tap is synced within seconds while the T5 isn't due.
