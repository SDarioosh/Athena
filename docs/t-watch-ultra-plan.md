# Plan: LilyGO T-Watch Ultra as an odysseus-sound recorder

Status: **milestone 1 verified on the owner's watch (2026-10-19). The BLE upload path
(milestones 5 and 6: Opus chunks, queue, BLE service, PC relay, Android app) is built and
tested off-device, not yet on hardware** (§11). Milestones 2–4 (power, mic quality, speech
gate) are still open.

## Why this watch

- The owner has a **T-Watch Ultra** and a **T-Watch S3**. The Ultra is the target. The S3
  is left out for now (§10).
- Both use the same ESP32-S3 chip as the planned XIAO ESP32-S3 Sense pendant
  ([xiao-esp32s3-sense-plan.md](xiao-esp32s3-sense-plan.md)). So this is the same recorder
  on a second board, not a separate project.
- The Ultra matches what the XIAO plan relies on:
  - a **1100 mAh battery** (the XIAO plan asks for ≥ 800 mAh)
  - a **microSD slot** for overflow storage
  - a **PDM mic**
- It also has things the XIAO doesn't: a battery-backed **RTC**, a **fuel gauge** in the
  power chip, a **screen**, a **vibration motor** and buttons. These come for free, with
  no soldering.
- Unlike the T-Rex (Zepp OS), we write the whole firmware. Screen-off recording isn't a
  question here. Battery life is the one big unknown, so it gets measured first (§8,
  milestone 2).

**This reverses a past decision.** The earlier T-Watch design (a PlatformIO
battery-logging sketch, removed in commit `756339e`) was dropped. The owner confirmed
on 2026-10-19: **the Ultra comes first, the XIAO later**, on the same firmware. This
plan starts fresh on the XIAO design.

## Decisions carried over from the XIAO plan

- **Transport: BLE to the phone only.** The Android companion app receives the audio
  and uploads it to the server. The watch never talks to the server.
- **Wi-Fi: off.** It's never initialised and isn't linked into the firmware.
- **Goal: 24/7 conversation tracking with the longest possible battery life.** Audio
  may reach the server minutes late.

## TL;DR

1. **One firmware for both boards.** It covers the audio pipeline, speech gate, Opus
   encoding, queue and BLE service, all from XIAO plan §2–§4. A small **board layer**
   handles the parts that differ: mic pins, storage, power chip, buttons, screen and
   haptics. The Ultra is the first board; the XIAO becomes a second board file.
2. **Ultra board layer:**
   - Turn off every power rail we don't need: GPS, LoRa, NFC, speaker, and the sensor
     unless raise-to-wake is wanted.
   - Read the AXP2101 fuel gauge.
   - Keep wall-clock time in the PCF85063A RTC.
   - Show a status screen on the AMOLED only on a button press.
   - Use the vibration motor to confirm mute/unmute.
3. **Phone app and server changes: the same as the XIAO plan** (XIAO §3.3 and §5). The
   only difference is the `device` name.
4. **Power:** expect roughly the same 20–35 mA as the XIAO, unverified. Light sleep
   probably isn't possible while the mic runs (XIAO §7.2). On 1100 mAh that gives
   **roughly 30–50 h**. LilyGO's own measured board floor (light sleep 2.1 mA, deep
   sleep 0.84 mA) adds a little on top.

## 1. Hardware facts (T-Watch Ultra)

