# Plan: Seeed Studio XIAO ESP32-S3 Sense as a convo-ai recorder

Status: **plan only, nothing implemented.** The XIAO comes **after the T-Watch Ultra**,
on the same shared firmware ([t-watch-ultra-plan.md](t-watch-ultra-plan.md), owner
decision 2026-10-19). Decisions 3, 4 and 7 below are answered; the rest wait until XIAO
work starts.

## Decisions so far

- **Transport: BLE to the phone only.** The XIAO never talks to the server directly. The
  Android companion app (Kotlin) receives the audio over BLE and uploads it.
- **Wi-Fi: disabled.** It's not initialised and not linked into the firmware. It can be
  added later as its own feature behind the same uploader interface (§6).
- **Goal: 24/7 conversation tracking with the longest possible battery life.** Every
  design choice below gives power priority over latency. Audio can reach the server
  minutes late; that's fine.

## TL;DR

The server already accepts audio from any device (`POST /v1/ingest/chunk`, free-form
`device`, ffmpeg decodes Ogg/Opus). Supporting the XIAO means:

1. **Low-power firmware.** The mic is always on. A cheap on-device speech gate decides
   when to keep audio. Only speech is Opus-encoded and queued (PSRAM first, SD as
   overflow). The CPU runs at a low clock, the radio is off most of the time, and the
   camera is removed.
2. **Batched BLE transfer.** The phone stays connected on a slow, low-power link. Every
   few minutes (or when the queue fills) both sides switch to a fast link, transfer
   everything, and drop back to slow.
3. **Android companion app** (already planned): keeps the BLE connection alive in a
   foreground service, uploads chunks to the server (over Tailscale when away from
   home), acks them so the device can delete them, and sends the time to the device.
4. **Small server change:** chunks routinely arrive late and in batches, so sessions are
   finalised when the device says the session ended, not on an idle timer.

**Honest power caveat:** the ESP32-S3 is not a low-power audio chip. While the mic is
running the chip most likely can't use light sleep (§7.2). Rough, unmeasured estimate:
**20–35 mA average → roughly 30–50 h on a 1000 mAh cell, 12–20 h on 400 mAh.** 24/7 wear
probably means a reasonably large battery plus a daily charge. Milestone 2 measures the
real number before we commit to the rest. The fallback options are in §7.4.

## 1. Hardware facts (XIAO ESP32-S3 Sense)

| Item | Fact | Implication |
|---|---|---|
| SoC | ESP32-S3, dual-core up to 240 MHz, Wi-Fi + BLE 5 | Run at 80 MHz or less. Wi-Fi stays off |
| Memory | 8 MB PSRAM, 8 MB flash | PSRAM is the main audio queue (about 1 h of speech at 16 kbps) |
| Mic | PDM digital mic: CLK = GPIO42, DATA = GPIO41 | I2S in PDM-RX mode. JTAG is unavailable while the mic runs |
| microSD | SPI: SCK GPIO7, MISO GPIO8, MOSI GPIO9, CS GPIO21 | Overflow storage only (it costs power). **The user LED shares GPIO21** |
| Camera | Detachable OV2640/OV3660 module | **Unplug it physically.** No need to leave it in standby |
| Battery | BAT+/BAT− pads, LiPo charging over USB-C | **No battery-voltage sense** (Seeed confirms it). Needs an external divider |
| Charge LED | Hard-wired | Can't be disabled. It only lights while charging |
| Antenna | External U.FL (included) | BLE needs it for reliable range to a phone in a pocket |
| RTC | No battery-backed clock | Wall-clock time comes from the phone over BLE (§4) |
| Free GPIOs (SD in use, camera off) | D0–D6 (GPIO1–6), GPIO43/44 | Button, battery divider, optional LED/motor |

Sources: Seeed wiki pin-multiplexing page for XIAO ESP32S3 (Sense); Seeed getting-started
page (no battery ADC); the Sense-board pin map in `developing-today/hardware-doc`.

### Extra parts

- **LiPo battery**: as big as the form factor allows (aim for ≥ 800 mAh for all-day
  wear). It solders to the BAT pads.
- **Battery divider**: 2 × 1 MΩ (high value to keep the drain to about 2 µA) from BAT+ to
  GPIO1 (A0), with 100 nF to GND. Read it rarely (every 5 min).
