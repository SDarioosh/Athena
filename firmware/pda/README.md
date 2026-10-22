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
| `main/t5_pins.h`, `t5_board.*` | pins and I2C addresses; I2C bus, PCA9535 (LoRa/GPS rail off, side button), PCF8563 RTC, BQ27220 gauge, BQ25896 charger, buttons, power management (light sleep, PM locks) |
| `main/pda_epd.*` | epdiy (board v7, ED047TC1) and the refresh policy: partial, full, clean |
| `main/touch_gt911.*`, `touch_idle.*`, `gesture.*` | GT911 on its INT line (polled while a finger is down, safety-net poll when idle) and tap / long press / swipe |
| `main/pda_proto.*` | the glance protocol: frames, parsing, the gate, PUT, OB_LIST/OB_ACK, TIME, BYE (pure, host-tested) |
| `main/ble_pda.*` | NimBLE GATT server, pairing window, passkey, advertising |
| `main/json.*`, `feed.*`, `pda_time.*` | JSON tokenizer, feed and detail parsing, feed times (pure) |
| `main/outbox.*`, `pda_actions.*` | persistent action queue, action JSON (pure) |
| `main/feed_store.*` | atomic feed swap, 20-entry detail cache (pure) |
| `main/pda_app.*` | state: feed, outbox, pending / failed lists, clock; the BLE store |
| `main/ui_pda.*`, `ui_chrome.*`, `gfx.*`, `fonts.*` | the screens of plan §5; the header, tabs, headings and Asleep screen; text layout, round ticks, the battery icon; Fira Sans Regular 12/20 and SemiBold 12/20/32 (OFL, `main/fonts/`) |
| `main/today_plan.*`, `ui_today.*`, `ui_layout.h` | the Today tab: the plan from the hub's insight or from rules (pure), its drawing (gfx only, rendered on the host), the screens' geometry |
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

