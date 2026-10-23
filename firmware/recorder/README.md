# Athena recorder firmware

ESP-IDF firmware for the wearable recorders. One firmware, one board layer:
`main/` is shared, `boards/<name>/board/` implements `main/board.h` for one board.
Plan: [`docs/t-watch-ultra-plan.md`](../../docs/t-watch-ultra-plan.md) (§6 layout, §8
milestones) and [`docs/xiao-esp32s3-sense-plan.md`](../../docs/xiao-esp32s3-sense-plan.md).

**Status: milestone 1 verified on the owner's watch; the BLE upload path (plan milestones
5–6) is built and host-tested but not yet run on hardware.** Recording sessions are encoded
to Opus and queued on the SD card; a phone ([`android-relay/`](../../android-relay)) or a PC
([`tools/ble_relay.py`](tools/ble_relay.py)) drains them over BLE and uploads them to the
server. The protocol: [`docs/twatch-ble-protocol.md`](../../docs/twatch-ble-protocol.md).
Wi-Fi is never initialised and not linked. No speech gate yet (milestone 4): a session
records everything between start and stop.

## What it does (T-Watch Ultra)

- Boots with every unused power rail off (GPS, LoRa, NFC, speaker, sensor, display), reads
  the rails back from the AXP2101 and logs them. Charge current 400 mA, 4.2 V target,
  charge LED off. The SD and display rails are on only while in use.