- **Privacy/mute button** → GPIO2 (D1) to GND. A press toggles mute (mic and encoder off,
  for consent situations); a long press triggers "pair/sync now". Wake on GPIO edge.
- **Optional:** a status LED on GPIO4, and a tiny vibration motor on GPIO3 (with a driver
  transistor) for "muted/unmuted" and "battery low" only. Both stay off by default for power.
- **microSD card**: overflow only. A small, low-idle-current card is better. Measure idle
  draw; if the phone link turns out reliable, running with no card at all is an option.

Pin choices are a proposal. Check them against the final wiring.

## 2. Audio pipeline on the device

```
PDM mic → I2S DMA (16 kHz, 16-bit) → DC removal / HPF + gain → speech gate
   → [pre-roll ring buffer] → Opus encoder (speech only) → Ogg pages → queue (PSRAM → SD)
```

- **Always-on capture** at 16 kHz mono with large DMA buffers (about 100–200 ms per
  interrupt) so the CPU wakes rarely.
- **Speech gate** (cheap and permissive, because Silero VAD on the server is the real
  judge):
  - Per 20 ms frame: energy above an adaptive noise floor, plus a zero-crossing and
    band-energy check.
  - Opens after about 300 ms of speech-like frames. Includes **2–5 s of pre-roll** from a
    PCM ring buffer, so the first words aren't lost.
  - Closes after about 10–20 s with no speech (hangover), so pauses in a conversation
    stay in one chunk.
  - The Opus encoder runs only while the gate is open. In silence the CPU only does DMA
    and the gate maths.
  - Later option: ESP-SR's WebRTC VAD or a tiny NN VAD if the energy gate turns out too
    leaky. Measure the CPU cost first.
- **Opus settings**: 16 kHz mono, **16 kbps VBR, 20 ms frames, low complexity (0–3),
  DTX on**. Validate transcription quality at 12 / 16 / 24 kbps in milestone 1 and pick the
  lowest bitrate that doesn't hurt whisper accuracy. Fewer bytes means less radio time.
- **Chunks**: a chunk is a speech segment capped at 30 s. Each chunk is a standalone
  **Ogg/Opus file**, so the server decodes it unchanged with `codec=opus`. Chunk metadata
  (session, sequence, start timestamp) goes in a small header record next to the audio.
  Gaps between chunks are silence the server never receives.
  - Alternative if Ogg muxing on the device is a pain: length-prefixed raw Opus packets,
    wrapped into Ogg **on the phone** before upload. Firmware stays simpler, and the
    server is unchanged.
- **Queue**:
  - Ring buffer in PSRAM: about 6 MB, which is about 50 min of speech at 16 kbps.
  - Spill to SD in large sequential writes (≥ 64 KB) only if the phone has been
    unreachable long enough that PSRAM passes ~70%.
  - Delete a chunk only after the phone acks it (meaning the phone has it stored safely).
  - Power loss loses the un-flushed PSRAM contents. Accepted trade-off: flushing to SD
    all the time costs more than the occasional loss. Revisit if it matters in practice.

## 3. BLE design

Use the **NimBLE** stack (smaller and lighter than Bluedroid). The device is the
peripheral and the phone is the central.

### 3.1 Link power strategy

| State | Settings |
|---|---|
| Not connected | Advertise slowly (1–2 s interval, low TX power) and stop after a while; then advertise in short bursts every few minutes. Advertising resumes immediately after a button long press or when the queue is high |
| Connected, idle | Peripheral asks for a **long connection interval (~1 s) with peripheral latency** (it may skip events while it has nothing to send). The phone app requests `CONNECTION_PRIORITY_LOW_POWER`. Staying connected this way is cheaper than reconnecting every time |
| Burst transfer | Triggered by queue ≥ N min of audio (default 5–10 min), a session end, or the phone asking. Switch to `CONNECTION_PRIORITY_HIGH`, **2M PHY**, data-length extension and MTU 247–517; send everything, then drop back to idle |
| Phone gone | Keep recording into the queue and fall back to burst advertising |

The radio is then on for seconds per hour. Exact Android behaviour for these priorities
varies by phone, so measure it.

### 3.2 Custom GATT service (convo-ai chunk transfer)

