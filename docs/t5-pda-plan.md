# Plan: LilyGO T5 E-Paper Pro as an odysseus-sound PDA

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
- **The phone builds the feed.** It calls the odysseus-sound HTTP API and the Odysseus
  calendar REST API, then sends a compact feed to the T5 (§4). Neither server needs a
  new endpoint for v1.
- **The MCP contract doesn't change.** The PDA uses the HTTP API (`/v1/…`), not MCP.

## TL;DR

```
odysseus-sound  /v1/digests, /v1/conversations, /v1/action-items ─┐
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
| Buttons: BOOT (GPIO0), side button (PCA IO1_2, active low), RST | pressed by hand: BOOT short and long, side short | back, pairing window, clean refresh |
| Front light (GPIO11, LED driver enable) | off | not used (daylight reading) |
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
- **Fonts:** Fira Sans 12 and 20 pt (SIL OFL), epdiy's pre-converted headers: ASCII,
  Latin-1, general punctuation, arrows. The euro sign isn't in them and is drawn as "EUR";
  other missing characters as "?".
- **Sleep:** with the screen idle for 60 s and no BLE link, the T5 goes into light sleep. The
  e-paper keeps the image. Touch or a button wakes it. Deep sleep with periodic wake
  for an advertising window is milestone 5. **Not built yet:** v1 stays awake with DFS
  (240 MHz during a refresh, 80 MHz otherwise); light sleep moves to milestone 5 with the
  power measurements.

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

```json
{"v": 1, "version": 1729411200, "generated": "2026-10-20T07:00:00Z", "tz": "Europe/Berlin",
 "briefing": {"date": "2026-10-19", "text": "…", "highlights": ["…"]},
 "conversations": [{"id": "c_…", "title": "…", "at": "…", "people": ["…"],
                    "summary": "…", "points": ["…"]}],
 "actions": [{"id": 42, "text": "…", "owner": "…", "due": "2026-10-23", "conv": "c_…"}],
 "calendar": [{"id": "…", "title": "…", "start": "…", "end": "…", "where": "…", "all_day": false}],
 "templates": ["Call", "Meeting", "Reminder"]}
```

- **Optional `templates`** (the Android relay only): up to 12 event titles the owner set in
  the phone app, for the T5's "+" picker (§5). The PC relay doesn't send it; the firmware
  must not require it and falls back to built-in titles. Readers ignore unknown keys.
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
  - `templates` (optional): the owner's event titles for the "+" picker (§5), e.g.
    `["Call", "Meeting", "Reminder"]`. At most 12 of up to 40 characters. The phone takes
    them from its settings, the PC relay from `--templates "Call,Meeting,Reminder"`. The key
    is left out when there are none, and the T5 then uses built-in defaults.
  - Field caps in characters: title 120, summary 600, 5 points of 160, action text 200.
  - The size limit is 32,768 bytes of compact UTF-8 JSON. Over it, each summary is cut to
    240 characters, then the oldest conversations are dropped. If it is still too big
    (hundreds of open items), the last action items go, then the last calendar entries.
  - A source that fails fails the whole build, so nothing is pushed and the T5 keeps its
    feed. Two cases don't count as failures: a digest 404, and no calendar configured.
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

## 5. Screens

Portrait, 540 × 960 (§1): a two-line header (title and clock; date, battery, sync and the
outbox count), the content, a bottom row for the screen's buttons and the pager (▲ n/m ▼),
and a tab bar of six (Today, Brief, Cal, Tasks, Talks, Status). Lists use the full width
with rows of two or three lines; the new-event form stacks the date, the time with ±15 min,
a 3 × 4 grid of hour slots and the durations.

| Screen | Content | Interactions |
|---|---|---|
| **Today** (home) | clock and date, battery, sync age; the next 3 events; the briefing's first lines; the count of open action items | tap a section to open it |
| **Briefing** | yesterday's digest, highlights | `digest_refresh` |
| **Calendar** | agenda, today to +7 days | tap an event for details; "+" opens the template picker |
| **Actions** | open items, the ones due soon first | tap: done; long press: dismiss; "→ calendar" when `due` is set |
| **Conversations** | the last 48 h, title, people, time | tap: summary and key points; "transcript" → `fetch` |
| **Status** | bond, phone last seen, outbox, feed version, battery, pairing window button | |

- **Adding an event without a keyboard:**
  - From an action item: "→ calendar" fills in the title from the item and the date from
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
- **Refresh policy as built:** a new screen is a full (GC16) update; a change on the same
  screen (a pending mark, a page) a partial (GL16) one; the minute clock a partial that
  doesn't count; after 10 counted updates, and on the side button or "Clean refresh", a
  clean refresh.

## 6. Phone app and servers

- **android-relay:**
  - a second BLE job (`PdaSync`) next to `DrainSession`, sharing `Scanner`,
    `GattClient` and `Bonding`
  - a feed builder
  - an Odysseus REST client with its own encrypted token (`TokenCipher`)
  - a settings section: Odysseus URL and token, event templates, sync interval
- **odysseus-sound server:** no change in v1. Optional later: `GET /v1/glance` that returns
  the four odysseus-sound sections in one call, so the phone makes 1 request instead of 4.
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
     host as odysseus-sound on another port. If the owner turned auth off
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
