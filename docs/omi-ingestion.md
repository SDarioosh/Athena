# Omi pendant → self-hosted ingestion: research notes

Status: **research done; the Omi relay is not built** (optional, after the XIAO; checked 2026-10-29).

Date: 2026-10-18. Source snapshots read locally: `BasedHardware/omi` @ `5448885` (2026-10-17),
`SimpleOpenSoftware/chronicle` `dev` @ `e5d5a16` (2026-09-22), `Ushadow-io/chronicle` `main` @ `087a106` (2026-01-06).
Constraint applied throughout: **no audio, transcript, or summary may leave the LAN.**

---

## 1. Summary / recommendation

**Path: direct BLE capture with a relay we control (Option 3). Phone relay first, plus an optional
Linux BLE collector at home. Don't use the Omi app or Omi cloud anywhere in the data path.**

- The official Omi app can't be made fully local:
  - The "Realtime audio bytes" webhook is sent **by Omi's cloud backend** (`routers/pusher.py`), so audio
    has already reached Omi's servers before it gets to us.
  - Custom STT ("Local Whisper", custom endpoint) can keep raw audio off Omi
    (`sendRawAudioToOmi=false`), but the app **still opens a socket to the Omi backend and sends it the
    transcripts**.
  - Release builds ignore runtime backend-URL overrides.
- Self-hosting the Omi backend means replacing Firestore/Firebase Auth/GCS/Pinecone/Redis/Typesense/OpenAI
  and building the Flutter app yourself. That's too much effort for a pipeline we already have.
- Direct BLE is simple and well documented in Omi's own `sdks/device/PROTOCOL.md`:
  - Service `19b10000-…`, audio notify characteristic `19b10001-…`, codec read characteristic `19b10002-…`.
  - Each notification is a 3-byte header followed by Opus at 16 kHz mono. CV1 uses codec 21 (20 ms frames).
  - Offline (SD/flash) audio is fetched over a separate storage service (`30295780-…`).