| Characteristic | Props | Purpose |
|---|---|---|
| `status` | read / notify | state (listening / speech / muted), battery mV, queue depth (chunks, seconds, bytes), SD free, firmware version, `boot_id` |
| `chunk_index` | read / notify | list of pending chunks: `{chunk_id, session_id, sequence, start_ts, duration_ms, bytes}` |
| `chunk_data` | notify | chunk bytes in MTU-sized frames: `{chunk_id, offset, payload}` |
| `control` | write (with response) | `time_sync(epoch_ms)`, `request(chunk_id[, offset])` to resume, `ack(chunk_id)` to delete, `session_end_ack(session_id)`, `mute(bool)`, `set_config(key, value)` |
| `events` | notify | `session_started(id, ts)`, `session_ended(id, ts, last_sequence)` |

- **Resumable transfers**: a request with an offset resumes after a disconnect. The phone
  checks the length and a CRC32 per chunk before it stores the chunk.
- **Delivery guarantee**: the device deletes a chunk only after `ack`. The phone sends
  `ack` once the chunk is stored in its own storage, and uploads it to the server
  separately. Every step is idempotent (the server dedupes on session + sequence), so a
  lost ack just means the chunk is sent again.
- **Optional**: also expose Omi's audio service (`19B10000-E8F2-537E-4F6C-D104768A1214`,
  codec `20` = Opus 16 kHz) for live listening by Omi-ecosystem tools. It's off by
  default, because live streaming defeats batching.
- **Security**: use BLE bonding (LE Secure Connections, Just Works or a passkey shown in
  the phone app). After bonding, accept connections only from bonded devices. The server
  ingest token stays on the **phone**; the XIAO never needs it.

### 3.3 Android companion app responsibilities

- A foreground service with a persistent connection (use `CompanionDeviceManager` for
  background-friendly reconnects). Auto-reconnect when the device is in range.
- Send `time_sync` on every connect and about every hour.
- Run the burst transfers: pull chunks, check them, store them in app storage, `ack` the
  device, then upload each one to `/v1/ingest/chunk` (`device=xiao-s3-<mac4>`,
  `codec=opus`), retrying until the server accepts it.
- On `session_ended`, upload the remaining chunks for that session, then call
  `POST /v1/sessions/{id}/end`.
- When the server is unreachable (no Tailscale / offline), keep the chunks queued on the
  phone. The phone has far more storage than the device. Upload resumes later.
- Show device status: battery, queue depth, last sync, muted.

## 4. Time and sessions

- **Time**: the device has only a monotonic clock. The phone writes the epoch over
  `time_sync`; the device stores the offset and re-syncs regularly (crystal drift is
  small). Until the first sync, chunks carry `{boot_id, monotonic_ms}`. They get
  converted at sync time, or the phone converts them using the offset it last wrote. A
  chunk never reaches the server without a wall-clock timestamp.