`test_ui_today` (needs zlib) renders the Today tab from the fictional fixtures through a
host stand-in for epdiy (`test/host/`) and writes the pages to
`build-pda-host/today_screens/*.png`: `today_insight_*` (with the hub's insight),
`today_insight_ticked_p1` (a to-do ticked on the T5), `today_rules_*` and
`today_rules_busy_*` (no insight: the rules), `today_empty_p1`, `today_long_p1`.

## Use

- **Portrait**, 540 × 960. Two orientations: 270 (default, the way LilyGO's factory firmware
  holds the board) and 90 (turned 180°): Status → "Turn 180°" or `rotate 90|270` on the
  console; kept in NVS.
- **Tabs** at the bottom: Today, Brief, Cal, Tasks, Talks (conversations), Status. On a tab: swipe left/right for the next/previous tab, up/down (or ▲ ▼) to page. In a detail (conversation, transcript, event): swipe right = back to the list, left = next page, then the next transcript part or the next item, nothing at the end.
- **Today:** the hub's "Today insight" when the feed has one for today (headline, then DO
  FIRST, COMING UP, ALSO WORTH A LOOK, each item with the hub's reason), else the same
  sections from rules over the checklists and the calendar (overdue, due today, today's
  events; the next 7 days; pinned, then undated; shopping lists without a due date left
  out). Tap a to-do to tick it (as on Tasks), an event for its details; "Digest ready on
  Brief ›" opens the digest. Plan §5 "Today as built" has the rules.
- **Tasks:** the owner's Odysseus checklists (feed `todos`, plan §4.4), a heading per note
  (★ pinned, due date on the right), done items struck through. Tap an item: done / not
  done (`todo_toggle`, shown at once in grey); tap it again before the phone has taken it to
  undo, after that it queues the reverse. Without Odysseus: "No checklists".
- **Suggested action items** (feed `actions`) are in their conversation's detail screen,
  below the summary: tap = done, hold = dismiss, tap a pending one = undo, "→ cal" opens
  the event form with the item's title and due date.
- **Calendar:** "+ New" → pick a template (from the feed's `templates`) → day, time slot,
  ±15 min, duration or all day → Add. Queued events show grey until the next feed.
- **Talks:** tap a conversation for its summary; "Transcript" shows the cached page or
  queues a `fetch`; "Next part" follows the page's `next`.
- **Status:** PHONE (connected or last seen, phones paired, changes waiting), BATTERY, and
  DETAILS (feed version, clock, screen, firmware, BLE name, battery mV); "Pair a phone",
  "Clean refresh", "Turn 180°", "Light", "Wipe cache (hold)".
- **Buttons:** BOOT short = back / Today; BOOT held 1 s = sleep (deep sleep, an "Asleep"
  screen stays on the panel with the next event, its date and the battery at sleep; press BOOT to wake, which restarts the firmware and redraws
  Today with one clean refresh, a single flash); the button next to BOOT (LilyGO's S3, labelled IO48 on the board, read through
  the PCA9535 IO1_2; GPIO48 itself is the e-paper's CKV line) short = front light on/off,
  held 1 s = sync mode (the 2-minute pairing window with fast advertising, so a phone
  finds it quickly). Clean refresh is on the Status screen. RST resets. The power button
  is hardware only: the firmware never sees it (checked on the board, 2026-10-20).
- **Front light:** the IO48 button toggles it between off and the last level used (50 %
  the first time); "Light" on the Status screen steps off → 25 % → 50 % → 100 % → off (kept
  in NVS). It stays on while the screen is used and goes off after 60 s without a touch or
  button; the next touch or button turns it on again. Off in sleep.
- **Sync while asleep:** asleep, the T5 also wakes on a timer when the phone's next sync is
  due (15 min after the last one, android-relay's default `pdaSyncMinutes`), without the
  UI, the front light or a screen refresh: BLE for bonded phones only (fast advertising,
  no pairing window) for about a minute, a sync if the phone comes, then back to sleep
  (at most 4 min awake; `main/sleep_plan.h` explains the numbers). If a new feed came,
  only the Asleep screen's "synced HH:MM" line is redrawn. BOOT during such a wake starts
  the UI. A phone set to a longer interval still syncs (one wake in between misses); with
  auto sync off the wakes find nobody.
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
| `open transcript:<ref>` | show a transcript page (cached, or queue a `fetch`) |
| `ob`, `ob ack <id hex> [result]` | the outbox; ack like a relay would (result 1 = rejected) |
| `screen <name>`, `tap <x> <y>`, `hold <x> <y>`, `swipe <dir>`, `button boot\|side [long]`, `redraw [clean]` | drive the UI without touching it |
| `fb` | framebuffer as base64 with the rotation (`pda_serial.py fb out.png` writes it upright) |
| `time [<epoch\|ISO> [utc_offset_min]]` | clock and display offset (normally set by TIME) |
| `ble [pair\|forget]` | BLE state, open the pairing window, delete all bonds |
| `light [0-100]` | front light level (kept in NVS) |
| `sleep [<min>\|off\|info]` | deep sleep as BOOT held (the USB console goes away); `<min>`: the first sync wake in that many minutes; `off`: no sync wakes; `info`: sync-wake counters since power-on |
| `rotate [90\|270]` | portrait orientation |
| `wipe` | delete feed, details and outbox |
| `i2c`, `batt`, `rtc [set <t>]`, `touch [trace <s>]`, `epd [bench\|clean]` | board bring-up and timing |
| `power` | light sleep on/off, CPU clock range, what keeps the T5 awake (PM locks) |
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
- Portrait (270°) by hand: text upright, tab taps, done + undo on the first item, opening
  a conversation, swipes both ways land where they should.
- All §5 screens drawn from the demo feed in landscape, then in portrait (both
  orientations), checked from framebuffer dumps; tap
  navigation, done / dismiss / undo, couldn't-sync, the event form, a queued `cal_add`, a
  queued `fetch` and the transcript appearing when its detail is loaded.
- Reboot and reflash: the feed, the outbox and its id counter, the clock (RTC) and the
  display offset survive.
- BLE from a Windows PC (`tools/pda_ble_test.py`): passkey pairing through WinRT with the
  code from the console, bond kept across reboots, MTU 517, TIME, OB_LIST + OB_ACK, feed
  PUT (4 KB in 0.8–1.0 s, 507 bytes per write: the 512-byte attribute cap), a 200 KB detail (7.3 KB/s), bad CRC refused
  with the feed unchanged, disconnect in the middle of a PUT (discarded), a lost OB_ACK
  (the same id is listed again and acked once), BYE and disconnect, the scan response
  with and without the outbox flag.
- Idle link rule: 60 s without a `ctrl` write or a `status` read (plan §3.6).
- A crash found and fixed: a flash write (outbox ack) during a refresh raised "Cache
  disabled but cached memory region accessed" in epdiy's LCD interrupt;
  `CONFIG_SPIRAM_XIP_FROM_PSRAM` keeps the cache on during flash writes.

## Not verified yet

- The PDA screens' look on the real panel: the owner saw the bring-up screen (title, 16-step
  grey ramp, touch targets) and reported no ghosting, missing text or orientation problem;
  the PDA screens themselves were checked through framebuffer dumps. Ghosting after a day
  of partial updates is untested.
- Grey doesn't show on the owner's panel (2026-10-20): grey text and lines were invisible
  on the PDA screens, so the UI draws only black and white (`GFX_FAINT` and `GFX_PENDING`
  are black in `main/gfx.h`). Use layout, labels, marks and font weight (SemiBold titles,
  Regular details), not grey, to set things apart.
- The 2026-10-21 look (SemiBold fonts, plain tabs, round ticks, the time column, the new
  header, Status sections and Asleep screen) and the tap feedback (a black frame with a DU
  update before a new screen): every tab read back from the T5's framebuffer and drawn
  right; the DU tap frame and the Asleep screen not yet looked at on the panel.
- Tab and menu changes are partial (GL16) updates since 2026-10-21, with no flash (the
  owner's choice); ghosting left by them until the next clean refresh (at most 10 counted
  updates) not yet judged on the panel. Turning the screen 180° stays a full update.
- Pairing being refused outside the window, re-pairing, and `ERROR mtu_too_small` (host
  tests only; Windows always negotiates 517).
- The Service Changed indication from an Android phone (the GATT client is compiled in for
  it, as on the T-Watch, SDarioosh/barvis-sound#49; Windows never sends one).
- The sibling relays against this firmware: the PC relay's `pda` subcommand and the
  Android `PdaSync` (they were written against the same spec and a fake T5).
- Battery life, flash encryption.
- The Today tab with the insight (2026-10-21): built, host-tested and rendered on the host
  (`test_ui_today`), **not on the board yet**. To check with the fixture, as in the example under
  Console (`cmd "time 2026-10-20T07:05:00Z 120"`, `put test/fixtures/feed_insight_demo.json`,
  `fb today.png`):
  the headline on three lines, DO FIRST with the two to-dos then the stand-up, lunch and
  the 1:1 (not the 08:00 call), page 2 with Aunt Rosa; tap the first to-do: the tick and
  strike-through show at once and `ob` lists a `todo_toggle`; tap an event: its details;
  "Digest ready on Brief ›" opens Brief. With `feed_demo.json` the rule-based sections and
  "No insight for today yet".
- Idle power (2026-10-20, `feature/t5-idle-power`): built, **not on the board yet**.
  Automatic light sleep (DFS 240/40 MHz), held off while the front light is lit, during a
  refresh and while USB power is in; touch on the GT911 INT line with a safety-net poll;
  buttons polled every 50 ms. Rebuild from a fresh `sdkconfig` (delete it first: the new
  `sdkconfig.defaults` entries don't override an existing one). To check:
  - Boot log: `INT <mode> (0x804D = 0x..)` from `touch` (note the mode), and no
    "light sleep mode will not be able to apply when bluetooth is enabled" from BLE.
  - On USB: `power` says light sleep on, kept awake by USB yes; the console works as before.
  - Touch with INT: after a few taps the log says "INT works: safety-net poll every
    1000 ms"; `touch` shows touches started by INT counting up and by poll at 0. If instead
    "a poll found a touch INT didn't report" or "INT stays at its active level" appears,
    paste the `touch` output: INT doesn't wake it and taps under 250 ms can be lost.
  - Taps, long press (700 ms) and swipes work as before, also as the first touch after a
    minute untouched (on battery, i.e. with light sleep); the first tap after idle isn't
    noticeably slow.
  - Buttons: a quick BOOT and IO48 press (as short as you can) are each reported once;
    held 1 s still gives `long` at 1 s; waking from deep sleep with BOOT still gives no
    stray "back".
  - Front light at 25 % stays steady (no flicker) for its 60 s, also on battery.
  - BLE on battery: the phone still finds the T5 for a due sync and "Sync T5 now", stays
    connected through a sync, and the clock's minute tick keeps time.
  - Unplug USB: `USB power out: light sleep on` in the log; plug in again and the
    console comes back within about 1 s (`USB power in`).
  - Current on battery with the screen idle and no link (`batt` mA, or a meter): before
    and after this change.
  - Deep sleep (BOOT held 1 s) still sleeps until a BOOT press (no instant wake from the
    touch INT or the light-sleep timer).
- Sync wakes from deep sleep and the no-flash wake (2026-10-20): built and host-tested
  (`test_sleep_plan`), **not on the board yet**. To check: the front light stays dark and
  the panel doesn't change during a timer wake (except the "synced HH:MM" line after a new
  feed, with no flash and no ghost of the old line); the phone syncs within a minute of
  each wake (`sleep info` counts wakes, syncs, misses); the deep-sleep timer's drift (the
  log's "woke N s early"); BOOT during a timer wake starts the UI; a BOOT wake shows Today
  with a single flash; the GT911 stays asleep through timer wakes (sleep current with and
  without them).
- Front light, sleep and the new button mapping (2026-10-20): built, **not on the board
  yet**. To check: the light's PWM (1 kHz on GPIO11) dims in steps without flicker or
  whine, and stays dark in sleep; BOOT held 1 s shows "Asleep" and the T5 sleeps once
  BOOT is released; a BOOT press wakes it without entering download mode and without a
  stray "back"; the IO48 button held 1 s opens sync mode (Status shows "Pairing open");
  sleep current. Checked so far: the IO48 button is the PCA9535 button (three presses
  logged as `button side`), and its short press toggles the light (console).
- Events after a daylight-saving change within the week are shown and created one hour
  off (the T5 knows only the phone's current UTC offset; plan §5 note).