- **Chronicle: BORROW, don't adopt.** It's a capable MIT-licensed self-hosted stack, but it overlaps almost
  entirely with our server: FastAPI, MongoDB, Redis WAL, Deepgram by default, with Parakeet/faster-whisper
  as options. Pieces worth taking:
  1. Its React Native Omi BLE relay app (`app/`, which uses Omi's `friend-lite-react-native` SDK and has an
     Android foreground-service plugin) as a starting point for our phone relay.
  2. `extras/chronicle-wearable-sdk` (an MIT fork of Omi's Python SDK with button/haptic/storage helpers)
     for a Linux collector.
  3. Design ideas from its audio-v2 contract: raw Opus 16 kHz/20 ms uplink, ~10 s chunks keyed by
     `(capture_session_id, sequence)` with immutable `captured_at`, and a live vs recovered delivery class.
     These map closely onto our `/v1/ingest/chunk`.
- Also worth reading: `cgallic/omi-pendant-chronicle`. It's a small MIT Kotlin Android app that relays
  raw Omi-framed Opus to a self-hosted HTTP sink and drains offline storage. It's the closest existing
  thing to our phone relay, but very young (1 star).

---

## 2. Option 1: Omi app developer features (stock app + Omi cloud)

**How it works**
- *Realtime audio bytes*: in the app, go to Settings → Developer Mode → "Realtime audio bytes", then set a
  URL and an interval of X seconds. The receiver gets `POST <url>?sample_rate=16000&uid=…` with
  `application/octet-stream` raw PCM16 LE mono. [docs AudioStreaming]
  - The docs' own diagram shows `Device → Omi Backend → POST octet-stream → Your Webhook`.
  - Source confirms it: `backend/routers/pusher.py` buffers `sample_rate * delay_seconds * 2` bytes and
    enqueues `{'type':'webhook','sample_rate':…,'data':…}`, which `send_audio_bytes_developer_webhook`
    then sends. This runs server-side inside Omi's cloud.
- *Real-time transcript* and *memory-created* webhooks: Omi's backend POSTs transcript segments or the
  full conversation object (transcript, structured summary, action items) to your URL. [docs Integrations]
  So by the time these arrive, Omi's cloud has already transcribed and summarized.
- *Custom STT* (Developer Options → Transcription). Providers listed in `app/lib/models/stt_provider.dart`:
  `omi, omiParakeet, openai, deepgram, falai, gemini, localWhisper, custom, customLive, onDeviceWhisper`.
  - The app sends audio to your STT endpoint as the primary socket. A secondary socket goes to the Omi
    backend with `custom_stt=enabled` (`services/sockets/transcription_service.dart`).
  - `CustomSttConfig.sendRawAudioToOmi` (default `true`) controls whether raw audio is also forwarded to
    Omi. Issue #6637 documented that audio used to go to `api.omi.me/v4/listen` unconditionally.
  - Even with the flag off, the code comments say "suggested transcripts still reach the backend": the
    secondary socket to Omi carries the transcript text. **This breaks our constraint.**
- *Backend URL*:
  - `app/lib/env/env.dart` hardcodes `productionApiBaseUrl = 'https://api.omi.me/'`. A build-time
    `--dart-define=OMI_API_BASE_URL` is honoured.
  - `backend_url_override.dart` supports a runtime override, but `restore(..., runtimeAllowed = !kReleaseMode)`
    clears it in release builds.
  - Issue #6635 ("iOS app hardcodes api.omi.me — no way to use self-hosted backend without custom build")
    is closed, but the code still gates the override to non-release builds.
  - **The store app can't be pointed at a custom backend.**
- *Account*: the backend authenticates every request with Firebase ID tokens (`auth.verify_id_token` in
  `backend/utils/other/endpoints.py`), so the stock app needs an Omi/Firebase account.
- *Offline storage*: the device stores audio on SD/flash while disconnected. The app pulls it over the
  storage characteristic (`services/wals/storage_sync.dart`, `services/devices/ring_protocol.dart`) and
  uploads it to the Omi backend (`/v2/sync-local-files`; PR #7981 added an on-phone "offline batch"
  mode that still uses that endpoint to transcribe).

**Data path**: pendant → phone app → `api.omi.me` (STT via Deepgram/Soniox/Modulate/Parakeet, chosen
by Omi) → your webhook.

**Local-only feasible?** **No.** Audio-bytes and memory webhooks originate in Omi's cloud. Custom STT
still sends transcripts to Omi.

**Effort**: trivial (just a webhook). **Risks**: violates the hard constraint; also depends on Omi's
account, quota and billing.

**Mapping to our API**: in principle a shim could turn the PCM16 webhook into
`POST /v1/ingest/chunk` (`codec=pcm16`, `device=omi`, `session_id=uid+day`, `sequence` counter,
`timestamp = receipt time − X s`). This is shown only for completeness. Rejected.

---

## 3. Option 2: Self-host the Omi backend (`backend/`) and custom-build the app

**How it works**
- FastAPI backend.
- Prerequisites in the setup docs: OpenAI, Modulate (STT fallback), Parakeet (self-hosted STT via
  `HOSTED_PARAKEET_API_URL`), Redis (Upstash), Pinecone, Hugging Face, Modal, GitHub, Google Maps,
  Typesense, Stripe.
- `backend/requirements.txt` pins `firebase-admin`, `google-cloud-firestore`, `google-cloud-storage`,
  `google-cloud-kms`, `google-cloud-tasks`, `deepgram-sdk`, `pinecone`, `langchain-pinecone`, `openai`,
  `redis`, `typesense`.
- `backend/.env.template` also wants `GOOGLE_APPLICATION_CREDENTIALS`, GCS buckets, `DEEPGRAM_API_KEY`,
  `GEMINI_API_KEY` (TTS), and PostHog.
- Persistence is Firestore (`database/_client.py` imports `google.cloud.firestore`). Auth is Firebase Auth.
- STT is pluggable:
  - `utils/stt/streaming.py` has an `STTService` enum and an ordered `STT_SERVICE_MODELS` list.
  - `HOSTED_PARAKEET_API_URL` lets you point at a self-hosted Parakeet server.
  - There is no whisper.cpp provider; you'd add one or write a Parakeet-compatible shim.
- `PROVIDER_MODE=offline` (`.env.offline.template`) exists, but it is a **test harness with fakes**
  (`backend/testing/e2e/fakes/{firestore,redis,stt,llm,…}.py`), not a production local mode.
- App side: build the Flutter app with `--dart-define=OMI_API_BASE_URL=https://<lan-host>/`
  (`env.dart`). Because release builds also use Firebase for login, you'd keep Firebase Auth (Google
  cloud) or patch auth out.

