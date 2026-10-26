# Plan: LilyGO T5 E-Paper Pro as an Athena PDA

Status: **firmware milestones 0–3 built** in `firmware/pda/` (2026-10-20; what is verified on
the board is in `firmware/pda/README.md`). The PC relay (§3.5, `ble_relay.py pda`) and the
phone side (`android-relay` `PdaSync`) are built and tested against fakes, not yet against
the real T5. Owner decisions are open (§11).

## Why

- The owner has a **LilyGO T5 E-Paper Pro** (ESP32-S3, 4.7" e-paper, touch). It becomes a
  pocket PDA. It shows the daily briefing, conversation insights, open action items and the
  calendar, and it sends simple actions back: mark an action item done, add an event.
- Reading is what e-paper is good at. It is readable in sunlight and draws no power between
  refreshes. Typing is slow on it, so the UI is built around taps, not text entry (§5).
- It reuses what the T-Watch Ultra work already built: the ESP-IDF board layer in
  `firmware/recorder/`, the BLE framing, pairing and delivery rules of
  [twatch-ble-protocol.md](twatch-ble-protocol.md), and the Android relay app.

## Decisions

- **Transport: BLE to the phone only.** The server can be reached from outside the home
  network only through Tailscale, and an ESP32 can't run Tailscale. The phone is already on
  the tailnet and already runs `android-relay`, so the phone is the bridge. The T5 never
  talks to the server.
- **Wi-Fi: off in v1.** The T5 holds no server or Odysseus token, and nothing is reachable
  over IP. A later LAN-only Wi-Fi mode for home use is possible (§11 Q3), but it isn't
  needed: at home the phone is in range anyway.
- **No Tailscale Funnel or other public exposure**, ever (AGENTS.md privacy rules).
- **The phone builds the feed.** It calls the Athena HTTP API and the Odysseus
  calendar REST API, then sends a compact feed to the T5 (§4). Neither server needs a
  new endpoint for v1.
- **The MCP contract doesn't change.** The PDA uses the HTTP API (`/v1/…`), not MCP.

## TL;DR

```
Athena  /v1/digests, /v1/conversations, /v1/action-items ─┐
                                                                   ├─ Tailscale ─ phone (android-relay)
Odysseus        calendar REST (scoped token)  ─────────────────────┘        │
                                                                       BLE (§3)
                                                                            │
                                                     T5 E-Paper Pro: feed cache + outbox
```

- **Down:** the phone builds a feed of about 5–30 KB of JSON and writes it to the T5. The T5
  stores it in internal flash and draws its screens from it. The last feed stays on screen
  while offline.
- **Up:** a tap stores an **action** in the T5's outbox. The phone reads it, calls the server
  and acks it only after the server answers OK. That is the same delivery rule as the watch
  chunks (protocol §1).
- **On demand:** opening a transcript that isn't in the feed is also an outbox action
  (`fetch`). The phone answers with a detail blob over the same download path.

## 1. Hardware facts (confirmed at bring-up, 2026-10-20)

Checked on the owner's board (LilyGO "T5 E-Paper S3 Pro", H752-01, schematic V1.0 24-12-24)
against LilyGO's `docs/pinmap.md` and examples (github.com/Xinyuan-LilyGO/T5S3-4.7-e-paper-PRO).

| Part | Confirmed | Use |
|---|---|---|
| ESP32-S3R8 (QFN56, rev 0.2), 16 MB QIO flash, 8 MB **octal** PSRAM | esptool `flash_id`, boot log | firmware; epdiy's framebuffers (3 × 260 KB) in PSRAM; code and rodata run from PSRAM too (`CONFIG_SPIRAM_XIP_FROM_PSRAM`, §2) |
| 4.7" ED047TC1, 960×540, 16 greys, 8-bit parallel bus; TPS65185 power IC (0x68) | drawn and timed; the owner saw all 16 grey steps and clean text | epdiy 2.1.3 board **`epd_board_v7`** works unchanged; VCOM 1560 mV (LilyGO's default) |
| PCA9535 I/O expander (0x20, INT GPIO38) | read back | port 1: e-paper control (epdiy); IO1_2 the side button (epdiy makes it an output, the firmware sets it back to input); IO0_0 the LoRa/GPS rail enable, held off |
| GT911 touch (0x5D, INT GPIO3, RST GPIO9) | taps on four corner targets | its raw frame is portrait (540 × 960): swap X/Y and flip Y gives the upright landscape panel, then the screen's portrait rotation is applied |
| PCF8563 RTC (0x51, INT GPIO2), coin cell holder | kept the time over reboots | wall clock between phone connections |
| BQ25896 charger (0x6B), BQ27220 fuel gauge (0x55) | 4.13–4.17 V, 93–99 %, USB, charging | battery %, charging state |
| Buttons: BOOT (GPIO0), side button (PCA IO1_2, active low), RST | pressed by hand: BOOT short and long, side short | BOOT: back, held 1 s = deep sleep (a BOOT press wakes it, EXT0); S3 button next to BOOT (silkscreen IO48, but read on PCA IO1_2; GPIO48 is CKV): front light on/off, held 1 s = sync mode (pairing window); the power button isn't readable |
| Front light (GPIO11, LED driver enable) | PWM built, not yet on the board | LEDC 1 kHz; level on the Status screen (off, 25, 50, 100 %), off after 60 s idle and in sleep |
| microSD (SPI, CS 12), LoRa SX1262 and GPS on the Pro | not touched | not used (§7) |

- **The screen is portrait (owner's choice, 2026-10-20):** a logical 540 × 960 screen on
  the 960 × 540 panel through epdiy's rotation (drawing and partial-update areas), and
  touches mapped the same way. Two orientations, a console (`rotate 90|270`) and Status
  screen ("Turn 180°") setting kept in NVS: **270 (default)** is `EPD_ROT_INVERTED_PORTRAIT`,
  the orientation LilyGO's factory firmware uses; 90 is the same turned 180°.
- The I2C bus is SDA 39 / SCL 40 for every chip. The firmware creates it and hands it to
  epdiy (`epd_init_with_config`), so there is one ESP-IDF `i2c_master` bus.
- **The COM ports swap.** On the owner's PC the T5 was COM35 first and later COM36 (the
  T-Watch Ultra took the other one). Identify it by MAC before every flash (`esptool
  read_mac`, `30:ed:a0:b1:ab:cc`), never by the port number.
- **Refresh times** (`epd bench` on the console). epdiy only drives the pixels that differ
  from what it last drew, so the time doesn't depend on the area:

  | Update | Time |
  |---|---|
  | partial, GL16 (no flash), any area that changed | 0.93–0.98 s |
  | full, GC16 (flashing), a new screen | ~1.0 s |
  | clean: one black/white flash cycle, then GC16 of every pixel | 1.57 s |
  | nothing changed | 0.13 s |
  | `epd_clear()` alone (epdiy's default cycles) | ~1.9 s, so the clean refresh uses one cycle |

  Because only changed pixels are driven, ghosting builds up in areas that never change;
  only the clean refresh clears it (§5).

## 2. Firmware

- **As built (§11 Q1 default):** a separate ESP-IDF project, `firmware/pda/`. It compiles
  `crc32.c` and `isotime.c` from `firmware/recorder/main/` unchanged and changes nothing in
  the recorder. The recorder's `board.h` is about mics, rails and the SD card, so the T5's
  board layer is PDA-specific and lives in the project's `main/` (`t5_pins.h`,
  `t5_board.*`, `pda_epd.*`, `touch_gt911.*`).
- **Reused:** the pairing and bonding rules and the NimBLE structure of the recorder's
  `ble_sync.c` (copied into `ble_pda.c`, not shared, because the services differ), the
  `ble_proto` encode/decode and gate style (`pda_proto.c`), `isotime`, `crc32`.
- **New:**
  - `epd` (epdiy wrapper)
  - `touch` (GT911)
  - `feed_store` (atomic feed swap in flash)
  - `outbox` (persistent action queue)
  - `ui_pda` (screens, §5)
  - `ble_pda` (the GATT service of §3)
- **Rendering:** the T5 draws its own screens from the feed. Use one or two bitmap fonts
  (two sizes, Latin-1 plus the punctuation that summaries use) and no LVGL in v1. LVGL's
  redraw model fights e-paper refresh, and the screens are simple lists.
- **Storage:** a LittleFS partition of 2 MB (`storage`) in internal flash, holding:
  - `feed.json` and `feed.tmp`, swapped atomically after the CRC checks out. A PUT is
    buffered in PSRAM and written to `feed.tmp` only at `PUT_END`, after the size and CRC
    check, then renamed over `feed.json`.
  - the outbox records: one file per action (`ob/<id>.act`: a header with the id, time,
    length and CRC-32, then the JSON) and `ob/next`, the id counter. Ids are never reused; a
    brand-new outbox starts at a random base below 2^31.
  - up to 20 cached detail blobs (`d/<seq>-<crc32(ref)>.json`), evicted oldest first
- **Flash writes during a refresh:** with code in flash, a LittleFS or NVS write disables
  the cache, and epdiy's LCD interrupt then reads the framebuffer in PSRAM and crashes
  ("Cache disabled but cached memory region accessed", seen on the board).
  `CONFIG_SPIRAM_XIP_FROM_PSRAM` runs code and rodata from PSRAM, so flash writes don't
  disable the cache.
- **Fonts:** Fira Sans (SIL OFL): Regular 12 and 20 pt, epdiy's pre-converted headers
  (ASCII, Latin-1, general punctuation, arrows); SemiBold 12, 20 and 32 pt, converted with
  epdiy's `fontconvert.py` (Latin-1 and punctuation; 32 pt ASCII only). Characters a
  SemiBold font lacks (arrows, ✓, ★, ▲) come from the Regular font of the nearest size.
  The euro sign isn't in them and is drawn as "EUR"; other missing characters as "?".
- **Sleep:** between events the T5 is in automatic light sleep (ESP-IDF PM, DFS 240/40 MHz;
  the BLE controller keeps the main crystal as its sleep clock). It stays awake during a
  refresh, while the front light is lit and while USB power is in (the USB console). Touch
  wakes it on the GT911 INT line (with a slow safety-net poll); the buttons are polled every
  50 ms (the side button is on the PCA9535, whose INT is epdiy's). The e-paper keeps the
  image. Deep sleep (BOOT held) wakes on BOOT and on a timer aimed at the phone's next due
  sync (last BYE + 15 min): no UI, no light, no refresh, BLE for bonded relays for about a
  minute, then sleep again (`main/sleep_plan.h`, `main/pda_sleep.c`). Waking skips epdiy's
  full clear: the first screen is one clean refresh. Built 2026-10-20, not yet measured on
  the board.

## 3. BLE service ("glance" protocol v1)

The T5 is the peripheral (GATT server), and the phone is the central, as with the watch. Our
own random base: `d023xxxx-51f4-43be-86e4-1653768283cd`.

| Name | UUID | Properties | Security | Value |
|---|---|---|---|---|
| service | `d0230000-51f4-43be-86e4-1653768283cd` | primary | | |
| `info` | `d0230001-…` | read | none | `proto=1;device=t5-pda;fw=…;board=t5_epaper_pro` |
| `status` | `d0230002-…` | read, notify | authenticated | struct (§3.2) |
| `ctrl` | `d0230003-…` | write (with response) | authenticated | one command (§3.3) |
| `data` | `d0230004-…` | notify | authenticated | responses (§3.3) |

- **Framing, MTU rules, one command at a time, `ERROR` format and error codes** are the
  same as watch protocol §5. Little-endian.
- **Pairing:** the same as watch protocol §3.1: passkey entry with LE Secure Connections
  and an authenticated key. New bonds are accepted only in a 2-minute pairing window. The T5
  has a large screen, so it shows the code there. The feed holds conversation summaries,
  so an unauthenticated link must never read it.
- The BLE name is `odysseus-pda`. The T5 advertises the service UUID, so the relay's scan
  filter can tell it from `barvis-tw`.

### 3.1 Blobs

All downloads are **blobs**: a JSON document identified by `kind` and `blob_id`.

- `kind` 1 is the **feed** (§4). The T5 keeps exactly one.
- `kind` 2 is a **detail** (`fetch` reply, §4.3). It is cached by `ref`.
- A blob is at most 256 KB. The CRC-32 is the same as in the watch protocol.

### 3.2 `status` (read, notify; 16 bytes)

| Offset | Type | Field |
|---|---|---|
| 0 | u8 | `proto` (1) |
| 1 | u8 | flags: bit0 time valid, bit1 charging, bit2 USB power, bit3 pairing window open, bit4 user is active (screen awake) |
| 2 | i8 | battery %, -1 if unknown |
| 3 | u8 | reserved |
| 4 | u16 | outbox entries waiting |
| 6 | u16 | reserved |
| 8 | u32 | `feed_version` of the stored feed, 0 if none |
| 12 | u32 | `boot_id` |

The T5 notifies `status` when the outbox changes. The relay reacts to a new outbox entry
within seconds while connected.

### 3.3 Commands

| Command (phone → `ctrl`) | Bytes | Responses (`data`) |
|---|---|---|
| `TIME` | `04 00 00 00` `epoch_ms:u64` `tz_offset_min:i16` | `TIME_OK` |
| `PUT_BEGIN` | `10` `kind:u8` `00 00` `blob_id:u32` `total:u32` `crc32:u32` | `PUT_READY` |
| `PUT_DATA` | `11` `offset:u32` `payload…` (≤ MTU − 3 − 5) | none (write with response is the flow control) |
| `PUT_END` | `12` | `PUT_OK` or `ERROR bad_crc` |
| `OB_LIST` | `20` `after_id:u32` `max:u16` | 0..`max` × `OB_ENTRY`, then `OB_END` |
| `OB_ACK` | `21` `action_id:u32` `result:u8` | `OB_ACKED` |
| `BYE` | `05` | `BYE_OK` |

| Response (`data`) | Bytes |
|---|---|
| `TIME_OK` | `86 00 00 00` |
| `PUT_READY` | `90` `kind:u8` `00 00` `blob_id:u32` |
| `PUT_OK` | `91` `kind:u8` `00 00` `blob_id:u32` |
| `OB_ENTRY` | `A0` `00` `len:u16` `action_id:u32` `created_ms:u64` `json…` (`len` bytes, ≤ 400, so one notification at MTU ≥ 420) |
| `OB_END` | `A1 00` `count:u16` `remaining:u32` |
| `OB_ACKED` | `A2` `result:u8` `00 00` `action_id:u32` |
| `BYE_OK` | `87` |
| `ERROR` | as watch protocol §5, plus code 8 `bad_crc`, 9 `too_large`, 10 `bad_state` (`PUT_DATA` without `PUT_BEGIN`) |

- **MTU ≥ 420** for this service (Android requests 517). A smaller MTU gets
  `ERROR mtu_too_small` on every command.
- **PUT** writes into a temporary file. On `PUT_END` the T5 checks the size and CRC, then
  swaps the file in atomically and redraws. A disconnect mid-PUT discards the temporary file.
  Blobs are small, so a PUT never resumes; it starts again.
- **`OB_ACK.result`** reports the server's answer back to the user:
  - 0 = done
  - 1 = rejected for good (e.g. 404, the item is gone). The T5 shows "couldn't sync" on that
    item and drops the action.
  - The relay never acks a temporary failure. The action stays queued.

### 3.4 Relay algorithm (phone)

1. Scan with both service UUIDs in the filter, and connect to whichever device shows up.
   The watch drain and the PDA sync are separate jobs that run one at a time on the BLE
   stack.
2. Request MTU 517, then subscribe to `data` and `status`. This triggers pairing on a new
   phone. Read `info` and check `proto=1`, then send `TIME`.
3. If `GET <server>/health` fails, show "offline" (by leaving the feed unchanged) and
   `BYE`. Back off: 1, 2, 5, 10, 15 min.
4. **Outbox first:** `OB_LIST`. For each action in order, call the server (§4.2). Then send
   `OB_ACK`: result 0 on 2xx, result 1 on a final 4xx, and no ack on 5xx or a network
   error (retry next sync).
5. **Feed:** build it (§4.1). If its content hash differs from the last one pushed to this
   T5, `PUT` it. Then answer any `fetch` actions by `PUT`ting their detail blobs.
6. **Stay connected** while `status` says the user is active. Then push outbox replies
   within seconds, and refresh the feed every 5 min. Otherwise `BYE`, and reconnect every
   15 min, and also as soon as the T5 advertises with "outbox waiting" in its scan
   response.

### 3.5 Details settled by the PC relay (v1)

The PC relay (`firmware/recorder/tools/barvis_relay/pda_*.py`, `ble_relay.py pda`) fixed
these points where the text above left a choice. The firmware and the phone follow them.

- **`TIME`** is 14 bytes. `tz_offset_min` is the UTC offset of the relay's time zone
  (`--tz`, the feed's `tz`) at that moment.
- **Scan response, "outbox waiting":** Service Data with a 128-bit UUID (AD type `0x21`)
  for the service UUID, one flags byte, bit0 = outbox not empty. Without it the relay
  waits for its 15-minute reconnect.
- **`OB_LIST`** asks for at most 16 entries. The relay continues with `after_id` = the last
  id listed while `remaining > 0`.
- **Order in one sync:** the actions' server calls run in outbox order, and each action
  except `fetch` is acked at once. Then the feed. Then each `fetch` reply is `PUT` and
  acked with result 0 after its `PUT_OK`. A `fetch` the server refuses for good (404) is
  acked with result 1 and no `PUT`.
- **Temporary failure stops the sync:** a 5xx, 408, 429 or network error on an action, or
  401/403, stops the sync at that action. Nothing after it is acked, no feed is built, the
  relay sends `BYE` and backs off. That keeps the actions in order.
- **Calendar:** the PC relay calls the Odysseus token routes of §6. It needs
  `--odysseus-url`, a token scoped to `calendar:read` and `calendar:write` (from
  `ODYSSEUS_TOKEN` or `--odysseus-token-file`; never on the command line), and
  `--calendar-href`, the CalDAV calendar new events go to.
  - Without the URL and token, no calendar is configured: `cal_add` and `cal_delete` stay
    queued without an ack and don't block the other actions, and the feed's `calendar` is
    `[]`.
  - The event mapping: `uid` becomes `id`, `summary` becomes `title`, `dtstart`/`dtend`
    become `start`/`end` (as given; all-day ends are exclusive), `location` becomes
    `where`.
  - A `cal_add` sends `summary` and `dtstart`, plus `dtend` when it is timed, plus
    `all_day` and `calendar_href`.
  - A `cal_delete` of an occurrence id (`<uid>::<occurrence>`) is result 1. The token route
    deletes the whole series, and one tap must not remove a recurring event.
  - Test against the Odysseus test instance (`127.0.0.1:7010`), never production.
- **`cal_delete` of an event that is already gone** (404) is result 0, not 1: after a
  lost `OB_ACKED` the T5 sends it again, and the user must not see "couldn't sync".
- **Strict checks** (anything else is acked with result 1):
  - exactly the fields of §4.2 for each `t`, no others; at most 400 bytes of UTF-8 JSON
  - `item_status`: `id` a positive integer
  - `cal_add`: `title` 1–200 characters. With `all_day: true`, `start` is `YYYY-MM-DD` and
    there is no `end`. Otherwise `start` and `end` are ISO-8601 date-times with an offset
    or `Z` (a time without an offset is refused), and `end` is after `start`.
  - `cal_delete`: `id` a string of up to 200 characters
  - `fetch`: conversation ids match `[A-Za-z0-9_.:-]{1,64}`; a page range
    (`@<from>-<to>`, seconds, up to 3 decimals) needs `transcript:` and `from < to`
  - `digest_refresh`: `date` is a real `YYYY-MM-DD` date
- **`cal_add` dedupe:** the key is `action_id` plus a hash of the action's JSON, so a T5
  whose ids start again after a wipe isn't taken for a repeat. The last 200 are kept in
  the relay's state file.
- **Blob ids:** a feed's `blob_id` is its `version`. A detail's `blob_id` is the `fetch`
  action's `action_id`.
- **`PUT_DATA`** carries min(MTU − 3, 512) − 5 bytes, so a frame is never longer than the
  512-byte limit on an attribute value. Android refuses longer writes, and BlueZ and
  Windows may too. That is 507 bytes at MTU 517 and 412 at MTU 420. If the OS doesn't
  report the MTU, 420 is assumed. The T5 accepts frames of up to 512 bytes, and a `data`
  notification is at most 512 bytes.
  On `ERROR bad_crc` at `PUT_END` the relay sends the blob again, 3 times in all.
- **Stay connected:** the relay waits for `status` notifications and also reads `status`
  every 30 s. It refreshes the feed every 5 min, and sends `BYE` once bit4 (user active)
  is clear.
- **Token:** the PC relay uses the server's `CONVO_INGEST_TOKEN`, which every `/v1` route
  accepts. It goes only in the `Authorization` header.

### 3.6 Details settled by the firmware (v1)

`firmware/pda/main/pda_proto.c` and `ble_pda.c`; byte vectors in
`firmware/pda/test/test_pda_proto.c`. They agree with the PC relay's §3.5 where both say something.

- **Checks on every `ctrl` write**, in order: another command still running →
  `ERROR busy`; MTU < 420 → `ERROR mtu_too_small` (arg = MTU); then the length:
  `TIME` 14, `BYE` 1, `PUT_BEGIN` 16, `PUT_DATA` ≥ 6 (header + at least one byte),
  `PUT_END` 1, `OB_LIST` 7, `OB_ACK` 6; anything else → `ERROR bad_length`.
- **`TIME`:** `epoch_ms` outside 2024-01-01 … 2100-01-01 or `tz_offset_min` beyond ±840 →
  `ERROR bad_time`. It sets the system clock, the RTC and the display offset (kept in NVS).
- **`PUT_BEGIN`:** kind not 1 or 2 → `ERROR bad_state` (arg = kind); `total` 0 →
  `bad_length`; over 256 KiB → `too_large` (arg = total). A `PUT_BEGIN` while a PUT is
  open drops that PUT and starts again.
- **`PUT_DATA`** goes in order: `offset` ≤ the bytes received so far (rewriting is fine, a
  gap isn't). A gap → `ERROR bad_state` (arg = bytes received); past `total` →
  `too_large` (arg = offset); no PUT open → `bad_state` (arg 0). Every `PUT_DATA` error
  **drops the PUT**, so the following `PUT_END` answers `bad_state` (arg 0); start again
  with `PUT_BEGIN`. The payload is copied in the write callback itself, so the write
  response means the bytes are in.
- **512-byte attribute cap** (Core Vol 3 Part F 3.2.9): every notification and every write
  is at most min(MTU − 3, 512) bytes, so the largest `PUT_DATA` payload is
  **min(MTU − 3, 512) − 5** (507 at MTU 517, not MTU − 8 = 509; Android drops longer
  notifications). A write longer than 512 bytes answers `ERROR bad_length`. §3.5 says the
  same.
- **Service Changed:** NimBLE's GATT client is compiled in (`CONFIG_BT_NIMBLE_GATT_CLIENT`,
  with the central role Kconfig needs) so the T5 confirms the indication Android sends a
  bonded peer on connect; without it the ATT error broke `requestMtu` and discovery on the
  T-Watch (SDarioosh/barvis-sound#49).
- **`PUT_END`:** size ≠ `total` → `ERROR bad_state` (arg = bytes received); CRC mismatch →
  `bad_crc` (arg = the CRC the T5 computed); a feed or detail that doesn't parse (wrong
  `v`, broken JSON, no `ref`) → `bad_state` (arg = blob_id); a flash write error →
  `io_error` (arg = blob_id). The stored feed is untouched in every error case.
- **`OB_LIST`:** `max` is capped at 16; `max` 0 answers only `OB_END` with
  `remaining` = the entries after `after_id`.
- **`OB_ACK.result`:** 0 = done; anything else = rejected for good ("couldn't sync").
  **`OB_ACKED.result`:** 0 = removed now, 1 = it wasn't queued (already acked). Both are
  success, as with the watch's `ACKED`.
- **Advertising:** whenever no relay is connected (the relay reconnects every 15 min),
  1000–1250 ms, 152.5–211.25 ms while the pairing window is open. Advertising data: flags
  and the 128-bit service UUID.
- **Scan response:** it holds 31 bytes. The PC relay's service data (AD `0x21`: the
  16-byte UUID and one flags byte, bit0 = outbox not empty) takes 19 bytes and the name
  `odysseus-pda` 14, so they don't fit together. The T5 sends the service data **only while
  the outbox has entries**, with the shortened name (AD `0x08`) `odysseus-p`; otherwise the
  complete name. No service data means nothing is waiting. The GAP device name is always
  `odysseus-pda`.
- **Idle disconnect:** the T5 drops a relay after **60 s with neither a `ctrl` write nor a
  `status` read**. Both count as activity, so the PC relay's `status` read every 30 s and
  android-relay's `TIME` every 20 s keep a link up while `status` bit4 (user active: a
  touch or button in the last 60 s) asks them to stay. A relay that stays connected must
  do one of the two at least every 60 s (every 30 s leaves margin). After `BYE_OK` the T5
  disconnects after ~1 s if the relay hasn't.
- **`status`** is notified (at most once a second) when any of its bytes changes: the
  outbox count, the feed version, the flags.
- **Actions** are written exactly as in §4.2, for example
  `{"t":"item_status","id":42,"status":"done"}`,
  `{"t":"cal_add","title":"Call","start":"2026-10-21T09:00:00+02:00","end":"2026-10-21T09:30:00+02:00","from_item":12}`
  (timed: local time with the display offset) or
  `{"t":"cal_add","title":"Reminder","start":"2026-10-23","all_day":true}` (no `end`).
  Undo before the relay has listed an action deletes it; after that, the T5 queues the
  reverse `item_status` (`open`).

## 4. Feed, actions and details

### 4.1 Feed (`kind` 1)

The phone builds it from these sources:

| Feed section | Source |
|---|---|
| `briefing` | `GET /v1/digests/{yesterday}` and `{today}` (404 → leave out) |
| `conversations` | `GET /v1/conversations?since=<now − 48 h>&limit=20`: id, title, start, people, summary, key points (cut to fit) |
| `actions` | `GET /v1/action-items?status=open`: id, text, owner, `due_date`, conversation id |
| `calendar` | Odysseus calendar REST, today to +7 days: id, title, start, end, location, all-day |
| `todos` | Odysseus notes (§4.4): the owner's checklists, pinned first, with their items |
| `insight` | Odysseus `GET /api/codex/insight/today` (scope `insight:read`): the hub's Today insight, a headline and the to-dos and events to look at today (404 → leave out) |
| `weights` | Odysseus `GET /api/codex/insight/weights` (same scope): the hub's learned rule weights per checklist for the T5's rule-based Today plan (§5); a failure keeps the last good table, never fetched → leave out |

```json
{"v": 1, "version": 1729411200, "generated": "2026-10-20T07:00:00Z", "tz": "Europe/Berlin",
 "briefing": {"date": "2026-10-19", "text": "…", "highlights": ["…"]},
 "conversations": [{"id": "c_…", "title": "…", "at": "…", "people": ["…"],
                    "summary": "…", "points": ["…"]}],
 "actions": [{"id": 42, "text": "…", "owner": "…", "due": "2026-10-23", "conv": "c_…"}],
 "calendar": [{"id": "…", "title": "…", "start": "…", "end": "…", "where": "…", "all_day": false}],
 "todos": [{"id": "3f2c…", "title": "Groceries", "pinned": true, "due": null,
            "items": [{"i": 0, "text": "Milk", "done": false}, {"i": 2, "text": "Eggs", "done": false}]}],
 "insight": {"date": "2026-10-20", "headline": "…",
             "focus": [{"ref": "todo:3f2c…:0", "why": "…"}, {"ref": "event:…", "why": "…"}],
             "coming_up": [{"ref": "event:…", "why": "…"}], "also": []},
 "weights": {"version": 1, "date": "2026-10-20", "weights": {"list:3f2c…": 2, "list:9a1e…": -1}},
 "templates": ["Call", "Meeting", "Reminder"]}
```

- **Optional `templates`:** up to 12 event titles for the T5's "+" picker (§5). The owner
  sets them in the phone app, or with the PC relay's `--templates`. The firmware must not
  require the key and falls back to built-in titles. Readers ignore unknown keys.
- The Android relay builds the feed, actions and details exactly as the PC relay does
  (§3.5, the next bullet and §4.3). Calendar times from Odysseus are sent in UTC with `Z`
  like the conversations' `at`, and all-day events as dates.

- Keep it under 32 KB. If it is too big, the phone cuts summaries first, then drops the oldest
  conversations.
- How the PC relay builds it (v1):
  - `briefing`: today's digest if there is one, else yesterday's. `text` is the headline, a
    blank line, then the summary. At most 5 `highlights`.
  - `conversations`: only `status: summarized` (no pending, failed or discarded ones),
    newest first, `at` in UTC with `Z`.
  - `actions`: the soonest `due` first, undated ones last. A missing `owner` or `due` is
    `null`.
  - `calendar`: sorted by `start`.
  - `todos`: see §4.4.
  - `insight` (added 2026-10-21): the hub's Today insight, from
    `GET /api/codex/insight/today` with the calendar's URL and token (scope
    `insight:read`). The hub answers `{"date", "generated", "headline", "focus",
    "coming_up", "also"}`, each section a list of `{"ref", "why", "title"}`, or 404
    `{"error": "no insight yet"}`.
    - Only `date`, `headline`, and `ref` and `why` per item are copied (no `title`, no
      `generated`); other keys are ignored. The keys come after `todos`.
    - Caps: `headline` ≤ 200 characters and `why` ≤ 80 (cut like the other fields), at
      most 5 items per section, the first that resolve. A `why` that is missing or not a
      string is `""`. A section that is missing or not a list is `[]`.
    - **Refs must resolve in this feed:** `todo:<note id>:<i>` only when `todos` has that
      note with an item whose `i` is that number (written as the feed writes it, so `01`
      or `x` never match), `event:<id>` only when `calendar` has an entry with that `id`
      (the hub's event `uid`, an occurrence's `<uid>::<occurrence>` included). Anything
      else is dropped silently. Without Odysseus todos or calendar, no ref of that kind
      resolves.
    - **Left out** (no `insight` key; the build doesn't fail): no Odysseus configured, no
      insight yet (404), a hub without the route (FastAPI's 404 "Not Found", or 405), a
      token without `insight:read` (403), any other final 4xx, and an answer that isn't a
      JSON object or has no `YYYY-MM-DD` `date` or no string `headline`. The relay logs
      why, and the phone's status line says it (for example "insight: token lacks
      insight:read"). Network errors, 5xx, 408/429 and 401 fail the build, like a failing
      calendar.
  - `weights` (added 2026-10-22): the hub's learned rule weights (§5 "Rule order and
    weights"), from `GET /api/codex/insight/weights` with the insight's URL and token
    (scope `insight:read`). The hub answers `{"version": 1, "date": "YYYY-MM-DD",
    "weights": {"list:<note id>": <int>}}`, `"weights": {}` when it has none.
    - The feed's key is `{"version": 1, "date", "weights": {…}}`, after `insight`. Only
      `list:<note id>` keys (the id 1 to 150 UTF-8 bytes) with integer values (not
      booleans, not `1.0`) are kept, clamped to [-6, 6], at most 32 in the hub's order
      (largest first); everything else is dropped. An answer with a `version` other than 1,
      or whose `weights` isn't an object, is malformed. A `date` that isn't `YYYY-MM-DD` is
      left out.
    - **Last good table:** any failure (an older hub's 404, 405, 403, network errors, 5xx,
      a malformed answer) keeps the last good table, so a weights failure never fails the
      build. The PC relay keeps it in its state file (`pda.json`, so it survives restarts);
      the Android relay in its private preferences. A table that was never fetched leaves
      the key out; so does a relay without Odysseus. An empty table (`{}`) replaces the
      last one: the hub has no weights any more.
    - The T5 matches the keys to `todos` by note id; a key for a note not in the feed is
      ignored there.
  - `templates` (optional): the owner's event titles for the "+" picker (§5), e.g.
    `["Call", "Meeting", "Reminder"]`. At most 12 of up to 40 characters. The phone takes
    them from its settings, the PC relay from `--templates "Call,Meeting,Reminder"`. The key
    is left out when there are none, and the T5 then uses built-in defaults.
  - Field caps in characters: title 120, summary 600, 5 points of 160, action text 200.
  - The size limit is 32,768 bytes of compact UTF-8 JSON. Over it, each summary is cut to
    240 characters, then the oldest conversations are dropped. If it is still too big
    (hundreds of open items), done checklist items go, then the whole `insight`, then the
    `weights`, then the last action items, then the last unpinned checklists, then the last calendar entries. An
    `insight` that is kept loses the refs to done items that were dropped, so every ref
    still resolves.
  - A source that fails fails the whole build, so nothing is pushed and the T5 keeps its
    feed. These don't count as failures: a digest 404, no Odysseus configured (no `calendar`
    entries, no `todos`, no `insight`, no `weights`), a hub that can't list notes for this
    token (§4.4), an insight the hub can't give (see `insight` above), and any weights
    failure (see `weights`).
  - **Push rule:** the content hash is SHA-256 over the feed without `version` and
    `generated`. The relay pushes when this hash differs from the last one it pushed to
    this T5, or when `status.feed_version` isn't the version it pushed (the T5 was wiped,
    or another relay pushed). `version` = max(now, last pushed + 1, T5's + 1), so it only
    grows.
- `version` is the build time in epoch seconds. It goes in `status.feed_version` so the
  phone can see what the T5 holds.
- Transcript links (`/view/<id>?sig=…`) are left out of the feed. The T5 can't open them,
  and there's no reason to carry signed links on the device. The detail screen can show a QR
  code to open the transcript on the phone instead (§11 Q4).

### 4.2 Actions (outbox, T5 → phone → server)

Each action is one JSON object of 400 bytes at most, with `"t"` saying the type:

| `t` | Fields | Phone calls |
|---|---|---|
| `item_status` | `id`, `status` (`done`/`dismissed`/`open`) | `PATCH /v1/action-items/{id}` (exists) |
| `cal_add` | `title`, `start`, `end` or `all_day`, optional `from_item` | `POST /api/codex/calendar/events` (§6) |
| `cal_delete` | `id` | `DELETE /api/codex/calendar/events/{id}` (§6; v2 on the T5) |
| `fetch` | `ref`: `conv:<id>`, `transcript:<id>`, or `transcript:<id>@<from>-<to>` | `GET /v1/conversations/{id}` or `/transcript`, then `PUT` a detail blob |
| `digest_refresh` | `date` | `POST /v1/digests/{date}` (exists), then a new feed |
| `todo_toggle` | `note`, `i`, `text`, `done` | Odysseus `POST /api/codex/todos` `toggle_item` (§4.4) |

- The T5 shows a queued action at once (the item is struck through, the event is shown in
  grey as "pending"). The T5 doesn't change its own feed. The next feed from the phone
  confirms the change, and an `OB_ACK` with result 1 undoes it.
- Actions are idempotent where the server allows it. `item_status` is. For `cal_add`, the
  phone dedupes on `action_id`: it keeps the event id it created for each recent
  `action_id`, so a re-sent action after a lost ack doesn't create a second event.

### 4.3 Details (`kind` 2)

`{"v": 1, "ref": "transcript:c_…", "title": "…", "segments": [{"o": 12.5, "s": "Anna", "me": false, "x": "…"}]}`.
Long transcripts are paged (`@from-to`, e.g. 10 minutes per page). The T5 asks for the next
page when the reader reaches the end.

The PC relay's formats (v1):

- **`transcript:<id>`**: `{"v": 1, "ref", "title", "from", "to", "next", "segments": […]}`.
  - Without a range it sends the first page, 0–600 s.
  - A page holds the segments with `from ≤ o < to`.
  - `next` is the ref of the following page (`transcript:<id>@600-1200`), or `null` at the
    end.
- **`conv:<id>`**: `{"v": 1, "ref", "title", "at", "people", "summary", "points",
  "actions": [{"id", "text", "status", "due"}]}`.

- The firmware reads details as the PC relay's §4.3 defines them: `conv:` details with
  `at`, `people`, `summary`, `points`, `actions`; transcript pages with `from`, `to`,
  `segments` and `next` (the next page's ref, `null` on the last). The first page it asks
  for is `transcript:<id>@0-600`; "Next part" asks for `next`.
- It also reads the feed's `templates` (the "+" picker; Call, Meeting, Reminder when there
  are none). An empty string in an optional field (`"owner": ""`) reads as missing.

### 4.4 Odysseus checklists (`todos`, added 2026-10-20)

The T5's Tasks tab shows the owner's own to-dos: the checklists in Odysseus notes, the
same ones the Odysseus website shows. The action items the server suggests from
conversations (`actions`) move into the conversation's detail screen (§5).

- **Token:** the same Odysseus token as the calendar, with **Todos** (`todos:read`) and
  **Todos write** (`todos:write`) added. The Today insight (§4.1) needs `insight:read` on
  the same token; without it the feed has no `insight`, and the rest syncs.
- **Reading (hub change, SDarioosh/Barvis):** `GET /api/codex/todos?format=json` (scope
  `todos:read`). Without `format=json` the route keeps answering with the chat-model text it
  has today. Answer:
  `{"notes": [{"id", "title", "type": "checklist"|"note", "label", "pinned", "due_date",
  "updated_at", "items": [{"text", "done"}], "content"}]}`: the token owner's notes that
  aren't archived, without calendar-reminder notes, pinned first, then the most recently
  updated (the order the website uses). `items` is `[]` for a plain note, `content` is
  `null` for a checklist. Notes of type `checklist`, `todo` (what the hub's Module Action
  Items task files) and `goal` all count as checklists (SDarioosh/Barvis#36). A hub without this option answers text, and a token without
  `todos:read` gets 403: both mean "no todos" (the feed leaves `todos` out and the phone
  says why), not a failed feed.
- **Feed `todos`:** only `type: checklist` notes with at least one item, in the hub's order,
  at most 20 notes and 40 items per note. Each item keeps its index `i` in the note's item
  list (the hub toggles by index). `title` ≤ 120 characters (an empty title is `null`), item
  `text` ≤ 160, `due` = `due_date` as `YYYY-MM-DD` or `null`. Done items are kept (shown
  struck through) unless the feed is too big (§4.1). The `type: checklist` filter is right
  for `todo` and `goal` notes too: the hub reports them as `checklist` (both relays test it).
- **`todo_toggle`:** `{"t":"todo_toggle","note":"<note id>","i":<index>,"text":"<the item's
  first 80 characters>","done":true|false}`. The relay:
  1. reads the note's current items (`GET /api/codex/todos?format=json`, once per sync);
  2. uses item `i` if its text starts with `text` (after trimming and collapsing spaces);
     otherwise the single item in that note whose text starts with `text`; if there is
     none or more than one, or the note is gone, it acks with result 1 ("couldn't sync":
     the item changed on the website since the last feed);
  3. if the item is already `done`, acks with result 0 without a call; otherwise
     `POST /api/codex/todos` `{"action": "toggle_item", "note_id", "index", "done"}`
     (scope `todos:write`). The hub answers HTTP 200 with `{"exit_code": 0, …}` on
     success and `{"exit_code": 1, "error"}` on failure: `exit_code` 1 is result 1.
  It is idempotent: `done` is explicit, never a flip. No Odysseus configured: the action
  stays queued, like `cal_add`.
- **Details settled by the PC relay** (the Android relay does the same):
  - **Matching:** `text` and the item texts are trimmed and their runs of whitespace
    collapsed to one space. Then a case-sensitive prefix match is made. The note is looked
    up by `id` among all the notes the hub returned.
  - **No call needed:** an item already in the wanted state (`done` *or* open) is result 0
    without a call.
  - **The hub answers:**
    - an `exit_code` other than 0 is result 1;
    - an answer without an integer `exit_code` is temporary (no ack);
    - a 403 on the toggle (the token lacks `todos:write`) leaves the action queued and
      doesn't stop the sync.
  - **Hub can't list notes:** when the hub can't list notes for the token (a text answer
    or a 403), `todo_toggle` stays queued too and the feed leaves `todos` out.
  - **Once per sync:** the notes are read once per sync, before the first `todo_toggle`.
    A successful toggle updates that copy, so a second action on the same item in the
    same sync (a tap, then the reverse) is matched against the new state. The feed reads
    the notes again afterwards.
  - **Checks:** `i` is an integer from 0 to 9999. `note` is 1–64 characters, `text` 1–80
    (neither may be only whitespace), and `done` is a JSON boolean.
  - **Feed items:**
    - only the first 40 items of a note, by index; an item that isn't a JSON object is
      skipped, and the others keep their `i`;
    - `due` is the first 10 characters of `due_date` if they are `YYYY-MM-DD`, else
      `null`.
  - **Trimming order** (§4.1):
    1. Done items go one at a time, from the last checklist backwards. A checklist left
       without items goes with them.
    2. Unpinned checklists go from the end.
    3. After the calendar entries, the pinned checklists go from the end, as a last
       resort.
- **On the T5:** a tap on an item queues `todo_toggle` with the opposite of its `done` and
  shows it at once (struck through, or not); a second tap before the relay has listed the
  action deletes it, after that it queues the reverse. Nothing else is editable on the T5
  (no adding or renaming items, no plain notes).
- **Finished lists leave on their own (hub, added 2026-10-21):** Odysseus archives an
  unpinned checklist once every item is done and it hasn't changed for 10 minutes
  (`FORK_CHECKLIST_ARCHIVE_MINUTES`, hub `src/checklist_auto_archive.py`). Archived notes
  aren't in `GET /api/codex/todos`, so the list leaves the Tasks tab at the next sync; the
  T5 and relays need no change. Untick an item within the 10 minutes to keep the list;
  later, unarchive it on the website (it is archived again 10 minutes after that unless
  it has an open item). Pinned lists stay, finished or empty.

## 5. Screens

Portrait, 540 × 960 (§1): a two-line header (title and clock in SemiBold 20 pt; the date
on the left, a battery icon with its percentage on the right, ↑n for actions waiting, and a
note only when worth a look: "synced 3 h ago" after an hour without a sync, "no feed"), the content, a bottom row for the screen's buttons and the pager (▲ n/m ▼), and a
tab bar of six plain labels (Today, Brief, Cal, Tasks, Talks, Status; the active one
SemiBold with a bar under the top rule). Titles of items are SemiBold, their details
Regular: weight, not grey, sets them apart (grey doesn't show on the panel). Tick boxes are
round: a ring, filled black with a white tick when done. A tap that opens another screen
draws a black frame around what was tapped with a fast black-and-white (DU) update first,
so the tap shows before the slower full refresh. Lists use the full width
with rows of two or three lines; the new-event form stacks the date, the time with ±15 min,
a 3 × 4 grid of hour slots and the durations.

| Screen | Content | Interactions |
|---|---|---|
| **Today** (home) | the header (clock, date, battery, sync age); INSIGHT, DO FIRST, COMING UP, ALSO WORTH A LOOK (below); "Digest ready on Brief ›" in the bottom row | tap a to-do: done / not done (`todo_toggle`, as on Tasks); tap an event: its details; "+n more ›": Tasks or Cal; the digest line: Brief |
| **Briefing** | yesterday's digest, highlights | `digest_refresh` |
| **Calendar** | agenda, today to +7 days | tap an event for details; "+" opens the template picker |
| **Tasks** | the owner's Odysseus checklists (§4.4): a heading per note (pinned first, with its due date), its items below | tap an item: done / not done (`todo_toggle`); without Odysseus: "No checklists. Set up Odysseus in the phone app" |
| **Conversations** | the last 48 h, title, people, time | tap: summary and key points, then that conversation's suggested action items (`actions` with this `conv`); on those: tap done, long press dismiss, "→ calendar" when `due` is set; "transcript" → `fetch` |
| **Status** | PHONE (connected or last seen, phones paired, changes waiting), BATTERY (icon, %, charging), DETAILS (feed version and build time, clock and zone, screen rotation, firmware, BLE name, battery mV); the buttons | |

- **Today as built (2026-10-21):**
  - **Sections:** INSIGHT (the hub's headline, wrapped, at most 3 lines), DO FIRST, COMING
    UP, ALSO WORTH A LOOK. Each item takes two lines: the to-do text or event title
    (SemiBold, cut to fit), then why it is there, with a hairline between items. A left
    column holds a to-do's round tick box, or an event's start time ("all day") with its
    weekday under it when it isn't today; events end in "›" and their second line is the
    hub's `why` and the place ("until 15:30" when there is neither).
    Empty sections are left out. The sections flow over pages (the pager, swipe up/down); a
    section that goes on to the next page repeats its heading with "(cont.)".
  - **With the hub's insight** (feed `insight`, §4.1, dated today): its items in the hub's
    order, under the hub's `why`; events also show their time. Items whose ref names nothing
    in the feed, done items, events that are over and repeats are dropped. Today's timed
    events still to come all end DO FIRST in time order, wherever the hub put them (with
    its `why` when it named them). DO FIRST is for today only: an event on a later day, or
    an item of a checklist due on a later day, that the hub put there moves to the end of
    COMING UP (with its time or "due Wed 28 Oct").
  - **A to-do shows once:** the hub's `why` is left out when it only restates the to-do (it
    starts with the same word, or holds at least 60 % of the to-do's words of 3+ letters);
    the second line then falls back to the list's title, but only for a list of more than
    one item (a one-item note's title is the to-do again, often reworded), and never when
    the title is the same text (any case, or the start of it cut with "…").
  - **Without one** (no key, or `date` isn't today), the same sections from rules, and
    INSIGHT says "All caught up!" (with a small filled tick). The hub's weights (feed
    `weights`, below) reorder items within each group, never across groups; without
    weights every w is 0 and the order is the one the T5 had before them:
    - DO FIRST: open items of overdue checklists ("overdue since 2026-10-19") and of those
      due today ("due today") by (-w, due date, feed order), then today's events that
      aren't over (all-day first, "today, all day"; then "today 14:00");
    - COMING UP: events and items of checklists due within the next 7 days, by (-w, date,
      time) with events at w 0 ("Wed 21 Oct 09:00", "due Fri 23 Oct");
    - ALSO WORTH A LOOK: items of pinned checklists not listed above ("pinned") by (-w,
      feed order), then of undated ones ("no date") by (-w, feed order); unpinned
      checklists due later than that aren't on Today.
  - **Rule order and weights (published 2026-10-22; the hub's candidate order for its
    insight follows the same rules):**

    | tier | items | within the tier | on the T5 |
    |---|---|---|---|
    | 0 | overdue and due today | higher weight first, then date, time, input order | DO FIRST (to-dos, then today's events) |
    | 1 | due in the next 7 days | higher weight first, then date, time | COMING UP |
    | 2 | items of pinned notes (undated or later) | higher weight first, then input order | ALSO WORTH A LOOK, first |
    | 3 | undated | higher weight first, then input order | ALSO WORTH A LOOK, then |
    | 4 | due later than a week | higher weight first, then date, time | not on Today |

    A weight never moves an item to another tier, so overdue and due-today items always
    come first. The weight table (feed `weights`, §4.1):

    | key | applies to | range |
    |---|---|---|
    | `list:<note id>` | the to-do items of that checklist | integer, -6 to 6 (0 when absent) |

    At most 32 entries; the T5 has only this key (the hub folds its per-note-type weights
    into it, since every T5 to-do is a checklist item), events always weigh 0. The hub
    learns each weight from the owner's outcomes on its Today page over the last 28 days
    (done, "↑ first", "not today", moved, ignored), and keeps it at 0 until a list has been
    shown often enough ([how the hub learns them](https://github.com/SDarioosh/Barvis/blob/barvis/docs/fork/today-insight.md#weights)).
    The T5 uses the weights only in the rule-based plan: with an insight for today, the
    hub's order stands. Integers only, no floats on the T5. A fictional day with known
    weights and its expected order is `firmware/pda/test/fixtures/insight_eval_day.json`,
    shared with the hub's insight eval.
  - **Left out:** done items, events that are over, and shopping lists: a checklist titled
    Shopping, Groceries or Grocery (any case, with or without " list") unless it has a due
    date. The hub's insight may still name one of their items.
  - **Caps:** 5 items per section from the rules, the rest as "+n more ›" on the heading
    (to Cal when most of the rest are events, else Tasks). With the insight, DO FIRST holds
    the hub's 5 plus today's events, up to 8.
  - **Nothing at all** (no item in any section): a big filled tick and "All caught up!"
    (SemiBold 32 pt) in the middle of the page, "Nothing due and nothing on the calendar
    for the next 7 days." under it; INSIGHT is left out unless the hub sent a headline.
  - **The digest** is on Brief only; the bottom row of Today says "Digest ready on Brief ›"
    when the feed has a briefing.
  - Code: `main/today_plan.c` (the plan, pure, `test/test_today_plan.c`; the weights are
    parsed by `main/feed.c`, `test/test_feed.c`), `main/ui_today.c`
    (the drawing), `main/ui_chrome.c` (header, tabs, headings, the Asleep screen); both
    rendered on the host by `test/test_ui_today.c`.
- **Asleep screen** (BOOT held 1 s, §2 "Sleep"): "Asleep" (SemiBold 32 pt), "since Tue 20
  Oct 21:40", "Press BOOT to wake"; NEXT UP: the next event that hasn't ended, its title
  and its full date and time (every line names its date, so nothing on it goes stale
  overnight), or "Nothing on the calendar this week"; at the foot the battery at sleep and
  "synced HH:MM", the only line a sync wake redraws (`ui_chrome_asleep_synced_area()`).
- **Adding an event without a keyboard:**
  - From a suggested action item (in its conversation): "→ calendar" fills in the title from the item and the date from
    `due_date`. The user picks a time from a grid (morning slots, then +/− 15 min) and a
    duration (15/30/60 min, all day).
  - From scratch: pick the date and time the same way, then a title from a short list of
    templates the owner configures in the phone app (Call, Meeting, Reminder, …).
  - A small on-screen keyboard is v2.
- **Refresh policy:**
  - partial refresh for list taps and the pending/strike-through state
  - full refresh when changing screens, every 10 partial refreshes, and on the button
- **Time:** shown in the feed's `tz`. `TIME` carries the phone's UTC offset, so the clock is
  right even before the first feed.
  - **As built:** the T5 shows every time in the offset of the last `TIME`. It has no time
    zone database, so it doesn't use the IANA name (shown on Status only). An event after a
    daylight-saving change within the week is shown, and a `cal_add` for such a day is
    made, one hour off until a `TIME` after the change. A fix for v2: the feed carries a
    POSIX TZ string (`"tz_posix": "CET-1CEST,M3.5.0,M10.5.0/3"`) that the T5 evaluates.
- **Swipes as built:** on a tab screen left/right = next/previous tab, up/down = page; in a
  detail screen right = back to the list, left = next page, then the next transcript part or
  the next item, nothing at the end (`ui_nav.c`, host-tested).
- **Refresh policy as built:** a new screen (tab, menu, detail) and a change on the same
  screen (a pending mark, a page) are partial (GL16, no flash) updates (owner's choice,
  2026-10-21: no flash on every tab change; ghosting is left to the clean refresh);
  turning the screen 180° is a full (GC16) one; the minute clock a partial that
  doesn't count; after 10 counted updates, and on the side button or "Clean refresh", a
  clean refresh. A new feed is a partial update; the phone's link only redraws Status; an
  update whose drawing matches the panel is skipped and not counted, so a sync that brings
  the same content leaves the screen alone.

## 6. Phone app and servers

- **android-relay:**
  - a second BLE job (`PdaSync`) next to `DrainSession`, sharing `Scanner`,
    `GattClient` and `Bonding`
  - a feed builder
  - an Odysseus REST client with its own encrypted token (`TokenCipher`)
  - a settings section: Odysseus URL and token, event templates, sync interval
- **Athena server:** no change in v1. Optional later: `GET /v1/glance` that returns
  the four Athena sections in one call, so the phone makes 1 request instead of 4.
- **Odysseus (hub repo):** checked in `SDarioosh/Barvis` on 2026-10-20 (branch `barvis`,
  commit `5cc32c5`; read only, no code run). File:line references are to that commit.
  1. **Endpoints.** Use the token-aware `/api/codex/calendar/…` routes. They wrap the UI's
     `/api/calendar/…` handlers. The plain `/api/calendar/*` routes reject every API token
     with 403 "API tokens must use a scope-aware API route" (`src/auth_helpers.py:153-154`).
     - **List:** `GET /api/codex/calendar/events?start=<ISO>&end=<ISO>[&calendar=<id or name>]`
       (`routes/codex_routes.py:431-436` → `routes/calendar_routes.py:1276-1340`). It
       returns `{"events": [...], "truncated"?: true}`, sorted by start, with recurring
       events expanded into occurrences whose `uid` is `<base>::<occurrence>`. Each event
       has `uid`, `summary`, `dtstart`, `dtend`, `all_day`, `is_utc`, `location`,
       `description`, `calendar`, `calendar_href`, … (`_event_to_dict`,
       `calendar_routes.py:752-799`). All-day events are `YYYY-MM-DD` with an exclusive
       end. Timed events end in `Z` when stored as UTC; legacy rows are naive server-local
       time. An unparseable range returns `{"events": []}`, not an error.
     - **Create:** `POST /api/codex/calendar/events` with a JSON body matching `EventCreate`
       (`codex_routes.py:438-450`, model `calendar_routes.py:200-210`): `summary`
       (required), `dtstart` (required, ISO), `dtend`, `all_day`, `description`,
       `location`, `calendar_href` (calendar id), `rrule`, `color`, `reminder_minutes`.
       An ISO time with an offset is stored as UTC (`_parse_dt_pair`, `:469-490`); a
       naive one is taken as server-local, so the phone always sends an offset. Without
       `dtend` an event lasts 1 h, or 1 day if all-day. It answers
       `{"ok": true, "uid": …, "event": {…}, "reminder": …}`, or 400 for an invalid
       body. **Create is not idempotent**: every POST makes a new `uid`
       (`calendar_routes.py:1361`), so the phone's dedupe by `action_id` (§4.2) is needed.
     - **Delete:** `DELETE /api/codex/calendar/events/{uid}` (`codex_routes.py:499-504` →
       `calendar_routes.py:1487-1530`). It returns `{"ok": true}`, and 404 if the event is
       gone. It deletes the whole series. The codex wrapper doesn't pass `scope`, so a
       single occurrence can't be deleted through a token.
     - There is **no update** route and **no list-calendars** route under `/api/codex`.
       `GET /api/calendar/calendars` (`calendar_routes.py:1253`) is cookie-only.
  2. **Scoped token: yes.** `calendar:read` and `calendar:write` are allowed scopes
     (`routes/api_token_routes.py:14-30`), and `calendar:write` adds `calendar:read`
     (`:68`). An admin creates the token in Settings → API tokens (checkboxes at
     `static/js/settings.js:4842`) or with `POST /api/tokens` and
     `scopes=calendar:read,calendar:write` (`api_token_routes.py:115-152`). The token is
     `ody_…`, shown once, and owned by the admin who made it. The codex calendar routes
     check the scope and act as that owner (`codex_routes.py:85-96`, `_as_owner` `:59-82`).
     No other surface is reachable with it.
  3. **CalDAV write-back: yes, but only into a CalDAV calendar.** The codex create calls the
     same handler as the UI. That handler pushes to CalDAV after the commit only when the
     target calendar's `source == "caldav"` (`calendar_routes.py:1388,1396-1397`). A failed
     push is logged and marked `caldav_sync_pending` (`:151-178`). The answer is still
     `ok`, and the next `/sync` retries it (`src/caldav_sync.py:566`). Without
     `calendar_href`, the event goes to the owner's *first* calendar row, which is
     unordered and may be a local calendar (`_ensure_default_calendar`, `:269-281`). So the
     phone sends the CalDAV calendar's id as `calendar_href`; the owner sets it in the
     relay settings, because a token can't list calendars. Delete pushes too, and records a
     tombstone (`:1515-1522`).
  4. **How it's reached.** Only the header `Authorization: Bearer ody_…`
     (`app.py:419-466`). There is **no `?access_token=`** form for these tokens, unlike our
     MCP server. The app listens on `APP_BIND`:`APP_PORT` (default `127.0.0.1:7011`,
     `app.py:1439-1442`). Production is port 7000 with `APP_BIND=0.0.0.0`
     ([hub deploy.md:203](https://github.com/SDarioosh/Barvis/blob/barvis/docs/fork/deploy.md)),
     so the phone uses `http://<server's Tailscale name or 100.x address>:7000`, the same
     host as Athena on another port. If the owner turned auth off
     (`AUTH_ENABLED=false`), the middleware isn't installed (`app.py:266`), and the routes
     run as `ODYSSEUS_FALLBACK_OWNER` whatever token is sent.

  Hub changes worth proposing (not needed for v1; they go in a hub PR, not here, per
  AGENTS.md "Where work belongs"):
  - `GET /api/codex/calendar/calendars` (`calendar:read`), so the phone can offer the
    CalDAV calendar in a picker instead of the owner pasting its id.
  - An idempotency key on create (for example `Idempotency-Key`, or a client `uid`), so a
    retry after a lost answer can't create a second event even when the phone loses its
    dedupe table.
  - Optional: `scope=occurrence` on the codex delete, and a codex `PUT` for updates (v2).

## 7. Privacy and security

- The data stays on the owner's devices: server → Tailscale → phone → BLE. Nothing goes to
  the cloud, and there is no telemetry.
- **The T5 holds no tokens.** It can only queue actions. The phone checks each action's
  type and fields before it calls a server. An action that doesn't parse is acked with result
  1, and the phone never forwards raw JSON.
- **A lost T5 exposes the cached feed:** summaries of the last 48 h, a week of calendar and
  open action items. Mitigations:
  - keep the feed in internal flash, never on the SD card
  - enable ESP32 flash encryption in milestone 5
  - the Status screen can wipe the cache
  - the phone app can "forget" the T5 (delete the bond), and then it never syncs again
  - optional: blank the screen to a clock after N hours without a phone (§11 Q5)
- The T5 has no audio and no mic. Nothing is recorded.

## 8. Milestones and acceptance tests

| # | Milestone | Done when |
|---|---|---|
| 0 | Bring-up | Find which COM port is the T5. epdiy full and partial refresh, touch coordinates, RTC, battery % and button input work on the board. The refresh times are measured. |
| 1 | Static screens | The firmware draws all screens of §5 from a `feed.json` built from `seed_demo.py` data and loaded over the USB console. Taps navigate. |
| 2 | BLE down | The PC test relay (`tools/`, bleak) pairs, sends `TIME` and `PUT`s a feed. The screen updates, the feed survives a reboot, and a bad CRC is rejected. Host tests cover the encode/decode code. |
| 3 | BLE up | Outbox `item_status` from the T5, through the PC relay, to the sandbox server's `PATCH`. A lost ack retries without duplicates. |
| 4 | Phone | android-relay syncs the T5 and the watch on one phone. The feed includes the Odysseus calendar. `cal_add` from an action item appears in Odysseus and in CalDAV. |
| 5 | Power and security | Deep sleep with a wake-up advertising window. The battery life is measured. Flash encryption is on. |

**Firmware status (2026-10-20):** milestones 0–3 are built in `firmware/pda/` and checked on
the owner's board with fictional data, through the USB console and a PC BLE test script
(`firmware/pda/tools/pda_ble_test.py`): bring-up and refresh times (§1), all §5 screens,
tap navigation, a feed loaded over USB and over BLE, reboot persistence, a bad CRC refused,
the outbox drained and acked over BLE, a lost ack listed again without a duplicate. Still
open: the PC relay's `pda` subcommand and the Android `PdaSync` against the real T5, and the
server `PATCH` end to end (milestone 3's last step). The full list is in
`firmware/pda/README.md`.

The PC relay (`firmware/recorder/tools/barvis_relay`) gets a `pda` subcommand in milestone 2.
That way the protocol can be tested without the phone, and in the sandbox with fictional
data only. **Built 2026-10-20:** `ble_relay.py pda` (`--list`, `--once`, stay-connected by
default). It is tested against a fake T5, a fake server and the real API with seeded
fictional data. It hasn't run against the T5 yet.

## 9. Risks

- **Watch and PDA on one phone:** two bonded peripherals and two jobs on the Android BLE
  stack. Keep them one at a time in v1, and test both together early (milestone 4).
- **Odysseus calendar REST** exists with a scoped token (§6). Events written into a
  non-CalDAV calendar are not written back, so the relay needs the CalDAV calendar id.
- **The epdiy setup on this board** (power IC, pinout) may need work if LilyGO's examples
  use a different driver fork. That is found out in milestone 0.
- **Ghosting and refresh time** may make lists feel slow. Fewer partial refreshes and a
  whole-screen redraw per page are the fallback.
- **Fonts:** names and summaries in non-Latin scripts need larger fonts in flash. v1 is
  Latin-1 (§11 Q6).

## 10. Not in v1

- Wi-Fi on the T5, search with an on-screen keyboard, creating notes, editing events, and
  meeting invites (accept/decline).
- Showing the T-Watch's recording state on the T5. This is easy later: the phone adds a
  `recorder` section to the feed.

## 11. Open questions for the owner

Defaults used for v1 until the owner answers (2026-10-20): Q1 a separate ESP-IDF project
`firmware/pda/` that builds shared sources from `firmware/recorder/main/` (no recorder
behaviour changes); Q2 48 h / 7 days; Q3 BLE only; Q4 no QR yet; Q5 off; Q6 Latin-1.


1. **Firmware layout:** a second app (`firmware/pda/`) that shares components with
   `firmware/recorder/`, or one project with a board-and-role switch?
2. **Feed window:** are 48 h of conversations and 7 days of calendar right?
3. **Wi-Fi at home** as a later direct path (LAN only, its own scoped token), or BLE only
   for good?
4. **Transcript QR code** on the detail screen to open the signed `/view` link on the
   phone: wanted?
5. **Auto-blank:** hide the cached content after N hours without a phone?
6. **Languages:** do summaries and names need anything beyond Latin-1?