- Logs every XL9555 expander input, to settle the touch-reset / SD-detect pin question
  (`boards/twatch_ultra/board/pins.h` has both readings and the schematic's answer).
- Reads the PCF85063A RTC into the system clock at boot; `time <ISO-8601>` on the USB
  console sets both.
- **Buttons** (owner's layout of 2026-10-19, plan §3):

  | Action | Does |
  |---|---|
  | BOOT press | Status screen for a few seconds (recording/muted, battery %, voltage, time, SD), then the display rail goes off again. Waits ~0.3 s first to rule out a double-click |
  | BOOT double-click (or `mute`) | Toggle mute. 2 buzzes = muted, 1 long buzz = unmuted. Mute ends whatever is recording |
  | BOOT hold ~1 s (or `sync` / `ble pair`) | Opens the **BLE sync window** for 2 minutes: the watch advertises and a new phone or PC may pair. Outside the window only already-paired relays get in |
  | PWR short press (or `session start` / `session stop`) | Starts or stops a **recording session** (1 buzz start, 2 buzzes stop; 3 buzzes if it ended on its own: card full or an error). The mic is encoded to Opus and written as 30 s Ogg/Opus chunks to `/sdcard/q/` (the upload queue). During a voice note, it stops the note |
  | PWR hold ~1 s (or `note start` / `note stop`) | Starts a **voice note**, or ends the one recording (as a PWR press does). The AXP2101 long-press time is set to 1 s (register 0x27 bits[5:4]); the power-off hold (~6 s) is left as shipped |
  | PWR hold ~6 s | Hardware power-off (AXP2101) |

- **Voice notes**: a note is its own short session. Every chunk carries the `NOTE` flag
  (protocol §4.3), which is kept in the chunk's `.met` record, so it survives reboots and
  reaches the relay. A note stops on a PWR press or a PWR hold, `note stop`, mute, or by itself after 5
  min (10 chunks). If a conversation session is recording, it ends cleanly first (its last
  chunk gets `LAST`), the note records, and then a new conversation session starts by
  itself (not if muted meanwhile).
- **Screen messages**: every button action lights the screen for a few seconds with what
  happened: "Recording", "Stopped", "Note 0:12", "Note saved", "Muted", "Unmuted",
  "Sync open".
- **Uploads**: the watch advertises only when something is waiting (a finished session, or
  10 min of audio from a running one, or anything queued after an hour without a sync).
  The relay connects, sets the watch's clock, pulls each chunk, uploads it to the server,
  and only then tells the watch to delete it (protocol §1, §6).
- **Power**: the chip light-sleeps whenever it has nothing to do; BLE advertising and
  syncing keep working, and it stays awake while a USB host is attached (console,
  flashing). Every button works on its first press. It never goes into standby by itself
  (light sleep only, by choice: a press always acts at once). `standby` on the console puts
  it into **standby**: deep sleep until **PWR** is pressed or USB power is plugged in.
  Waking is a reboot of about a second that shows the status screen (no buzz; mute is
  kept). The waking press does nothing else: it doesn't start a recording. The power log is
  written to the card before standby (its RAM ring doesn't survive deep sleep).
  BOOT doesn't wake it: GPIO0 is a strapping pin.
- **Off**: hold PWR ~6 s; the AXP2101 cuts the power (a PWR press turns it back on, a
  full boot). The hold passes the 1 s voice-note mark first, so a few seconds of note are
  recorded, kept on the card and uploaded with the next sync.
- **`rec [s]`** (console only): the milestone-1 test, N seconds (default 60) from the mic to
  `/sdcard/rec_<UTC time>.wav`. The log line `recorder: level at ...` gives the file's RMS,
  peak, DC offset, clipped samples and how often the limiter acted. `stop` or mute ends it
  early.
- **Mic conditioning** (`main/audio_dsp.c`, applied inside `mic_read()`): 100 Hz high-pass,
  the board's EQ, gain (default +30 dB), and a limiter at -1 dBFS. The ESP32-S3's PDM
  converter adds no gain and no high-pass, so the raw mic level (speech at ~30 cm around
  -55 dBFS, normal for a MEMS mic) was nearly inaudible, under a +600 DC offset and rumble.
  The T-Watch's mic port leaves a hole at 1-2.5 kHz and a peak at 3-4 kHz (muffled speech);
  `board_mic_eq()` corrects it with +9 dB at 1.6 kHz and -6 dB at 3.6 kHz. With all of it,
  20 s of speech at ~30 cm records at about -24 dBFS, nothing clipped. `gain [dB]` and
  `eq on|off` on the console change it (saved in NVS). The right value is milestone 3's
  call, by transcription accuracy.
- The mic drops its first 250 ms after each start: the PDM filter starts with a
  near-full-scale click and a settling DC offset.
- **`level [s]`** on the console: reads the mic for N seconds (default 5) without the SD
  card and prints the conditioned level once a second. A quiet room reads about -38 dBFS
  at +30 dB; talk during it and the level should rise well above that.
- **Copy recordings off over USB** (no card reader): `ls` lists the card, `get <file>` sends
  one file as base64 with a CRC-32, and `tools/pull.py` does both and saves the files
  (~16 KB/s, so a 60 s recording takes about 2 minutes). Keep the output folder outside
  the repository. A card reader is faster for many files.
  ```bash
  python firmware/recorder/tools/pull.py --port COM35 --out ~/barvis-recordings
  ```
- USB console (`help`): `status`, `batt`, `rails`, `exp`, `time`, `session [start|stop]`,
  `note [start|stop]`, `queue [drop <id>]`, `ble [pair|forget]`, `sync`, `rec [s]`, `stop`, `level [s]`,
  `gain [dB]`, `eq [on|off]`, `ls [q]`, `get <file>`, `mute`, `screen`, `sd`, `reboot`.
  `status` also prints the free heap and the last reset reason.
  `sd failwrite <n>` makes the next n chunk writes fail (to test the recovery below).
  `batt sim`, `coredump [get|erase]`, `crash yes|wdt` and `events [clear]`: see below.

## What it does (T-Watch S3)

The same firmware on the LilyGO T-Watch S3 (`-DBOARD=twatch_s3`, board layer
`boards/twatch_s3/board/`). Everything above works the same, with these differences:

- **One button**, the side key (the AXP2101's power key). Clicks are counted, and each count
  does what a T-Watch Ultra button does:

  | Action | Does |
  |---|---|
  | 1 click | Start or stop a recording session (the Ultra's PWR press) |
  | 2 clicks | Mute / unmute (the Ultra's BOOT double-click) |
  | 3 clicks | Status screen (the Ultra's BOOT press) |
  | 4 clicks | BLE sync window for 2 minutes, to pair a phone (the Ultra's BOOT hold) |
  | Hold ~1 s | Start a voice note, or end the recording (the Ultra's PWR hold) |
  | Hold ~6 s | Hardware power-off |

  Clicks count when they are less than 0.5 s apart; the action runs 0.5 s after the last
  click. For 1 s after an action, clicks are ignored (the confirming buzz can shake the key);
  wait for the buzz before the next action. A hold always counts. Five or more clicks do
  nothing. Logic: `main/click_counter.c` (host-tested).
- **No SD card**: the queue and recordings live in a FAT partition in the 16 MB flash
  (`partitions_twatch_s3.csv`, ~11.8 MB, about an hour of Opus audio), mounted at `/flash`.
  Recording stops when less than 1 MB is free. `ls`, `get`, `tools/pull.py` work the same.
- **Screen**: 240x240 ST7789 LCD with a compact status layout; the backlight is on or off,
  not dimmed.
- **Power**: ALDO1 (RTC) stays on; screen (ALDO2 backlight, ALDO3 logic), haptics enable
  (BLDO2), LoRa, GPS and speaker are off unless used. Charging 200 mA to 4.2 V with the
  battery-temperature input disabled (the watch has no thermistor; LilyGO does the same).
- **Mic**: Knowles SPM1423 PDM on GPIO44/47, no EQ yet (not measured on this watch).

Build and flash (from `firmware/recorder`):

```bash
idf.py -B build-s3 -DBOARD=twatch_s3 -DSDKCONFIG=build-s3/sdkconfig -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.twatch_s3" build
idf.py -B build-s3 -p COM40 erase-flash flash
```

The first flash over the factory firmware: erase it (the old file system and settings
would otherwise sit in the new partitions). If `esptool` can't open the port, power the
watch off (hold the side key ~6 s), then press it again and retry; or open the back cover
and use BOOT + RST (download mode) on versions that have them.

## What it does (T4-S3 conference recorder)

The same firmware on the LilyGO T4-S3 (2.41" AMOLED, `-DBOARD=t4_s3`, board layer
`boards/t4_s3/board/`), as a table-top recorder with four INMP441 I2S mics. Sessions, voice
notes, the flash queue, BLE sync and the console work as on the T-Watch S3. Differences:

- **One button, BOOT** (RST only resets the chip). The slide switch is the power switch.
  BOOT presses go through the same click counter as the T-Watch S3's side key
  (`main/key_press.c` turns the GPIO level into presses, both host-tested):

  | Action | Does |
  |---|---|
  | 1 press | Start or stop a recording session |
  | Hold ~1 s | Start a voice note; a press or another hold ends it |
  | 2 presses | Mute / unmute |
  | 3 presses | Status screen |
  | 4 presses | BLE sync window for 2 minutes, to pair a phone |
  | Tap the screen | Status screen (one per touch, however long the finger rests) |

  No haptics: every action shows on the screen instead. The tap comes from the CST226SE
  touch chip's interrupt line (GPIO8); it also wakes the chip from light sleep.
- **Mics: four INMP441** on the 30-pin header, two stereo pairs sharing one clock:

  | INMP441 | L/R | SD (data) | SCK | WS |
  |---|---|---|---|---|
  | A-L | GND | GPIO41 | GPIO39 | GPIO40 |
  | A-R | 3V3 | GPIO41 | GPIO39 | GPIO40 |
  | B-L | GND | GPIO42 | GPIO39 | GPIO40 |
  | B-R | 3V3 | GPIO42 | GPIO39 | GPIO40 |

  Every mic: VDD to 3V3, GND to GND. I2S0 drives the clock and reads pair A; I2S1 reads
  pair B as a slave on the same clock, so all four are sampled on the same edges (a mic
  array; checked on the board: pair B stays in step). The firmware finds the mics that
  answer each time the mic starts and mixes them to mono (average, `main/mic_mix.c`); a
  missing mic is left out, so one to four mics all work. Console: `mics [s]` shows each
  mic's level, `mics 0x<mask>` picks the mics mixed (bit 0 A-L ... bit 3 B-R), `mics auto`
  goes back to every mic that answers. `level`, `gain` and `eq` work on the mix.
- **No audio lost to flash writes**: the mic's reader task empties the I2S buffers into an
  8 s ring in PSRAM, and the I2S interrupt keeps running while flash is written
  (`CONFIG_I2S_ISR_IRAM_SAFE`). Measured: 74.9 s recorded in 74.9 s; the recorder fell up to
  2.3 s behind while closing a chunk, the ring absorbed it. `status` shows lost buffers.
- **Screen on while recording** (`board_screen_on_while_recording()`; the watches keep
  their few-seconds screen): a red REC (or NOTE), the running time, the mics mixed ("NO
  MICS" in amber when none answer), USB/battery and "PRESS BOOT TO STOP", for the whole
  recording. A tap or a status screen shows for a few seconds, then it comes back. At 40 %
  brightness, its lines move a few pixels every minute (AMOLED burn-in). Meant for a desk
  on USB; on battery the screen costs runtime.
- **Screen**: 450x600 RM690B0 AMOLED (QSPI, driven by `esp_lcd_co5300` with LilyGO's init
  list). Lines too wide for a panel now shrink to fit (all boards). The UI draws in 16-row strips (a 14 KB buffer instead of 43 KB).
- **Charger: SY6970** (I2C 0x6A): 512 mA charge current, watchdog off, ADC one-shot per read.
  No fuel gauge: the percentage is estimated from the voltage. With the slide switch off the
  battery is cut off and reads as absent.
- **No RTC chip**: the time is the ESP32-S3's own clock (kept through sleep and resets, not
  through the power switch). The phone sets it at every sync, or `time` on the console.
- **Storage**: the flash partition, as on the T-Watch S3. The microSD slot isn't used yet.

Build and flash (from `firmware/recorder`):

```bash
idf.py -B build-t4 -DBOARD=t4_s3 -DSDKCONFIG=build-t4/sdkconfig -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.t4_s3" build
idf.py -B build-t4 -p COM40 flash
```

## Upload path (BLE)

```
mic -> Opus (libopus, 16 kHz mono, 20 ms, 24 kbps VBR, complexity 1) -> 30 s Ogg/Opus chunks
    -> /sdcard/q (survives reboots) -> BLE (NimBLE GATT) -> relay -> POST /v1/ingest/chunk
```

- **Opus**: [`78/esp-opus`](https://components.espressif.com/components/78/esp-opus)
  (upstream libopus, BSD-3-Clause, fixed point with Xtensa multiply macros, MIT packaging).
  Complexity 1: better than 0 for ~10 % more CPU; 2 and up cost ~30 % more. Estimated
  load from instruction counts on a PC: **~0.67 M instructions per 20 ms frame, roughly
  40–60 % of one core at 80 MHz** (so DFS will rarely drop to 40 MHz while recording).
  The session logs the real figure: `encoder N us avg / M us max per 20 ms frame` (20000
  us = a full core) at every chunk and at the end. If it's too high: complexity 0, cache
  settings, or Espressif's `esp_audio_codec` (only `opus_enc.c` changes).
- **Chunks**: each a standalone Ogg/Opus stream that decodes to exactly its length;
  start times are contiguous; pages are written and fsynced every second, so a power cut
  loses at most ~1 s, and the chunk being written is recovered at the next boot. Details:
  protocol §2.
- **Low battery**: below the board's limit (T-Watch: 5 % or 3.5 V, two readings 15 s
  apart) the recording ends cleanly, the watch buzzes 3 times and shows BATTERY LOW, and
  refuses new recordings until 10 % or USB. `batt sim <percent> [mV]` / `batt sim off`
  fakes the battery for testing.
- **Crashes and hangs**: `status` and BLE `info` show the last reset reason (`reset=`). A
  crash saves a core dump to flash: `coredump` shows where, `tools/pull.py --coredump`
  copies it, `idf.py coredump-info -c <file>` decodes it, `coredump erase` clears it. The
  session and BLE tasks are on the task watchdog: a 10 s hang restarts the watch, and a
  conversation that was recording starts again after the boot. `crash yes` / `crash wdt`
  test it.
- **Event log**: `events` prints what started and ended recordings, kept in flash (NVS,
  the last 128): boots with the reset reason, every button event (a PWR press with the PMU's
  interrupt bits: "no key edges" means the PMU reported a press the key never made; presses
  the 1 s guard ignored), recording starts and stop requests with what asked for them,
  recording ends with the reason, mute, low battery and card power cycles, each with the UTC
  time, uptime and battery. `events clear` empties it. When a recording stopped or started
  by itself, read `events` before anything else.
- **Power log**: `power` shows the battery, what the watch is doing, the CPU clock and the
  drain rate; every 5 min (`power log <min>|off`) a sample goes to `/sdcard/power.csv`
  (written hourly, never while recording or syncing). `power locks` shows what keeps the
  CPU awake. Light sleep isn't used: while recording the I2S driver holds the APB clock,
  and BLE on the main crystal blocks it at all times (PROGRESS.md round 17).
- **Card errors**: a failed write (the card sometimes answers `0x108`) costs at most the
  open page (~1 s): the chunk is cut back to its last page and the session goes on with a
  new chunk after 100/300/1000 ms of skipped (but counted) mic frames; it ends only after 4
  failures in a row. After each failure the card is powered off and on (~0.3 s, the mic ring
  keeps the audio) before the chunk is read back: a card that failed once could time out on
  every later command until then (2026-10-23). `status` and the end-of-session log count them.
- **Queue**: `queue` lists it (id, session, sequence, start, length, size, flags);
  `queue drop <id>` deletes one without uploading. `ls q` / `get q/<file>` and
  `tools/pull.py --queue` copy chunks over USB for debugging without BLE (they stay
  queued). Recording stops when the card has less than 8 MB free.
- **BLE**: `ble` prints the state (advertising and why, connected relay, MTU, PHY, bonds,
  sync window, last sync, counters). `ble forget` deletes every pairing. Pairing is passkey
  entry with LE Secure Connections: the watch shows a 6-digit code (also logged on the USB
  console) and you type it on the phone or PC, only in the sync window (protocol §3.1). If
  a phone paired outside the window, remove `barvis-tw` in its Bluetooth settings and pair
  again in the window.

### Drain from a PC: `tools/ble_relay.py`

The phone's part, for testing before (or instead of) the Android app. Needs Python 3.10+
and a Bluetooth adapter. The token comes from `CONVO_INGEST_TOKEN` or `--token-file`
(there is no `--token` option, so it never lands in shell history).

```bash
python3 -m venv ~/.venvs/barvis-relay
~/.venvs/barvis-relay/bin/pip install -r firmware/recorder/tools/requirements.txt

# first time: open the sync window on the watch (BOOT hold ~1 s); when the PC pairs, the
# watch shows a 6-digit code.
# bleak can't type it, so pair through the OS and type the code:
#   Linux:   bluetoothctl -> agent on -> default-agent -> scan on -> pair <address>
#   Windows: Settings > Bluetooth & devices > Add device > Bluetooth > barvis-tw
#   macOS:   run the --list below; macOS asks for the code
~/.venvs/barvis-relay/bin/python firmware/recorder/tools/ble_relay.py --list

export BARVIS_SERVER=http://<server>:8000
export CONVO_INGEST_TOKEN=...            # or --token-file ~/.config/barvis/token (chmod 600)
~/.venvs/barvis-relay/bin/python firmware/recorder/tools/ble_relay.py --once   # drain once
~/.venvs/barvis-relay/bin/python firmware/recorder/tools/ble_relay.py          # keep relaying
```

Windows (PowerShell): `py -m venv $HOME\venvs\barvis-relay`, then
`$env:BARVIS_SERVER="http://<server>:8000"; $env:CONVO_INGEST_TOKEN="..."` and
`& $HOME\venvs\barvis-relay\Scripts\python firmware\recorder\tools\ble_relay.py --once`.

Other options: `--list` (show the watch's status and queue, change nothing), `--dry-run
--save DIR` (pull and check chunks into a folder outside the repo, no upload, no ack),
`--address`, `--scan-timeout`, `-v`. Exit codes: 0 ok, 1 some chunks skipped, 2 retry
later (server, link or no adapter), 3 token rejected or bad settings, 4 watch not found.
Tests (no Bluetooth needed): `python -m pytest -q firmware/recorder/tools`.

### Sync the T5 PDA from a PC: `ble_relay.py pda`

The same relay also speaks the T5 E-Paper Pro's "glance" protocol
([docs/t5-pda-plan.md](../../docs/t5-pda-plan.md) §3). It sends the T5's outbox actions to
the server, acks each one with its result, and `PUT`s the feed (briefing, conversations,
open action items) whenever it changed. Same venv, token and server variables as above.
The T5 holds no token.

```bash
export BARVIS_TZ=Europe/Berlin           # the feed's time zone and the T5 clock (default UTC)
~/.venvs/barvis-relay/bin/python firmware/recorder/tools/ble_relay.py pda --list   # look only
~/.venvs/barvis-relay/bin/python firmware/recorder/tools/ble_relay.py pda --once   # sync once
~/.venvs/barvis-relay/bin/python firmware/recorder/tools/ble_relay.py pda          # keep syncing
```

- Without `--once`, the relay stays connected while the T5's screen is awake. It handles
  outbox entries within seconds and refreshes the feed every 5 min.
- Otherwise it reconnects every 15 min, or sooner when the T5 advertises "outbox waiting".
- Calendar: `--odysseus-url http://127.0.0.1:7010` (the Odysseus test instance) with a
  `calendar:read`/`calendar:write` token in `ODYSSEUS_TOKEN` or `--odysseus-token-file`, and
  `--calendar-href <CalDAV calendar id>`. Without them, calendar actions stay queued on the
  T5 and the feed has no events.
- The same Odysseus URL and token fill the T5's Tasks tab with the owner's checklists
  (plan §4.4; the token also needs `todos:read` and `todos:write`), and the feed with the
  hub's Today insight (plan §4.1; the token also needs `insight:read`, else the feed has no
  `insight`).
- `--templates "Call,Meeting,Reminder"`: event titles for the T5's "+" picker.
- Exit codes: 0 ok, 1 some actions still waiting, 2 retry later, 3 token rejected,
  incompatible or bad settings, 4 T5 not found.
- The code is in `tools/pda_relay.py` and `tools/barvis_relay/pda_*.py`. The tests are in
  `tools/tests/test_pda.py` (fake T5, fake server) and `server/tests/test_pda_feed.py` (the
  real API, seeded fictional data).

## Build

Requires ESP-IDF v5.4 or newer (CI uses v5.5.2) with the esp32s3 toolchain installed
(`./install.sh esp32s3` in the ESP-IDF checkout, then `. export.sh`).

```bash
cd firmware/recorder
idf.py -DBOARD=twatch_ultra -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.twatch_ultra" build
```

The first build downloads `espressif/esp_lcd_co5300` (the AMOLED driver) from the
component registry into `managed_components/` (git-ignored; `dependencies.lock` pins it).
`sdkconfig` is generated and git-ignored; edit `sdkconfig.defaults` (shared) or
`sdkconfig.twatch_ultra` (board) instead.

## Flash and monitor

The watch's USB-C port is the ESP32-S3's native USB (Serial/JTAG). Plug it in and:

```bash
idf.py -p /dev/ttyACM0 flash monitor      # Linux; macOS: /dev/cu.usbmodem*
```

If `esptool` can't connect, put the watch into **download mode**: hold **BOOT**, tap
**RST**, release BOOT, then run the flash command again. After flashing, tap RST (or
power-cycle) to leave download mode; the USB serial port re-enumerates.

Confirm the memory layout on the first flash (`esptool.py --port /dev/ttyACM0 flash_id`
should report a 16 MB flash; the PSRAM line in the boot log should say quad mode, 8 MB).

Exit the monitor with `Ctrl-]`.

On Windows a reset through the USB port's RTS line (`idf.py monitor`, a plain serial
script) may land in the ROM downloader (`boot:0x23 DOWNLOAD`). `esptool.py -p COMx
--after watchdog_reset read_mac` boots the firmware; then attach with
`idf.py monitor --no-reset`. Log output and the `recorder>` console share the port.

Without a monitor (scripts, agents): `tools/wcon.py --port <port> status` sends one command
and prints the answer; `--boot` resets the watch and prints the boot log; `--listen 60`
prints the log for a minute and follows the watch through a power-off. It opens the port
without resetting the watch (RTS is lowered before DTR). Only one program can hold the port.

## Host tests

The pure parts (WAV header, calendar/ISO-8601, PCF85063A BCD codec, 5x7 font and text
rendering, CRC-32, the Ogg/Opus writer and page scanner, the chunk queue, the BLE protocol
codec and command handler) build and run on the host without ESP-IDF:

```bash
cmake -S firmware/recorder/test -B build-host && cmake --build build-host
ctest --test-dir build-host --output-on-failure
```

## Layout

```
firmware/recorder/
├── CMakeLists.txt            # -DBOARD=<name> picks boards/<name>/board
├── sdkconfig.defaults        # PM + tickless idle, USB console, no UART0 (GPS pins)
├── sdkconfig.twatch_ultra    # quad-SPI PSRAM, 16 MB flash, 80 MHz
├── partitions.csv
├── main/
│   ├── board.h               # the board interface
│   ├── app_main.c            # events: buttons, console, recorder, screen timer
│   ├── mic.[ch]              # I2S PDM RX + conditioning (shared; pins, EQ from the board)
│   ├── session_rec.[ch]      # recording session: mic -> Opus -> chunks
│   ├── opus_enc.[ch]         # libopus wrapper (78/esp-opus)
│   ├── ogg_opus.[ch]         # Ogg/Opus writer + page scanner (pure, host-tested)
│   ├── chunk_queue.[ch]      # the queue on /sdcard/q (pure, host-tested)
│   ├── chunk_store.[ch]      # thread-safe queue with card access, used by BLE and the recorder
│   ├── ble_proto.[ch]        # BLE protocol codec + command handler (pure, host-tested)
│   ├── ble_sync.[ch]         # NimBLE GATT server, advertising, pairing window
│   ├── crc32.[ch]            # zlib-compatible CRC-32
│   ├── recorder.[ch]         # milestone-1 WAV test recorder (console `rec`)
│   ├── ui.[ch]               # status screen (text bands, no LVGL)
│   ├── console_cmds.[ch]     # esp_console commands over USB Serial/JTAG
│   ├── audio_dsp.[ch]        # high-pass, EQ, gain, limiter (pure, host-tested)
│   ├── wav isotime font5x7 textfb audio_stats .[ch]  # pure, host-tested
├── boards/twatch_ultra/board/
│   ├── pins.h                # pin map, expander map (both candidate readings)
│   ├── board_twatch_ultra.c  # implements board.h
│   ├── axp2101.[ch] xl9555.[ch] pcf85063a.[ch] pcf85063a_codec.[ch] drv2605.[ch]
│   ├── display.[ch]          # CO5300 via espressif/esp_lcd_co5300 (QSPI)
│   ├── sdcard.[ch]           # SD over SPI (bus shared with LoRa/NFC, their CS Hi-Z)
│   └── idf_component.yml
├── test/                     # host tests (plain CMake + ctest)
├── tools/pull.py             # copy recordings (or --queue chunks) off the card over USB
├── tools/wcon.py             # one console command per call, --boot log, --listen (scripts)
├── tools/ble_relay.py        # PC relay: drain the watch over BLE, upload (barvis_relay/, tests/)
└── tools/pda_relay.py        # `ble_relay.py pda`: sync the T5 PDA (barvis_relay/pda_*.py)
```

## Third-party code

- `espressif/esp_lcd_co5300` (Apache-2.0) from the ESP-IDF component registry.
- `78/esp-opus` (libopus, BSD-3-Clause, Xiph.Org; MIT packaging) from the registry.
- NimBLE (Apache-2.0), part of ESP-IDF.
- The CO5300 init sequence in `display.c` and the fuel-gauge battery model in
  `board_twatch_ultra.c` are copied from
  [LilyGoLib](https://github.com/Xinyuan-LilyGO/LilyGoLib) (MIT, Copyright (c) 2024
  ShenZhen XinYuan Electronic Technology Co., Ltd), with the notice kept at each copy.
  Register maps were cross-checked against XPowersLib and SensorLib (MIT) but the drivers
  here are our own.

## Not verified on hardware yet

See `PROGRESS.md`. On the owner's watch (2026-10-19) the rails, battery, RTC, SD card,
status screen, buttons, haptics and recording work, and recorded speech is audible and no
longer muffled after the conditioning above (owner listening test). The right PDM slot,
stereo capture, the inverted clock edge and a 2.048 MHz PDM clock all read the same as the
defaults. SD card detect is confirmed (P12: LOW with a card, HIGH without). Still open: a
recording through the server pipeline, and tuning gain and EQ by transcription accuracy
(milestone 3).

The BLE upload path has not run on hardware at all yet: Opus encode time and CPU load at
80 MHz, BLE throughput, pairing with Android and BlueZ, power-cut recovery on a real card,
`fsync` once a second (SD power and wear), the advertising and connection current, and
the runtime heap after BLE starts. The PR's Server verification section has the steps.

Verified on the owner's watch on 2026-10-20 (PROGRESS.md round 17): the button layout
(double-click timing, PWR 1 s hold, the ~6 s power-off hold, also on USB), voice notes (NOTE
on every chunk, the 5-min cap, a conversation ending and resuming around a note, mute
mid-note), recovery of a chunk cut off by a restart, recovery from SD write errors, the
encoder time (10.4-11.9 ms avg per 20 ms frame) and the heap after BLE starts (197 KB
internal free). Still open: a note through the server (`kind=note`), a real battery pull
during recording, and the cause of the SD `0x108` errors and of extra PWR short presses
(ignored within 1 s of the previous one).