**Data path (after full work)**: pendant → custom-built Omi app → self-hosted Omi backend → local STT/LLM.

**Local-only feasible?** **Only with large changes.** Firestore, Firebase Auth, GCS, Pinecone and
Typesense would each need a local replacement or emulator. The Firebase emulators are dev-only. Nothing
in the repo provides a supported local deployment.

**Effort**: very high (weeks), and ongoing. The repo moves very fast (13.7k stars, 1.7k open issues,
macOS releases several times a day, last push 2026-10-18), so a local fork would rot quickly.

**Risks**: maintenance burden; you'd end up running a second pipeline that duplicates our
VAD/STT/diarization/summaries; auth coupling to Google.

**Verdict**: ignore. The only parts worth reusing are the protocol docs and the app's
BLE/storage-sync code as reference.

---

## 4. Option 3: Omi SDKs / direct BLE (recommended basis)

**What's available** (`BasedHardware/omi/sdks/`: `python`, `python-cli`, `react-native`, `swift`,
`rust`, `device/{cpp,dart,go,rust,typescript}`)
- `sdks/python` is published as `omi-sdk` 0.1.0 (MIT). It uses `bleak` + `opuslib`. Examples use
  Deepgram, but there are optional `stt/whisper.py` (openai-whisper) and `stt/parakeet.py` engines.
  The STT part is irrelevant to us: we'd send audio to our server.
- `sdks/swift` is an iOS/macOS package with local Whisper. `sdks/react-native` is the BLE library family
  used by Chronicle's app (`friend-lite-react-native`).

**BLE protocol** (`sdks/device/PROTOCOL.md`, `sdks/python/omi/constants.py`, firmware `omi/firmware/omi/src/lib/core/`)

| Item | Value |
|---|---|
| Omi service | `19b10000-e8f2-537e-4f6c-d104768a1214` |
| Audio data (notify) | `19b10001-e8f2-537e-4f6c-d104768a1214` |
| Audio codec (read, 1st byte) | `19b10002-e8f2-537e-4f6c-d104768a1214`. 0=PCM16, 1=PCM8, 20=Opus 10 ms (DevKit), **21=Opus 20 ms "fs320" (Omi CV1)** |
| Battery | `0000180f-…` / level `00002a19-…` |
| Storage service | `30295780-4301-eabd-2904-2849adfeae43`. Write/notify characteristic `…5781…`, status read characteristic `…5782…` |

- **Framing**: each notification is `[packet_id lo][packet_id hi][fragment_index][payload…]`.
  - `transport.c` writes `id & 0xFF`, `id >> 8` and a per-frame `index`, then splits the codec frame
    to fit the negotiated MTU (`NET_BUFFER_HEADER_SIZE 3`).
  - Normally one notification carries one Opus frame. If the MTU is small, the frame is fragmented and
    `fragment_index` goes 0, 1, …
  - The SDK decoder just drops the 3 bytes and decodes each payload. That's correct only when nothing is
    fragmented, so a robust relay should reassemble by `packet_id`/`index`.
- **Codec (CV1, `config.h`)**: Opus, CELT mode, `RESTRICTED_LOWDELAY`, 32 kbps VBR, complexity 3,
  `CODEC_PACKAGE_SAMPLES 320` (20 ms at 16 kHz), so about 80 bytes per frame and 50 frames/s, roughly
  4 KB/s. It decodes to PCM16 LE mono at 16 kHz (`OPUS_FRAME_SAMPLES=960` is only a decode buffer bound).