- **Sessions = conversations as the device sees them**:
  - A session starts when the speech gate opens and nothing is active.
  - It ends after `session_gap` (default 3 min, matching the server's conversation split)
    with no speech, or after a max length (e.g. 2 h), or on mute.
  - Session ID: random 64-bit hex.
  - The server's `stop` reply is ignored for live recording, because the device's gate
    owns session boundaries and replies arrive late anyway. The phone just logs it.

## 5. Server changes needed

1. **Finalise on explicit end, not on idle (required).** With batched BLE, chunks for a
   session often arrive 5–60+ min after they were recorded, so the current
   `session_idle_seconds` (600 s, wall-clock) would close and process sessions too early.
   Proposed:
   - Sessions from devices with `store_and_forward=true` (a form field or per-device
     config) are finalised only by `POST /v1/sessions/{id}/end`, or by a long safety
     timeout (e.g. 24 h).
   - If a chunk still arrives after finalisation, re-open the session and reprocess it.
   - First check what `store.add_chunk` does today for a non-`recording` session.
2. **Gapped sessions:** chunks are speech segments with silence gaps between them. Check
   that transcript offsets, diarization, and conversation splitting use each chunk's
   `started_at`, not a cumulative audio length.
3. **Silence stop:** `silence_stop_seconds` / `max_session_seconds` don't apply to this
   device (the device decides). Make the stop logic skippable per device.
4. **Device status (optional):** accept optional `battery_mv`, `queue_seconds`, `fw`
   fields from the phone, and expose last-seen per device for the app.
5. **Tests:** a fixture Ogg/Opus chunk from the real device; late, out-of-order, and
   after-end ingest; a gapped-session transcript timeline.

Transcription, LLM summaries, search, and MCP are unchanged.

## 6. Firmware project layout (proposal)

```
firmware/xiao-esp32s3/            # ESP-IDF 5.x (PlatformIO or idf.py)
├── sdkconfig.defaults            # PM + tickless idle on, NimBLE, Wi-Fi not linked, PSRAM on
├── main/
│   ├── app_main.c                # tasks + state machine
│   ├── mic.c                     # I2S PDM RX, DC removal, gain
│   ├── speech_gate.c             # energy/ZCR gate, pre-roll ring buffer
│   ├── encoder.c                 # libopus + Ogg page writer
│   ├── queue.c                   # PSRAM ring + SD spill, ack/delete, chunk index
│   ├── transport.h               # uploader interface (BLE now, Wi-Fi later)
│   ├── transport_ble.c           # NimBLE GATT service, link-parameter switching
│   ├── power.c                   # DFS config, battery ADC, low-battery behaviour
│   └── ui.c                      # mute button, optional LED/motor
└── test/                         # host tests: gate, Ogg writer, queue/ack logic
```

- **ESP-IDF rather than Arduino**, because this design depends on power management
  (`esp_pm` DFS, tickless idle), NimBLE tuning, and leaving Wi-Fi out of the build. Arduino
  hides or ships those differently. (Arduino as an IDF component is an acceptable middle
  ground if libraries are needed.)
- **Wi-Fi off**: don't call `esp_wifi_init`, and keep Wi-Fi out of the image. A future Wi-Fi
  uploader would implement `transport.h` and ship as a separate, opt-in feature.
- Config (gate thresholds, bitrate, session gap) lives in NVS and is set from the phone
  through `set_config`. No secrets live on the device except the BLE bond keys.
- Repo hygiene: the old T-Watch `firmware/` was removed, so this project starts fresh
  in `firmware/xiao-esp32s3/`. `.gitignore` ignores every `secrets.h` and
  `firmware-archive/`.

## 7. Power budget

### 7.1 Where the current goes (estimates to replace with measurements)

| Consumer | Estimate | Lever |
|---|---|---|
| CPU + I2S DMA running continuously, 80 MHz | ~15–25 mA | Lowest stable clock, large DMA buffers, tickless idle between interrupts |
| Opus encoding (only while speech) | +5–15 mA while active | Low complexity, 16 kbps, encode only gated speech |
| PDM mic | ~0.5–1 mA | — |
| BLE idle connection (1 s interval + latency) | ~0.1–0.5 mA avg | Long interval, peripheral latency, low TX power |
| BLE bursts | a few mA averaged | Batch rarely, 2M PHY, large MTU |
| PSRAM | ~1–3 mA? | Unavoidable if used. Measure; it may be cheaper than SD writes |
| microSD idle | 0.1–1+ mA depending on card | Overflow only; try running without a card |
| Camera module | unknown | **Unplug it** |
| Battery divider | ~2 µA | 1 MΩ resistors |

### 7.2 Key uncertainty: light sleep with the mic running

The ESP-IDF I2S driver most likely holds a power-management lock while the channel is
enabled, which blocks automatic light sleep. If so, the chip can't sleep between DMA
interrupts, and the floor is "active at low MHz" (~15–25 mA) rather than the 2–3 mA
light-sleep floor. **Milestone 2 confirms this.** It sets the whole battery-life figure.

### 7.3 Battery-life estimates (unverified)

At an average of 20–35 mA: 400 mAh → ~12–20 h; 1000 mAh → ~30–50 h; 1500 mAh → ~45–75 h.

### 7.4 If the measured number is too low

1. **Hardware wake-on-sound:** add a low-power acoustic-activity-detect mic (e.g. Vesper
   VM3011, about 10 µA in detect mode) or an analog mic with a comparator. The ESP32
   stays in light sleep, the external chip wakes it on sound, and the pre-roll is lost
   (or kept by the VM3011's own buffer). This is the biggest possible win, and it's
   hardware work.
2. **Duty-cycled listening:** sample 1 s out of every N during long silences. This loses
   the start of conversations and is a worse fit for "tracking".
3. **Different board:** the Omi device itself uses the **XIAO nRF52840 Sense**. The
   nRF52 is far more efficient for always-on audio + BLE. If 24/7 on a small battery is
   a hard requirement, this is the realistic route. The phone app and server work above
   transfer unchanged.

## 8. Milestones and acceptance tests

| # | Milestone | Done when |
|---|---|---|
| 0 | Docs | AGENTS.md / MISSION.md list the XIAO (BLE-only, Wi-Fi off), hardware facts, checks (`idf.py build`, host tests) |
| 1 | Mic + codec quality | Record test speech at 12/16/24 kbps Opus, run it through the server pipeline, pick a bitrate; tune gain/HPF |
| 2 | **Power baseline** | Current measured (USB power meter or PPK2 on BAT) for: idle mic + gate, speech + encoding, BLE idle, BLE burst, SD write; light-sleep question (§7.2) answered; **go/no-go on the ESP32-S3 for 24/7** |
| 3 | Gate + queue | An 8 h bench run with a mix of speech and silence: queued audio ≈ speech time, nothing missed at speech onsets (pre-roll works), PSRAM→SD spill works |
| 4 | BLE transfer | A test script (`bleak` on a PC) acts as the central: index, resumable transfer, ack/delete, time sync, survives forced disconnects mid-chunk |
| 5 | Android app link | The phone app holds the connection in the background for 24 h, bursts on schedule, uploads, sends `/end`; conversations show up processed |
| 6 | Server late-chunk handling | §5.1–5.3 are implemented with tests; a session uploaded 1 h late is processed once, completely |
| 7 | Wear test | 24 h on the body: battery life recorded, conversations captured vs. missed, false-trigger rate |

## 9. Risks

- **Battery life** (above). Main risk; milestone 2 is the gate.
- **Mic quality**: the PDM mic is small and quiet, and in a pocket or under clothing
  speech gets muffled. Test wearing positions early. An external I2S MEMS mic (e.g.
  ICS-43434) is the upgrade path.
- **Speech-gate misses**: a too-strict gate loses quiet speakers, and a too-loose one
  wastes power. Thresholds are tunable from the phone.
- **Android background limits**: vendor battery savers kill BLE services. Use a
  foreground service plus `CompanionDeviceManager`, and document per-vendor settings.
- **Data loss on power cut** (PSRAM queue). Accepted for now.
- **Heat** is low with Wi-Fi and the camera off, but check it inside the enclosure.
- **Privacy/consent**: 24/7 recording of other people. The mute button is a requirement,
  and the consent notes must cover this device.

## 10. Open decisions (owner)

1. **Battery and form factor**: which capacity fits? Is a daily charge acceptable if
   true multi-day isn't reachable on the ESP32-S3?
2. **Fallback**: if milestone 2 shows < 24 h on your battery, should we go to the
   wake-on-sound mic mod, or switch to the XIAO nRF52840 Sense?
3. ~~**Framework**~~: **decided 2026-10-19: ESP-IDF**, one shared firmware with the
   T-Watch Ultra (`firmware/recorder/` plus a board layer). This replaces the
   `firmware/xiao-esp32s3/` layout in §6.
4. ~~**Server late-chunk policy** (§5.1)~~: **decided 2026-10-19: yes**. Finalise on
   explicit `/end` + 24 h safety timeout + re-open on late chunks.
5. **Extra parts**: mute button + battery divider (recommended minimum)? LED and vibration
   motor, or none?
6. **microSD**: keep it as overflow, or run card-less and rely on PSRAM + the phone?
7. ~~**Omi live-audio service**~~: **decided 2026-10-19: skip it.**

**Hold:** no XIAO-specific code until the open ones are answered. Shared firmware and
server work proceed under the T-Watch Ultra plan.

## Not verified yet

- Whether I2S PDM RX blocks light sleep on the ESP32-S3 (§7.2), and every current figure
  in §7.
- Opus encoder CPU load on the ESP32-S3 at 16 kHz, complexity 0–3, and which library
  port to use.
- Whisper accuracy at 12–16 kbps Opus from this mic.
- How well Android phones actually keep long connection intervals in the background.
- What `store.add_chunk` does for chunks arriving after a session ended, and how the
  pipeline lays out gapped chunks in a session timeline.
