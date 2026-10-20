# PDA firmware: LilyGO T5 E-Paper Pro

The odysseus-sound pocket PDA ([docs/t5-pda-plan.md](../../docs/t5-pda-plan.md)). It shows
the daily briefing, recent conversations, open action items and the week's calendar on
the 4.7" e-paper and queues taps (done, dismiss, add an event, fetch a transcript) as
actions. The phone (android-relay `PdaSync`) or the PC relay (`ble_relay.py pda`) brings
the feed in and takes the actions out over BLE: the glance protocol of plan §3.

- **No Wi-Fi, no tokens.** The T5 talks only BLE to a bonded relay. Wi-Fi is never
  initialised and not linked.
- **Fictional data only** in this repo: `test/fixtures/` comes from
  `tools/make_demo_feed.py`, which mirrors `server/scripts/seed_demo.py`.
- It is its own ESP-IDF project (plan §11 Q1 default). It compiles `crc32.c` and
  `isotime.c` from `../recorder/main/` unchanged and changes nothing in the recorder.

## Layout

| File | What |
|---|---|
| `main/t5_pins.h`, `t5_board.*` | pins and I2C addresses; I2C bus, PCA9535 (LoRa/GPS rail off, side button), PCF8563 RTC, BQ27220 gauge, BQ25896 charger, buttons |
| `main/pda_epd.*` | epdiy (board v7, ED047TC1) and the refresh policy: partial, full, clean |
| `main/touch_gt911.*`, `gesture.*` | GT911 polling and tap / long press / swipe |
| `main/pda_proto.*` | the glance protocol: frames, parsing, the gate, PUT, OB_LIST/OB_ACK, TIME, BYE (pure, host-tested) |
| `main/ble_pda.*` | NimBLE GATT server, pairing window, passkey, advertising |
| `main/json.*`, `feed.*`, `pda_time.*` | JSON tokenizer, feed and detail parsing, feed times (pure) |
| `main/outbox.*`, `pda_actions.*` | persistent action queue, action JSON (pure) |
| `main/feed_store.*` | atomic feed swap, 20-entry detail cache (pure) |
| `main/pda_app.*` | state: feed, outbox, pending / failed lists, clock; the BLE store |
| `main/ui_pda.*`, `gfx.*`, `fonts.*` | the screens of plan §5; text layout; Fira Sans 12/20 (OFL, `main/fonts/`) |
| `main/pda_console.*`, `pda_cmds.c` | USB console |
| `test/` | host tests (no ESP-IDF needed) |
| `tools/pda_serial.py` | console helper: run commands, load a blob, dump the screen as PNG |
| `tools/pda_ble_test.py` | BLE test from a PC (bleak; pairing through WinRT on Windows) |
| `tools/make_demo_feed.py` | writes the fictional fixtures |

Storage: a 2 MB LittleFS partition `storage` in internal flash, mounted at `/lfs`:
`feed.json` (swapped in by rename), `d/` (details), `ob/` (one file per action plus
`next`). NVS holds the BLE bonds and the display's UTC offset.

## Build and flash

ESP-IDF ≥ 5.4 (tested with 5.5.5 here and 5.5.2 in CI):

```bash
cd firmware/pda && idf.py build
```

**Two ESP32-S3 boards may be on the same PC** (the T5 and the T-Watch Ultra). Find the
T5 by its MAC before every flash, because the COM numbers swap:

```bash
python -m esptool --chip esp32s3 -p COM36 read_mac
```

Flash only if it prints the T5's MAC (the owner's is `30:ed:a0:b1:ab:cc`):

```bash
idf.py -p COM36 flash
```

A board that doesn't enumerate: hold BOOT, tap RST, release BOOT (download mode), then
flash again. `idf.py flash` keeps the LittleFS partition, so the feed and outbox survive
a reflash; `wipe` on the console (or the Status screen) clears them.

Host tests:

```bash
cmake -S firmware/pda/test -B build-pda-host && cmake --build build-pda-host && ctest --test-dir build-pda-host
```

## Use

- **Tabs** at the bottom: Today, Briefing, Calendar, Actions, Talks (conversations),
  Status. Swipe left/right for the next tab, up/down (or ▲ ▼) to page.
- **Actions:** tap = done, hold = dismiss, tap a pending one = undo (before the phone has
  taken it; after that, a tap queues `open`). "→ cal" opens the event form with the item's
  title and due date.