- **Offline storage** (CV1 firmware ≥ 3.0.20, "ring" protocol; `storage.c`, app `ring_protocol.dart`):
  - Commands written to `…5781`: `0x10` INFO, `0x11` READ `[start_seq:u64 BE][count:u32 BE]`,
    `0x12` ADVANCE `[seq:u64]` (frees storage), `0x13` CLEAR, `0x03` STOP.
  - Notifications on the same characteristic: `0x01` ACK, `0x02` INFO, `0x03` DATA, `0x04` DONE,
    `0x05` READ_BEGIN.
  - Ring records are 444 bytes: `[timestamp:u32 BE][440 B payload packed as (len:u8, opus frame)…, zero-padded]`.
  - The `…5782` read returns `[used_bytes][unread_packets][free_bytes][rtc_valid]` (u32 LE each).
  - The timestamp appears to be device RTC epoch seconds, valid when `rtc_valid=1`. I haven't verified
    this against a real device.
  - **Older firmware uses different storage protocols.** The app has a "multi-file" mode (list/read/delete
    files) and a legacy `[0x00, file_num, offset]` mode, which Chronicle's SDK uses
    (`chronicle_wearable_sdk/bluetooth.py`). A relay has to check the firmware version
    (`RingProtocol.isRingBufferFirmware`).

**Where it can run**
- **Linux server with a BLE adapter**: `bleak` uses BlueZ, so this works if the pendant is in radio range
  of the server (one room). Good for "at desk/home" capture with no phone at all.
- **Phone relay (needed when away from home)**:
  - Android: BLE plus a foreground service works reliably. Chronicle and cgallic both use foreground
    services; cgallic mentions "BLE/Doze hardening".
  - iOS: Core Bluetooth tasks are suspended in the background unless the app declares the
    `bluetooth-central` background mode (Apple docs). The official Omi app declares `bluetooth-central`
    plus `audio`, `location` and others in `Info.plist`. Chronicle's `app.json` declares only `audio`, so
    its iOS background BLE behaviour is unverified. iOS also requires a Mac and Xcode to sideload, plus
    re-signing every 7 days on a free account or a paid developer account.
- **Battery/range**: I couldn't verify Omi-specific range or battery numbers from the docs. Expect normal
  BLE behaviour (about 10 m and through one wall). Continuous streaming costs more pendant battery than
  storing to flash and syncing in batches. cgallic's app defaults to "low-power push-to-sync" with live
  streaming as an explicit mode, which suggests a real trade-off.

**Data path**: pendant → BLE → our relay (phone or Linux) → `POST /v1/ingest/chunk` on the LAN/Tailscale →
our pipeline. No third party involved.

**Local-only feasible?** **Yes**, with no changes to Omi firmware.

**Effort**: moderate.
- Linux collector: about 1–2 days with `omi-sdk`/bleak.
- Android relay: about 1–2 weeks starting from Chronicle's RN app or cgallic's Kotlin app.
- iOS relay: more effort, mainly testing background behaviour.

**Risks**:
- Fragment reassembly and storage-protocol differences across firmware versions.
- iOS background limits.
- No vibration/stop UX unless we drive the haptic characteristic (CV1 has `haptic.c`; Chronicle's SDK
  exposes haptic control).

---

## 5. Option 4: Chronicle

**Identity**
- `Ushadow-io/chronicle` is a **fork** of upstream `SimpleOpenSoftware/chronicle` (GitHub API `fork: true`,
  `parent`). The fork has 0 stars and 0 issues. Its `main` was last committed 2026-01-06 (repo
  pushed_at 2026-10-14, on other branches).
- Upstream: MIT license; 75 stars, 28 forks, 25 open issues; created 2024-10; last push 2026-09-22;
  default branch `dev` with about 1,154 commits; 10 contributors (AnkushMalaker 831 commits,
  thestumonkey 188, …); releases v0.2.2 (2026-02), v0.3.0, v0.3.1 (2026-07-26).
- Maturity: active, but essentially one or two maintainers, and the architecture changes fast (ADRs and
  dated plan docs from 2026-07 and 2026-08).

**Architecture** (README, `docs/audio-pipeline-architecture.md`)
- React Native/Expo mobile app (iOS + Android) connects to the Omi over BLE using `react-native-ble-plx` +
  `friend-lite-react-native`.
- It streams **raw Opus 16 kHz mono, 20 ms packets** over WebSocket `ws(s)://host:8000/ws/audio` with
  subprotocol `chronicle.audio.v2`. Control messages are Protobuf JSON and media is Protobuf binary,
  defined in `contracts/audio/v2/proto/.../audio.proto`. It is **not Wyoming** on the app↔backend link.
  Wyoming appears only as an optional ASR client in the wearable SDK (`[wyoming]` extra).
