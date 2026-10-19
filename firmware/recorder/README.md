# barvis-sound recorder firmware

ESP-IDF firmware for the wearable recorders. One firmware, one board layer:
`main/` is shared, `boards/<name>/board/` implements `main/board.h` for one board.
Plan: [`docs/t-watch-ultra-plan.md`](../../docs/t-watch-ultra-plan.md) (§6 layout, §8
milestones) and [`docs/xiao-esp32s3-sense-plan.md`](../../docs/xiao-esp32s3-sense-plan.md).

**Status: milestone 1, bring-up test firmware for the LilyGO T-Watch Ultra.** No BLE, no
Wi-Fi (not initialised and not linked), no Opus, no speech gate yet.

## What it does (T-Watch Ultra)

- Boots with every unused power rail off (GPS, LoRa, NFC, speaker, sensor, display), reads
  the rails back from the AXP2101 and logs them. Charge current 400 mA, 4.2 V target,
  charge LED off. The SD and display rails are on only while in use.
- Logs every XL9555 expander input, to settle the touch-reset / SD-detect pin question
  (`boards/twatch_ultra/board/pins.h` has both readings and the schematic's answer).
- Reads the PCF85063A RTC into the system clock at boot; `time <ISO-8601>` on the USB
  console sets both.
- **PWR short press**: status screen for ~5 s (recording/muted, battery %, voltage, time,
  SD), then the display rail goes off again.
- **BOOT short press**: toggle mute. 1 buzz = recording, 2 buzzes = muted.
- **BOOT long press (1 s)**: logs "sync now" (placeholder for BLE).
- **BOOT held + PWR short press**, or `rec` on the console: records 60 s from the PDM mic
  (16 kHz, mono, 16-bit) to `/sdcard/rec_<UTC time>.wav`, then powers the card off. The
  log line `recorder: level: ...` gives the file's RMS, peak, DC offset and clipped samples.
  **Stop early** with BOOT held + PWR short press again, `stop` on the console, or mute
  (BOOT short press): the file keeps what was recorded so far. Releasing BOOT after the
  combination doesn't count as a BOOT press.
- **`level [s]`** on the console: reads the mic for N seconds (default 5) without the SD
  card and prints the level once a second. A quiet room reads about -55 dBFS; all zeros or
  no audio means a mic problem. Talk during it: the level should rise well above that.
- The mic drops its first 250 ms after each start: the PDM filter starts with a
  near-full-scale click and a settling DC offset. The S3 has no PDM high-pass filter, so a
  DC offset of about +600 stays in the data.
- USB console (`help`): `status`, `batt`, `rails`, `exp`, `time`, `rec [s]`, `stop`,
  `level [s]`, `mute`, `screen`, `sync`, `sd`, `reboot`.

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
rendering) build and run on the host without ESP-IDF:

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
│   ├── mic.[ch]              # I2S PDM RX (shared; pins from the board)
│   ├── recorder.[ch]         # milestone-1 WAV test recorder
│   ├── ui.[ch]               # status screen (text bands, no LVGL)
│   ├── console_cmds.[ch]     # esp_console commands over USB Serial/JTAG
│   ├── wav isotime font5x7 textfb audio_stats .[ch]  # pure, host-tested
├── boards/twatch_ultra/board/
│   ├── pins.h                # pin map, expander map (both candidate readings)
│   ├── board_twatch_ultra.c  # implements board.h
│   ├── axp2101.[ch] xl9555.[ch] pcf85063a.[ch] pcf85063a_codec.[ch] drv2605.[ch]
│   ├── display.[ch]          # CO5300 via espressif/esp_lcd_co5300 (QSPI)
│   ├── sdcard.[ch]           # SD over SPI (bus shared with LoRa/NFC, their CS Hi-Z)
│   └── idf_component.yml
└── test/                     # host tests (plain CMake + ctest)
```

## Third-party code

- `espressif/esp_lcd_co5300` (Apache-2.0) from the ESP-IDF component registry.
- The CO5300 init sequence in `display.c` and the fuel-gauge battery model in
  `board_twatch_ultra.c` are copied from
  [LilyGoLib](https://github.com/Xinyuan-LilyGO/LilyGoLib) (MIT, Copyright (c) 2024
  ShenZhen XinYuan Electronic Technology Co., Ltd), with the notice kept at each copy.
  Register maps were cross-checked against XPowersLib and SensorLib (MIT) but the drivers
  here are our own.

## Not verified on hardware yet

See `PROGRESS.md`. On the owner's watch (2026-10-19) the rails, battery, RTC, SD card,
status screen, buttons, haptics and recording work. In a quiet room `level` shows a
plausible noise floor (about -55 dBFS, never stuck at zero) with the default left PDM slot,
but speech hasn't been checked. Switching to the right slot at runtime gave the same data,
so which slot the mic really uses is still unproven. Still open: talking during `level` or
listening to a recording, SD_DET with the card out, and a recording through the server
pipeline.
