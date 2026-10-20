# Plan: LilyGO T5 E-Paper Pro as an odysseus-sound PDA

Status: **firmware milestones 0–3 built** in `firmware/pda/` (2026-10-20; what is verified on
the board is in `firmware/pda/README.md`). Relays: see their branches. Owner decisions are open
(§11).

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

### 3.6 Details settled by the firmware (v1)

`firmware/pda/main/pda_proto.c` and `ble_pda.c`; byte vectors in
`firmware/pda/test/test_pda_proto.c`. They agree with the PC relay's §3.5 (on its branch)
where both say something.

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
  notifications). A write longer than 512 bytes answers `ERROR bad_length`. This replaces
  the "MTU − 8" of the PC relay's §3.5.
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
 "calendar": [{"id": "…", "title": "…", "start": "…", "end": "…", "where": "…", "all_day": false}]}
```

- Keep it under 32 KB. If it is too big, the phone cuts summaries first, then drops the oldest
  conversations.
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
| `cal_add` | `title`, `start`, `end` or `all_day`, optional `from_item` | Odysseus calendar create (to confirm, §6) |
| `cal_delete` | `id` | Odysseus calendar delete (v2) |
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
- **Odysseus (hub repo):** confirm in `SDarioosh/Barvis`. Per
  [frontend-decision.md](frontend-decision.md) §8, it has calendar REST endpoints
  (`routes/codex_routes.py`), CalDAV write-back and scoped REST tokens. Check:
  1. the list endpoint for a date range, and the create and delete endpoints with their
     fields
  2. a token scoped to calendar only (read + write), not a full admin token
  3. that a create through REST is written back to CalDAV like a create in the UI
  4. that the endpoints are reachable on the Tailscale address the phone already uses

  If anything is missing, the fix goes in a hub PR, not here (AGENTS.md "Where work
  belongs").

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
data only.

## 9. Risks

- **Watch and PDA on one phone:** two bonded peripherals and two jobs on the Android BLE
  stack. Keep them one at a time in v1, and test both together early (milestone 4).
- **Odysseus calendar REST** may not have the fields or the scoped token assumed here (§6).
  It is unverified until the hub repo is checked.
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