- FastAPI backend → Redis audio WAL → MongoDB `AudioChunkDocument` (~10 s Opus, unique
  `(capture_session_id, sequence)`, immutable `captured_at`) → VAD/STT/pyannote → Conversations →
  memory/Markdown vault.
- **STT**:
  - Default `config/defaults.yml` uses `stt: stt-deepgram` (cloud).
  - Local options: `stt-parakeet-batch` (`PARAKEET_ASR_URL`), Nemotron, Gemma/Qwen ASR.
  - `extras/asr-services` provides faster-whisper, transformers, NeMo (Parakeet/Canary) and VibeVoice
    (diarizing) providers in Docker.
  - Local-only is possible by configuration, but it isn't the default.
- **Diarization/speaker ID**: optional pyannote-based `extras/speaker-recognition`.
- **LLM**: OpenAI by default; Ollama, llama.cpp embeddings and any OpenAI-compatible URL are configurable.
- **Offline device storage**:
  - The Python wearable SDK and `extras/local-wearable-client` implement storage reads with the
    **legacy** `[0x00, file_num, offset]` command, which doesn't match current CV1 ring firmware.
  - I found no device-storage sync code in the mobile app (`app/src` only uses AsyncStorage).
- Other relays: `extras/havpe-relay` (ESP32 voice device), `extras/local-wearable-client`
  (desktop BLE → backend, macOS menu-bar app).

**Data path (local config)**: Omi → Chronicle app (BLE) → WS `/ws/audio` → Chronicle backend
(Mongo/Redis) → local ASR → local LLM.

**Local-only feasible?** **Yes, with configuration** (swap Deepgram and OpenAI for Parakeet or
faster-whisper and Ollama/llama.cpp).

**Verdict**: **BORROW.**
- Adopting it wholesale would replace our SQLite/whisper.cpp/diarization/summary pipeline with
  Mongo + Redis + Docker services and tie us to a fast-changing protobuf protocol.
- Borrow:
  - the RN app's BLE connect/reconnect/foreground-service code (fork it and replace the WS sender with
    our HTTP chunk uploader);
  - the chunk-identity design;
  - optionally its `/ws/audio` v2 contract as inspiration for our later streaming variant.

---

## 6. Option 5: Other local-first Omi relays (brief)

