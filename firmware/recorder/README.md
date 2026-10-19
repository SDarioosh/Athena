# odysseus-sound recorder firmware

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
  | PWR hold ~1 s (or `note start` / `note stop`) | Starts a **voice note**. The AXP2101 long-press time is set to 1 s (register 0x27 bits[5:4]); the power-off hold (~6 s) is left as shipped |
  | PWR hold ~6 s | Hardware power-off (AXP2101) |

- **Voice notes**: a note is its own short session. Every chunk carries the `NOTE` flag
  (protocol §4.3), which is kept in the chunk's `.met` record, so it survives reboots and
  reaches the relay. A note stops on a PWR press, `note stop`, mute, or by itself after 5
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

## Upload path (BLE)

```
mic -> Opus (libopus, 16 kHz mono, 20 ms, 16 kbps VBR, complexity 1) -> 30 s Ogg/Opus chunks
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

Exit the monitor with `Ctrl-]`. Log output and the `recorder>` console share the port.

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
└── tools/ble_relay.py        # PC relay: drain the watch over BLE, upload (barvis_relay/, tests/)
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

The button layout of 2026-10-19 and voice notes are not tested on the watch yet: the
double-click timing (~0.3 s), the PWR 1 s long press via PMU register 0x27, that a hold of
~6 s still powers the watch off, and a voice note end to end (NOTE flag on every chunk,
5-min cap, the conversation ending and resuming around it, `kind=note` on the server).
