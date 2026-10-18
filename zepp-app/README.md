# Convo Rec Test (T-Rex 3 Pro)

A Zepp OS Mini Program for checking whether the T-Rex 3 Pro can record conversations
in 30-second Opus chunks **with the screen on and with the screen off**. It also checks
whether the phone can upload those chunks to the convo-ai server over Tailscale. The full
recording app is built after these results are in.

## Why this test matters

The Zepp OS docs say that when the screen turns off inside a Mini Program, **the system
exits the app after about 10 s** ([setWakeUpRelaunch](https://docs.zepp.com/docs/reference/device-app-api/newAPI/display/setWakeUpRelaunch/)).
Background "App Services" can't use timers, and their API table lists only *playback*
for `@zos/media` ([App Service](https://docs.zepp.com/docs/guides/framework/device/app-service/)).
So recording probably needs the page alive. This app measures what really happens:

| Measurement | Meaning |
|---|---|
| `chunks` / `rec` vs `wall` time | recorded seconds vs elapsed seconds |
| `interr.` + `relaunch` | page was killed mid-recording and the app later reopened |
| `late` | the 30 s timer fired late: JS was suspended but the app wasn't killed |
| `max gap` | longest hole between chunks |
| `B/s`, `empty` | bytes per second of full chunks; 0-byte files mean the mic didn't record |

## Build

Needs Node ≥ 20.19 (zeus-cli 1.9 requires it; this server has Node 22 at `~/tools/node22`).

```bash
npm i -g @zeppos/zeus-cli          # or use npx
cd zepp-app
npm install
zeus build                         # → dist/*.zab for both T-Rex 3 Pro sizes
node --test tests/*.mjs            # logic tests (state machine, envelope, base64)
```

## Install on the watch

1. Zepp app on your Android phone: enable Developer Mode. Profile → Settings → About →
   tap the Zepp logo 7 times. See [docs](https://docs.zepp.com/docs/guides/tools/zepp-app/).
2. `zeus login`, then `zeus preview` in `zepp-app/`. Pick the T-Rex 3 Pro target and scan
   the QR code in the Zepp app (Profile → Developer Mode → Scan).
   - The preview uploads the *app package* to Zepp to make the QR code. No audio is
     involved (dev tooling).
3. In the Zepp app, open the mini app's **Settings** and enter:
   - **Server URL**, e.g. `http://100.x.y.z:8000` (the server's Tailscale IP) or
     `https://<host>.<tailnet>.ts.net` (see the note on HTTP below).
   - **Ingest token**: `CONVO_INGEST_TOKEN` from `server/.env`.

## Test protocol (about 30 min)

Keep the phone nearby with Bluetooth on. Talk or play a podcast near the watch so chunks
contain speech.

**Test A: screen ON, 10 min.** Press **Screen ON**. The display stays lit. Wait 10 min and
press **STOP**. Write down the five result lines.

**Test B: screen OFF, 10 min.** Press **Screen OFF** and lower your wrist. Don't touch
the watch for 10 min. Then raise your wrist:
- If the app is showing, press **STOP**.
- If you see the watch face instead, open *Convo Rec Test* again. It notices the
  interrupted recording, logs the gap and resumes. Press **STOP**.

Write down the result lines and whether the app or the watch face appeared on wake.

**Test C (optional): screen OFF, wake every 2 min.** Same as B, but raise your wrist
every 2 minutes. This shows whether frequent wakes keep it alive.

**Test D: upload.** Press **Ping**, which should show `server OK (… ms)`. Then press
**Upload**, which sends the last test's chunks one at a time. Expect roughly 10–30 s each
over BLE. Check on the server:

```bash
curl -s -H "Authorization: Bearer $CONVO_INGEST_TOKEN" http://127.0.0.1:8000/v1/sessions | python3 -m json.tool | head -30
```

Report back:

| test | wall | rec | chunks | interr. | late | relaunch | max gap | B/s | empty | woke to app? |
|---|---|---|---|---|---|---|---|---|---|---|
| A screen on | | | | | | | | | | — |
| B screen off | | | | | | | | | | |
| C wake/2 min | | | | | | | | | | |
| D ping / upload | ping: | uploaded: | | | | | | | | |

## Known unknowns (not verifiable without the watch)

- **Recorder output format:** the API docs say `codec.OPUS`; the 3.0 release notes mention
  MP3. The server decodes with ffmpeg, which probes the container, so either should
  work. Test D confirms it.
- **Microphone permission:** none is documented for `@zos/media`. If `empty` > 0 or
  the screen shows `ERROR … recorder.start failed`, that's the first suspect.

If a test button doesn't start recording, the screen shows `ERROR` and the failing step
(`create recorder`, `setFormat`, `recorder.start`, or `recorder is IDLE 2 s after start`).
The watch only buzzes when recording really started. **Clear** dismisses the error.
- **HTTP vs HTTPS:** Android may block plain `http://` from the Zepp app. If Ping fails
  with a network error, use HTTPS via Tailscale (`tailscale serve --https=443
  http://127.0.0.1:8000` on the server, then `https://<host>.<tailnet>.ts.net` as the URL).
- **BLE upload speed:** about 8 KB/s has been reported for file transfer. A 30 s Opus
  chunk is about 60–120 KB, so live upload during recording may need tuning.
- **appId:** `1092611` is a placeholder. If `zeus preview` complains, create the app in the
  [Zepp developer console](https://console.zepp.com/) and put its ID in `app.json`.

## Layout

- `page/index.js`: watch UI, recorder, display policy, upload over BLE.
- `shared/session.js`: recording-test state machine (pure, unit-tested).
- `shared/envelope.js`: binary chunk envelope and base64 (pure, unit-tested).
- `shared/uploader.js`: phone-side HTTP calls (tested against a running server).
- `app-side/index.js`: Side Service in the Zepp phone app.
- `setting/index.js`: server URL and token settings page in the Zepp app.