Main sources: LilyGO's hardware reference in
[LilyGoLib `docs/hardware/lilygo-t-watch-ultra.md`](https://github.com/Xinyuan-LilyGO/LilyGoLib/blob/master/docs/hardware/lilygo-t-watch-ultra.md)
and the arduino-esp32 board file
[`variants/lilygo_twatch_ultra/pins_arduino.h`](https://github.com/espressif/arduino-esp32/blob/master/variants/lilygo_twatch_ultra/pins_arduino.h).
The schematic is in LilyGoLib (`schematic/T-Watch Ultra V1.0 SCH 25-07-24.pdf`).

| Item | Fact | Implication |
|---|---|---|
| SoC | ESP32-S3, dual-core up to 240 MHz, Wi-Fi + BLE 5 | Run at 80 MHz or less with Wi-Fi off, as on the XIAO |
| Memory | 16 MB QSPI flash, 8 MB **QSPI** PSRAM (LilyGO: external, not in the chip; some reviews say ESP32-S3R8) | PSRAM is the main audio queue (~1 h of speech at 16 kbps). Use the quad-SPI PSRAM setting; confirm with `esptool.py flash_id` on first flash |
| Mic | TDK **T3902** PDM mic: CLK = GPIO17, DATA = GPIO18 | I2S in PDM-RX mode, same code as the XIAO with different pins |
| microSD | SPI: MOSI 34, MISO 33, SCK 35, CS 21. Power rail **ALDO1**. Card-detect on the XL9555 expander | The SPI bus is **shared with LoRa and NFC**. Keep those unpowered and their CS lines safe (§2.2) |
| Power chip | X-Powers **AXP2101** (I2C 0x34, IRQ GPIO7): charger, fuel gauge, power rails, power button | Battery % and voltage without extra parts. Each peripheral has its own rail that we can turn off |
| Battery | **1100 mAh** (4.07 Wh), internal | LilyGO: charge below 500 mA (≤ 0.5 C) so the power chip doesn't overheat |
| RTC | NXP **PCF85063A** (I2C 0x51, IRQ GPIO1), with backup power | Time survives reboots. The phone still corrects drift (§4) |
| Display | 2.06" AMOLED 410×502, CO5300 driver over QSPI (CS 41, SCK 40, D0–D3 = 38/39/42/45, TE 6, RST 37). Power rail **ALDO2**, plus enable on XL9555 | Off by default. A black-background AMOLED status screen uses little power while on |
| Touch | CST9217 (I2C 0x1A) | Not needed. LilyGO measures +2.5 mA in light sleep when touch can wake the watch, so keep it off |
| Buttons | **PWR** (AXP2101, firmware can read it; 6 s hold = hard power-off); **BOOT** (GPIO0, usable as a button after boot); **RST** (not programmable) | Mute and wake-screen use these (§3) |
| Haptics | TI **DRV2605** (I2C 0x5A), enable on XL9555 | Short buzz for mute/unmute and low battery |
| IO expander | XINLUDA **XL9555** (I2C 0x20) | Enables for haptics and display, touch reset, SD card-detect. **Sources disagree on pin numbers** (§2.1) |
| Sensor | Bosch **BHI260AP** (I2C 0x28, IRQ GPIO8). Power rail **ALDO4** | Optional raise-to-wake. Otherwise powered off |
| GPS | u-blox MIA-M10Q (UART 43/44, PPS 13). Power rail **BLDO1**; backup on LDO1 (VRTC, always on) | Off |
| LoRa | SX1262 (or SX1280, depends on variant). CS 36, RST 47, BUSY 48, IRQ 14. Power rail **ALDO3** | Off |
| NFC | ST25R3916 (CS 4, IRQ 5). Power rail **DLDO1** | Off |
| Speaker | MAX98357A amp (I2S 9/10/11). Power rail **BLDO2** | Off. The recorder never plays audio |
| I2C bus | SDA 3, SCL 2, shared by all I2C chips | — |
| Case | IP65 | Fine for daily wear |

LilyGO's measured board currents (all from their reference, firmware unknown):

| Mode | Current |
|---|---|
| Light sleep, wake on PWR + BOOT | 2.1 mA |
| Light sleep, wake on PWR + BOOT + touch | 4.6 mA |
| Deep sleep, timer wake, backup off | 0.85 mA |
| Power off, backup only | 77 µA |

So even with the chip asleep, the board draws about **1–2 mA**. That's small next to
the ~15–25 mA of a chip that can't sleep while the mic runs, but it's in the budget.

## 2. Board layer (Ultra)

### 2.1 Bring-up order

1. **I2C**, then the **AXP2101**:
   - Keep DC1 (the ESP32-S3 supply) on.
   - Explicitly **turn off** BLDO1 (GPS), ALDO3 (LoRa), DLDO1 (NFC), BLDO2 (speaker),
     ALDO2 (display) and ALDO4 (sensor).
   - Turn **on** ALDO1 (SD) only while the card is in use.
   - LilyGoLib's own startup turns almost every rail on, so we can't copy it as-is.
   - Set the charge current to ≤ 500 mA.
   - Turn off the power chip's charge LED. LilyGoLib sets it to Hi-Z.
   - Enable only the fuel-gauge ADC channels (battery voltage, %, charging).
2. **XL9555**: set the haptic and display enables low; read SD card-detect.
   - **Open point:** LilyGO's hardware page lists touch reset = expander pin 10 and SD
     card-detect = 12. The arduino-esp32 board file says touch reset = 8 and SD
     card-detect = 10. Check the schematic, then test on the watch.
3. **PCF85063A**: read the time. If it's valid (set at least once by the phone), chunks
   get a wall-clock timestamp right away.
4. **Mic**: I2S PDM RX on GPIO17/18. This is shared code; only the pin numbers come from
   the board file.

### 2.2 Shared SPI bus

SD, LoRa and NFC share MOSI/MISO/SCK. With the LoRa and NFC rails off:

- Their CS lines (GPIO36, GPIO4) must not be driven high into an unpowered chip. That can
  back-power it through its input-protection diodes and leak current.
- Leave both CS pins as inputs (Hi-Z), or follow the schematic's pull-ups. Then compare
  SD-write current with and without this fix in milestone 2.

### 2.3 Storage

- PSRAM queue first, then **microSD** as overflow, as in XIAO §2.
- The SD rail (ALDO1) is turned on only for a spill or a read-back, then turned off. That
  saves the card's idle current, which the XIAO can't do because its SD power is fixed.
- **Decided: microSD** (§10). Internal flash (~80 min of speech at 16 kbps in ~10 MB)
  stays only a fallback if the card turns out to be a problem.

## 3. Watch UI

The watch is a recorder first. The UI is small and off most of the time.

Layout chosen by the owner on 2026-10-19:

| Input | Action |
|---|---|
| BOOT press | Screen on for a few seconds: **recording / speech / muted**, battery %, queue (minutes waiting), last phone sync, time. It waits ~0.3 s first to rule out a double-click |
| BOOT double-click | **Toggle mute** (mic and encoder off). 2 buzzes = muted, 1 long buzz = unmuted |
| BOOT hold ~1 s | Open the 2-minute BLE sync/pairing window (a stand-in until a touch UI exists) |
| PWR short press | Start / stop a conversation recording session. 1 buzz = started, 2 buzzes = stopped |
| PWR hold ~1 s | Start a **voice note**. A PWR press stops it; it stops by itself after 5 min. The AXP2101 long-press time is set to 1 s (register 0x27 bits[5:4]) |
| PWR hold ~6 s | Hardware power-off (AXP2101 power-off time, left as shipped) |
| Raise to wake | **Not for now** (§10): the screen stays off until a button press. The BHI260AP gesture could add it later, after power is measured |

- Every action lights the screen for a few seconds with what happened: "Recording",
  "Stopped", "Note 0:12", "Note saved", "Muted", "Unmuted", "Sync open".
- The old BOOT+PWR combo is gone. Sync stays automatic (protocol §7.1). Touch stays out of
  scope (held in reset).

**Voice notes.** A voice note is a short recording the owner starts on purpose, for
example a reminder or an idea. It is its own session: a new session ID, 30 s chunks as
usual, each chunk flagged `NOTE` (protocol §4.3). It stops on a PWR press or after 5 min
(10 chunks). If a conversation session is recording, the watch ends it cleanly first,
records the note, and then starts a new conversation session (unless muted meanwhile). The
mic has one owner, so notes and conversations never mix. The server's gap rule joins the
two conversation halves; notes are never joined. Mute ends whatever is recording and
cancels the resume.

- The **mute button is a requirement** (consent), as on the XIAO.
- A short buzz also signals **low battery** and **phone unreachable for a long time**
  (queue > 70%).
- Screen drawing: start with a minimal driver that redraws a few text lines and icons
  on a black background. Use LVGL only if the UI grows. The CO5300 QSPI driver:
  - Check the ESP-IDF component registry for one first.
  - LilyGoLib (MIT license) has a working driver to borrow from, with its copyright
    notice kept.

## 4. Time and sessions

The same as XIAO §4, with one simplification: once the RTC has been set by the phone, it
keeps time through reboots and power-off. So chunks carry a wall-clock time even before
the phone reconnects. The phone still sends `time_sync` on connect and about every hour.
The firmware writes the corrected time back to the RTC.

## 5. Phone app and server

- **Phone app:** the same as XIAO §3.3. It uploads with `device=twatch-ultra-<mac4>` and
  `codec=opus`. It also shows the watch's battery % (from the fuel gauge) in the status
  view.
- **Server:** the same changes as XIAO §5:
  - finalise sessions on an explicit end, not on idle
  - handle gapped sessions
  - let the device skip silence stop
  - optional device status

  The server work is done once and serves both boards. Nothing extra is needed for the
  Ultra.
- **MCP tools: unchanged.** No contract change (AGENTS.md "MCP tool contract").

## 6. Firmware project layout (proposal)

This replaces the XIAO plan's `firmware/xiao-esp32s3/` with one project and a board
layer:

```
firmware/recorder/                 # ESP-IDF 5.x
├── sdkconfig.defaults             # PM + tickless idle, NimBLE, Wi-Fi not linked
├── sdkconfig.twatch_ultra         # quad-SPI PSRAM, 16 MB flash, partitions
├── sdkconfig.xiao_s3_sense        # later
├── main/                          # shared: everything in XIAO §6 except board code
│   ├── app_main.c, mic.c, speech_gate.c, encoder.c, queue.c
│   ├── transport.h, transport_ble.c
│   └── board.h                    # interface: power rails, battery, time, buttons, ui, storage
├── boards/
│   ├── twatch_ultra/              # AXP2101, XL9555, PCF85063A, CO5300 screen, DRV2605, pins
│   └── xiao_s3_sense/             # later: ADC divider, GPIO button, LED, pins
└── test/                          # host tests: gate, Ogg writer, queue/ack logic
```

- **ESP-IDF**, for the same reasons as XIAO §6: power management, NimBLE tuning, and
  Wi-Fi left out of the image.
- LilyGoLib itself is an Arduino library and needs arduino-esp32 ≥ 3.3. We use it as a
  reference, not a dependency.
- For the AXP2101 there's XPowersLib (also LilyGO, MIT), which builds under ESP-IDF.
- `.gitignore` already ignores `secrets.h` and `firmware-archive/`. Add `build/` and
  `sdkconfig` (keep `sdkconfig.defaults*`).
- CI: add an `idf.py build` job for each board once the project exists (AGENTS.md
  "Checks").

## 7. Power budget

### 7.1 Estimates (to replace with measurements)

| Consumer | Estimate | Lever |
|---|---|---|
| CPU + I2S DMA, 80 MHz, mic always on | ~15–25 mA | As on the XIAO (XIAO §7.2: light sleep probably blocked while I2S runs) |
| Opus encoding (speech only) | +5–15 mA while active | Low complexity, 16 kbps |
| Board floor (power chip, RTC, expander, regulators, leakage) | ~1–2 mA | LilyGO's sleep numbers. Turn off unused rails |
| BLE idle connection + bursts | ~0.5–3 mA avg | Long interval + latency, batching (XIAO §3.1) |
| AMOLED status screen | tens of mA, **only while on** | ~5 s per press; black background, few lit pixels |
| microSD | ~0 when the rail is off | Power it only to spill or read |
| GPS / LoRa / NFC / speaker / sensor | ~0 with rails off | Check that the rails really are off (power chip register read-back) |

### 7.2 Battery-life estimate (unverified)

At an average of 20–35 mA on 1100 mAh: **~30–50 h**. That means a daily charge with
margin, or about two days at best. The owner accepts a daily charge but wants the
longest battery life possible without hurting recording quality (§7.4). If milestone 2 measures much worse, the fallbacks in
XIAO §7.4 apply, though the wake-on-sound mic mod is harder inside a sealed watch.

### 7.3 Measuring

- Check first whether the AXP2101 reports battery discharge current (LilyGoLib turns on
  a "battery current" ADC channel).
  - If it does, the firmware can log its own draw to SD, which makes milestone 2 easy.
  - If not, measure total current with a USB meter while the battery is unplugged, or
    with a PPK2 on the battery leads. That means opening the case.
- Either way, also log battery % over a full discharge as the end-to-end number.

### 7.4 Rule: battery life, but never at the cost of recording quality

The owner's priority (2026-10-19): **as much battery life as possible, as long as voice
recording stays good and complete.** So:

- **Quality decides first, power second.** Bitrate, gain and the speech gate are chosen
  by transcription accuracy and missed speech on the server. Among the settings that are
  good enough, pick the one that uses the least power.
- **The speech gate stays permissive.** Missing the start of a sentence is worse than
  encoding some extra noise. Pre-roll stays at 2–5 s. Silero VAD on the server remains
  the real judge.
- **No duty-cycled listening** (XIAO §7.4 option 2). It loses speech.
- **Everything that isn't recording gets cut hard:** unused rails off, Wi-Fi out of the
  image, screen off, SD powered only while in use, BLE batched with long idle intervals,
  CPU at the lowest clock that keeps up with the mic and encoder.
- Every power optimisation is checked against the milestone 3 recordings. If accuracy
  or missed-speech numbers get worse, the optimisation is rolled back.

## 8. Milestones and acceptance tests

Milestones 4–7 are shared with the XIAO plan (XIAO §8) and are done once.

| # | Milestone | Done when |
|---|---|---|
| 0 | Docs | AGENTS.md, MISSION.md and PROGRESS.md list the Ultra: BLE-only, Wi-Fi off, board facts, checks (`idf.py build`, host tests) |
| 1 | **Bring-up test firmware** (the "test app" for this watch) | The watch boots with every unused rail off. It shows battery %, RTC time and SD card status on a button press. The mute button and buzz work. It records 60 s of mic audio to SD as WAV. That file goes through the server pipeline (`smoke_upload.py`-style, fictional speech only) and transcribes. The expander-pin question (§2.1) is answered |
| 2 | **Power baseline** | Current measured (§7.3) for: board idle, mic + gate, speech + encoding, BLE idle, BLE burst, SD write, screen on. Light-sleep question answered. Full-discharge run with the gate running. **Go/no-go for 24/7** |
| 3 | Mic quality on the wrist | Opus at 12/16/24 kbps, recorded at the wrist, with a sleeve, and in a pocket, run through the server. Pick a bitrate and gain |
| 4 | Gate + queue | XIAO milestone 3 on this board: an 8 h bench run, pre-roll works, PSRAM → SD spill with the SD rail switching |
| 5 | BLE transfer | XIAO milestone 4: a `bleak` test script acting as the phone |
| 6 | Android app link | XIAO milestone 5 |
| 7 | Server late-chunk handling | XIAO milestone 6 |
| 8 | Wear test | 24 h on the wrist: battery life, conversations captured vs missed, false triggers, mute used |

**The T-Watch S3** is left out for now (§10). The board layer keeps the door open: it
would be one more board file later.

## 9. Risks

- **Battery life** (§7). The main risk; milestone 2 decides it.
- **Mic on the wrist:** a sleeve or jacket muffles speech. The wrist moves, so there's
  rubbing and clothing noise. The owner's voice is close, others are farther away.
  Milestone 3 tests real wearing positions. Unlike the XIAO, there's no easy external-mic
  upgrade.
- **Shared SPI bus** leaking current into unpowered LoRa/NFC chips (§2.2).
- **Doc conflicts** on expander pins (§2.1). The schematic decides.
- **Hardware power-off:** a 6 s PWR hold turns the watch off and recording stops. Show
  "recording stopped" on the phone when the watch disappears, so it's noticed.
- **Android background limits, data loss on power cut, consent**: same as XIAO §9.
- **Charging heat:** keep charge current ≤ 500 mA as LilyGO says.

## 10. Owner decisions (2026-10-19)

1. **Device order:** the T-Watch Ultra first, the XIAO pendant later on the same
   firmware.
2. **One shared firmware** (`firmware/recorder/` plus a board layer, §6), for now. It can
   be split later if that turns out better.
3. **Framework: ESP-IDF.**
4. **Buttons:** the owner changed the layout on 2026-10-19; see §3. BOOT press = status
   screen, BOOT double-click = mute, BOOT hold = sync window, PWR short press = start/stop
   a session, PWR hold ~1 s = voice note.
5. **Screen:** off until a button press. No raise-to-wake for now.
6. **Overflow storage: microSD.**
7. **T-Watch S3:** left out for now.
8. **Carried over from the XIAO plan:**
   - Server late-chunk policy: finalise on `/end` plus a 24 h safety timeout, re-open on
     late chunks.
   - Omi live-audio service: skip.

   Both apply to both boards.
9. **Battery:** a daily charge is acceptable, but battery life should be as long as
   possible while recording stays good (rule in §7.4).

## 11. Upload path as built (2026-10-19)

The protocol is [twatch-ble-protocol.md](twatch-ble-protocol.md). Decisions made while
building it, some of which change the XIAO plan's §3:

- **The phone acks a chunk only after the server accepted it** (not after storing it, as
  XIAO §3.2 proposed). The phone keeps no audio of its own; a chunk stays on the watch until
  the server has it, and a phone that can't reach the server doesn't pull chunks.
- **Drain, then disconnect.** No idle long-interval connection (XIAO §3.1): the watch
  advertises only when something is waiting (finished session, 10 min queued, or hourly),
  the relay drains it in a 15–30 ms burst and disconnects.
- **Sessions are button-driven for now** (PWR short press starts/stops, PWR hold ~1 s
  records a voice note, mute stops; §3). The speech
  gate (§8 milestone 4) will start and end sessions on its own later; the protocol doesn't
  change.
- **Chunks go to the SD card directly**, no PSRAM queue yet; the card stays mounted while a
  session records. Milestone 2 measures what that costs; the PSRAM-first queue of §2.3 is
  still the plan if it matters.
- **Opus**: libopus via `78/esp-opus`, 16 kbps, complexity 1, `codec=ogg` uploads.
- **`device=twatch-ultra`**, without the `-<mac4>` suffix of §5, while there is one watch.
- **Pairing**: passkey entry with LE Secure Connections (owner decision 2026-10-19): the
  watch shows a 6-digit code on the AMOLED, the phone or PC types it. New bonds only in a
  2-minute sync window (BOOT hold ~1 s).
- **Server**: no protocol change was needed. One fix: chunks that arrive while their
  session is being processed are no longer lost (they come in bursts now).

## Not verified yet

- Every current figure in §7, and whether I2S PDM RX blocks light sleep (XIAO §7.2).
- XL9555 pin numbers for touch reset and SD card-detect (sources disagree).
- Whether the chip is a plain ESP32-S3 with external QSPI flash and PSRAM (LilyGO's
  hardware page) or an ESP32-S3R8 (some reviews). This only affects sdkconfig.
- Whether the AXP2101 on this board reports battery discharge current.
- Leakage on the shared SPI bus with the LoRa/NFC rails off.
- Whether a CO5300 driver exists in the ESP-IDF component registry.
- T3902 mic quality at the wrist, and whisper accuracy at 12–16 kbps Opus from it.
- Everything in §11 on hardware: Opus CPU load at 80 MHz, BLE throughput, pairing with
  Android and BlueZ, Android background limits, power-cut recovery on a real card, and the
  battery cost of advertising, bursts and the card staying mounted while recording.