- **Calendar:** "+ New" → pick a template (from the feed's `templates`) → day, time slot,
  ±15 min, duration or all day → Add. Queued events show grey until the next feed.
- **Talks:** tap a conversation for its summary; "Transcript" shows the cached page or
  queues a `fetch`; "Next part" follows the page's `next`.
- **Status:** phone, bonds, outbox, feed version, clock, battery; "Pair a phone",
  "Clean refresh", "Wipe cache (hold)".
- **Buttons:** BOOT short = back / Today; BOOT long (1 s) = pairing window (2 min);
  side button = clean refresh. RST resets.
- **Pairing:** open the window, then pair from the phone (android-relay) or the PC
  (Windows Settings → Bluetooth → Add device → `odysseus-pda`, or `tools/pda_ble_test.py
  pair`). The T5 shows the 6-digit code; type it on the other side.

## Console

USB Serial/JTAG, `pda>` prompt; `help` lists everything. `tools/pda_serial.py -p COM36
cmd "<command>"` runs one from a script.

| Command | What |
|---|---|
| `put begin <kind> <bytes> <crc32 hex>`, `put data <base64>`, `put end` | load a blob (1 = feed, 2 = detail) through the same checks as a BLE PUT; `pda_serial.py put <file> [--kind 2]` does it all |
| `feed`, `detail <ref>` | what is stored |
| `ob`, `ob ack <id hex> [result]` | the outbox; ack like a relay would (result 1 = rejected) |
| `screen <name>`, `tap <x> <y>`, `hold <x> <y>`, `swipe <dir>`, `button boot\|side [long]`, `redraw [clean]` | drive the UI without touching it |
| `fb` | framebuffer as base64 (`pda_serial.py fb out.png`) |
| `time [<epoch\|ISO> [utc_offset_min]]` | clock and display offset (normally set by TIME) |
| `ble [pair\|forget]` | BLE state, open the pairing window, delete all bonds |
| `wipe` | delete feed, details and outbox |
| `i2c`, `batt`, `rtc [set <t>]`, `touch [trace <s>]`, `epd [bench\|clean]` | board bring-up and timing |
| `reboot` | restart |

Example, with the demo data:

```bash
python tools/pda_serial.py -p COM36 cmd "time 2026-10-20T07:05:00Z 120"
python tools/pda_serial.py -p COM36 put test/fixtures/feed_demo.json
python tools/pda_serial.py -p COM36 fb today.png
```

## Verified on the owner's board (2026-10-20)

- Bring-up: every I2C chip answers (PCA9535 0x20, PCF8563 0x51, BQ27220 0x55, GT911 0x5D,
  BQ25896 0x6B; the TPS65185 at 0x68 only while the panel is powered). ESP32-S3R8, 16 MB
  QIO flash, 8 MB octal PSRAM.
- Refresh times (`epd bench`): clean (one flash cycle + GC16) 1.57 s; full GC16 of a new
  screen ~1.0 s; GL16 partial 0.93–0.98 s whatever the area; nothing changed 0.13 s.
  `epd_clear()` with epdiy's default cycles alone takes ~1.9 s, so the clean refresh uses
  one cycle.
- Touch and buttons, by hand on the board: raw GT911 coordinates are portrait (540 × 960),
  the mapping is swap X/Y + flip Y (four corner targets); a tab tap, done and undo on an
  action item, swipes left and right; the side button (clean refresh); BOOT long (pairing
  window) and short.
- All §5 screens drawn from the demo feed, checked from framebuffer dumps; tap
  navigation, done / dismiss / undo, couldn't-sync, the event form, a queued `cal_add`, a
  queued `fetch` and the transcript appearing when its detail is loaded.
- Reboot and reflash: the feed, the outbox and its id counter, the clock (RTC) and the
  display offset survive.
- BLE from a Windows PC (`tools/pda_ble_test.py`): passkey pairing through WinRT with the
  code from the console, bond kept across reboots, MTU 517, TIME, OB_LIST + OB_ACK, feed
  PUT (4 KB in 0.8 s, 509 bytes per write), a 200 KB detail (7.3 KB/s), bad CRC refused
  with the feed unchanged, disconnect in the middle of a PUT (discarded), a lost OB_ACK
  (the same id is listed again and acked once), BYE and disconnect, the scan response
  with and without the outbox flag.
- A crash found and fixed: a flash write (outbox ack) during a refresh raised "Cache
  disabled but cached memory region accessed" in epdiy's LCD interrupt;
  `CONFIG_SPIRAM_XIP_FROM_PSRAM` keeps the cache on during flash writes.

## Not verified yet

- The PDA screens' look on the real panel: the owner saw the bring-up screen (title, 16-step
  grey ramp, touch targets) and reported no ghosting, missing text or orientation problem;
  the PDA screens themselves were checked through framebuffer dumps. Ghosting after a day
  of partial updates is untested.
- Pairing being refused outside the window, re-pairing, and `ERROR mtu_too_small` (host
  tests only; Windows always negotiates 517).
- The sibling relays against this firmware: the PC relay's `pda` subcommand and the
  Android `PdaSync` (they were written against the same spec and a fake T5).
- Battery life, light sleep (not implemented: the T5 stays awake with DFS; plan
  milestone 5), deep sleep with an advertising window, flash encryption.
- Events after a daylight-saving change within the week are shown and created one hour
  off (the T5 knows only the phone's current UTC offset; plan §5 note).