| Project | What | Status |
|---|---|---|
| [cgallic/omi-pendant-chronicle](https://github.com/cgallic/omi-pendant-chronicle) | Kotlin Android app plus Python desktop collector. POSTs raw Omi-framed Opus (3-byte header kept) to `/raw` and `/raw-batch` with `X-Pendant-Secret`. 3-day on-phone ring buffer. Drains offline storage. Push-to-sync low-power default. | MIT, 1 star, last push 2026-07-23. **Closest to our needs.** Its drain is "list → read → delete files" (multi-file protocol), so check against ring firmware. |
| [thecloudcowboy/locus-tether](https://github.com/thecloudcowboy/locus-tether) | End-to-end local pipeline (Jetson Thor) with its own `omi_capture` phone app. Tailscale for remote access. | MIT, 17 stars, last push 2026-09-01. Reference only. |
| [kbdevs/omibutfree](https://github.com/kbdevs/omibutfree) | "Omi Local": Flutter iOS app. Direct BLE, on-phone SQLite, Sherpa-ONNX/Whisper on-device STT (cloud keys optional). SD sync is on the roadmap, not done. | 31 stars, no license detected by GitHub (the README badge says MIT), last push 2026-02-17. Useful iOS BLE reference. |
| [tnewtown4/omi-private](https://github.com/tnewtown4/omi-private) | Self-hosted backend for Omi, "zero third-party dependencies". | MIT, 1 star, last push 2026-02-24. Not evaluated in depth. |
| [Rahulsharma0810/omi-whisperx](https://github.com/Rahulsharma0810/omi-whisperx) | WhisperX STT that still creates Omi memories. | Uses the Omi cloud for memories, so not local. |

---

## 7. Comparison

| Option | Audio path | Cloud dependency | Offline (pendant storage) sync | Effort | Maturity | Verdict |
|---|---|---|---|---|---|---|
| 1. Omi app webhooks / custom STT | Pendant → Omi app → **api.omi.me** → webhook (custom STT: app → your STT, but transcripts → Omi) | **Hard**: Omi backend, Firebase account; Deepgram etc. inside Omi | Yes, but uploads to Omi cloud | Trivial | High (official) | **Reject** (breaks constraint) |
| 2. Self-host Omi backend + custom app build | Pendant → custom-built Omi app → self-hosted Omi backend | Firestore, Firebase Auth, GCS, Pinecone, Redis, Typesense, OpenAI/Deepgram; each needs replacing | Yes (app's WAL sync → `/v2/sync-local-files`) | Very high, ongoing | Upstream high; local deployment unsupported | **Ignore** |
| 3. Direct BLE (own relay; Omi SDK protocol) | Pendant → our phone/Linux relay → `/v1/ingest/chunk` | **None** | Yes: implement storage ring protocol (`0x10/0x11/0x12`) | Moderate | Protocol documented in Omi repo; SDK 0.1.0 | **Adopt (recommended)** |
| 4. Chronicle (upstream) | Pendant → Chronicle RN app → WS `/ws/audio` → Chronicle backend | None if configured local; defaults are Deepgram + OpenAI | Legacy storage read in Python SDK only; none in mobile app | Medium to adopt, high to keep in sync | Active, small team, 75 stars, v0.3.1 | **Borrow** (app BLE code, chunk-identity design) |
| 5. cgallic relay | Pendant → Kotlin Android app → self-hosted `/raw` | None | Yes (multi-file drain) | Low–moderate to adapt | Very young (1 star) | **Borrow / reference** |

---

## 8. Integration plan (recommended path → `POST /v1/ingest/chunk`)

**Components**
1. `omi-relay-linux` (Python, bleak; based on `omi-sdk` or `chronicle-wearable-sdk`). Runs on the Ubuntu
   server or a Pi near the desk.
2. `omi-relay-android` (fork Chronicle's RN app or cgallic's Kotlin app). Foreground service, BLE
   auto-reconnect, local disk queue.
3. iOS relay: only if the user's phone is an iPhone. Needs the `bluetooth-central` background mode and
   testing.

Both relays must never pick the pendant at the same time: BLE allows one central per connection. Rule:
the phone relay yields when it sees the home Wi-Fi SSID, or the Linux relay only scans when the phone
isn't connected.

**Capture → chunk**
- On connect, read the codec characteristic. Expect `21`; accept `20`. If the value is 0 or 1 (PCM), send
  `codec=pcm16`.
- Reassemble notifications by `(packet_id u16 LE, fragment_index)`, then strip the header to get whole
  Opus frames.
- Buffer **30 s** of frames (1,500 × 20 ms), the same as the T-Rex chunks, so the server's VAD and
  reassembly logic stays the same.
- **Codec on the wire**, two choices:
  - (a) Preferred: `codec=ogg`. Mux frames into Ogg Opus (16 kHz input rate, 1 channel, 20 ms granule
    steps) on the relay. Server decodes with libopus/ffmpeg the same way as watch chunks.
  - (b) Simpler relay: `codec=opus`. Define it as a sequence of `[len:u16 LE][opus frame]`, 16 kHz mono,
    20 ms per frame, with optional form field `frame_ms=20`. The server wraps it in Ogg or decodes it
    directly. Document the format in the API spec, since raw Opus has no container.
  - Avoid `pcm16` on the phone: about 32 KB/s versus about 4 KB/s.

**Form fields**

| Field | Live stream | Offline storage drain |
|---|---|---|
| `device` | `omi` | `omi` |
| `session_id` | New UUIDv7 per BLE connection or capture run, persisted across app restarts until disconnect > N min | New UUIDv7 per drain, or per contiguous timestamp run (split if the gap is over 3 min) |
| `sequence` | Monotonic per session from 0 | Monotonic per session from 0 |
| `timestamp` | Wall clock of the first frame in the chunk: phone/server UTC at receipt − (frames queued × 20 ms). Corrected using the BLE `packet_id` gap to account for drops | From the ring record `timestamp` (u32 BE, believed to be RTC epoch s) when `rtc_valid=1`. Otherwise estimate as `now − unread_packets × frames_per_record × 20 ms` and flag it |
| `codec` | `ogg` (or `opus` per above) | same |
| optional extras (propose) | `delivery=live`, `dropped_frames` | `delivery=recovered`, `device_seq_start` (ring seq) |

- **Idempotency**: the server dedupes on `(session_id, sequence)` (a session is bound to one device);
  a repeated upload returns `duplicate: true`. For drains, call
  `0x12 ADVANCE` only **after** the server returns `ok: true`. This mirrors the firmware's
  "advance on phone confirmation" design, so a failed upload never loses data.
- **Auth**: `Authorization: Bearer <CONVO_INGEST_TOKEN>`. Store it in Android Keystore/EncryptedSharedPreferences
  (iOS: Keychain), provisioned by QR code from the server's admin page. Transport options: HTTPS on the
  LAN via a Tailscale cert, or HTTP limited to RFC1918/Tailscale ranges (Android
  `network_security_config`, iOS ATS `NSAllowsLocalNetworking`). When there's no LAN, queue on the phone
  (cgallic keeps a 3-day ring) and upload later. Never fall back to any cloud.
- **`stop: true`**: the Omi records continuously and has no app-level "session".
  - Interpret `stop` as "close the current `session_id`, start a new one on the next speech".
  - Optionally buzz the CV1 haptic so it matches the watch's vibrate-on-stop UX. The haptic
    characteristic UUID isn't verified here; check `chronicle_wearable_sdk/uuids.py` or firmware
    `haptic.c`.
  - Don't disconnect BLE on `stop`.
- **Streaming variant (later)**: a WS endpoint taking the same fields in a JSON `start` message, followed
  by binary 20 ms Opus frames. Chronicle's audio-v2 contract is a good template.
- **Server work**: the Phase 2 server (`feature/server-pipeline`) already accepts any `device`,
  `codec=ogg|opus` (ffmpeg probes the container), `pcm16` with `sample_rate`, and dedupes retries.
  Still to add: the raw `[len][frame]` framing from option (b) if we choose it, and the `delivery`
  flag. The rest of the pipeline (Silero VAD → whisper.cpp → diarization → 3-min split →
  llama-server → SQLite) is unchanged.

**Milestones**
1. Linux relay prototype: bleak connect, codec read, reassembly, 30 s Ogg chunks, POST.
2. Server support for `device=omi` plus dedupe.
3. Storage drain on Linux (ring protocol) with ADVANCE after ack.
4. Android relay (fork), foreground service, disk queue.
5. Optional: iOS relay and the streaming WS.

---

## 9. Open questions for the user

1. **Phone OS: Android or iPhone?** On iOS the relay must be sideloaded via Xcode on the Mac (7-day
   re-sign on a free account, or $99/yr), needs `bluetooth-central` background mode, and background
   reliability needs testing.
2. Which Omi hardware and firmware version? (CV1 vs DevKit 1/2; firmware ≥ 3.0.20 decides which storage
   protocol to implement.)
3. Is the server reachable from the phone when you're away (Tailscale)? If not, how many hours or days
   should the phone queue?
4. Live streaming or low-power "store on pendant and sync when home"? This trades pendant battery against
   latency.
5. Is the official Omi app still used on any device? It must be uninstalled or unpaired from the pendant,
   or it will compete for the BLE connection and upload to Omi's cloud.
6. Should `stop: true` trigger the pendant haptic, or just rotate sessions silently?

---

## 10. Sources

Omi docs
- https://docs.omi.me/doc/developer/apps/AudioStreaming — realtime audio bytes (PCM16, `?sample_rate&uid`, flows via Omi backend)
- https://docs.omi.me/doc/developer/apps/Integrations — memory-created / real-time transcript webhooks
- https://docs.omi.me/doc/developer/backend/Backend_Setup — backend prerequisites (OpenAI, Modulate, Parakeet, Redis, Pinecone, HF, Modal, Typesense, Stripe…)
- https://docs.omi.me/doc/developer/sdk/sdk — SDK overview

Omi source (BasedHardware/omi @ 5448885, main)
- https://api.github.com/repos/BasedHardware/omi — 13.7k stars, MIT, pushed 2026-10-18
- https://github.com/BasedHardware/omi/blob/main/sdks/device/PROTOCOL.md — GATT UUIDs, codec IDs, 3-byte header, 16 kHz
- https://github.com/BasedHardware/omi/blob/main/sdks/python/omi/constants.py , `decoder.py`, `bluetooth.py`, `pyproject.toml` (omi-sdk 0.1.0)
- https://github.com/BasedHardware/omi/blob/main/omi/firmware/omi/src/lib/core/transport.c — header bytes (packet id LE + fragment index)
- https://github.com/BasedHardware/omi/blob/main/omi/firmware/omi/src/lib/core/config.h — Opus CELT 32 kbps, 320-sample frames, CODEC_ID 21
- https://github.com/BasedHardware/omi/blob/main/omi/firmware/omi/src/lib/core/storage.c and `sd_card.h` — storage UUIDs, ring commands, 444-byte records
- https://github.com/BasedHardware/omi/blob/main/app/lib/services/devices/ring_protocol.dart — ring wire format, firmware ≥ 3.0.20
- https://github.com/BasedHardware/omi/blob/main/app/lib/services/wals/storage_sync.dart — multi-file storage protocol
- https://github.com/BasedHardware/omi/blob/main/app/lib/env/env.dart and `backend_url_override.dart` — api.omi.me default, override disabled in release
- https://github.com/BasedHardware/omi/blob/main/app/lib/models/stt_provider.dart , `models/custom_stt_config.dart`, `services/sockets/transcription_service.dart` — custom STT, `sendRawAudioToOmi`, transcripts still to backend
- https://github.com/BasedHardware/omi/blob/main/app/ios/Runner/Info.plist — `bluetooth-central` background mode
- https://github.com/BasedHardware/omi/blob/main/backend/routers/pusher.py — server-side audio-bytes webhook
- https://github.com/BasedHardware/omi/blob/main/backend/requirements.txt , `backend/.env.template`, `backend/.env.offline.template`, `backend/utils/stt/streaming.py`, `backend/utils/other/endpoints.py`, `backend/database/_client.py`
- https://github.com/BasedHardware/omi/issues/6635 — store app hardcodes api.omi.me
- https://github.com/BasedHardware/omi/issues/6637 — custom STT still streamed audio to Omi
- https://github.com/BasedHardware/omi/pull/7981 — offline (batch) capture mode

Chronicle
- https://github.com/Ushadow-io/chronicle , https://api.github.com/repos/Ushadow-io/chronicle — fork of SimpleOpenSoftware/chronicle
- https://github.com/SimpleOpenSoftware/chronicle , https://api.github.com/repos/SimpleOpenSoftware/chronicle (stars/push/license), `/contributors`, `/releases`
- https://github.com/SimpleOpenSoftware/chronicle/blob/dev/app/README.md — RN app, `/ws/audio`, `chronicle.audio.v2`
- https://github.com/SimpleOpenSoftware/chronicle/blob/dev/contracts/audio/v2/README.md and `proto/backend/audio_contract/v2/audio.proto`
- https://github.com/SimpleOpenSoftware/chronicle/blob/dev/docs/audio-pipeline-architecture.md
- https://github.com/SimpleOpenSoftware/chronicle/blob/dev/extras/asr-services/README.md
- https://github.com/SimpleOpenSoftware/chronicle/blob/dev/config/defaults.yml — Deepgram default STT
- https://github.com/SimpleOpenSoftware/chronicle/tree/dev/extras/chronicle-wearable-sdk — SDK fork; legacy storage read in `bluetooth.py`
- https://github.com/SimpleOpenSoftware/chronicle/blob/dev/app/app.json — iOS `UIBackgroundModes: [audio]`; `plugins/with-ws-fgs.js` (Android FGS)

Other
- https://github.com/cgallic/omi-pendant-chronicle
- https://github.com/thecloudcowboy/locus-tether
- https://github.com/kbdevs/omibutfree
- https://github.com/tnewtown4/omi-private
- https://github.com/Rahulsharma0810/omi-whisperx
- https://developer.apple.com/library/archive/documentation/NetworkingInternetWeb/Conceptual/CoreBluetooth_concepts/CoreBluetoothBackgroundProcessingForIOSApps/PerformingTasksWhileYourAppIsInTheBackground.html — iOS `bluetooth-central` background mode (from search snippet; full page didn't extract)

Not verified: Omi BLE range and battery life; meaning of the ring-record timestamp (assumed RTC epoch seconds);
CV1 haptic characteristic UUID; Chronicle iOS background BLE behaviour; tnewtown4/omi-private claims.
