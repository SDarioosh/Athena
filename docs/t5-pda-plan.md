# Plan: LilyGO T5 E-Paper Pro as an odysseus-sound PDA

Status: **plan.** Proposed 2026-10-20. The PC relay side (§3.5) is built and tested
against a fake T5; the firmware and the phone side are not. Owner decisions are open (§11).

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

## 1. Hardware facts (to confirm at bring-up)

From LilyGO's documentation. Check each one on the owner's board in milestone 0.

| Part | Use |
|---|---|
| ESP32-S3, 16 MB flash, 8 MB PSRAM | firmware, a 960×540 4-bit framebuffer (≈ 260 KB) in PSRAM, feed cache in flash |
| 4.7" e-paper, 960×540, 16 grey levels, parallel bus, with its own power IC (TPS65185) | the display, driven by [epdiy](https://github.com/vroland/epdiy) |
| GT911 capacitive touch | taps, swipes |
| RTC (PCF8563 class) | wall clock between phone connections |
| Charger and fuel gauge (BQ25896 / BQ27220 class) | battery %, charging state |
| microSD slot | not used in v1 (§7) |
| Buttons | wake, page back, full refresh |
| Optional LoRa module on some variants | not used |

- Two ESP32-S3 native-USB devices are connected to the owner's PC (COM35 and COM36). One is
  the T-Watch Ultra. Find out which one is the T5 before flashing anything
  (`idf.py -p COMx monitor` on a known-good image, or match the MAC).
- **Refresh:** a full refresh with flashing takes about 1 s; a partial refresh of a region
  takes a few hundred ms. After many partial refreshes the panel ghosts, so it needs a full
  refresh every N updates or on a button press. Measure both in milestone 0.

## 2. Firmware

- **The same ESP-IDF project as the recorder.** Add a new board, `boards/t5_epaper_pro/`,
  behind the existing `board.h` layer. Also add a new app target, because the PDA records
  no audio. The open question is a second `main` or a Kconfig switch in the shared `main/`
  (§11 Q1).
- **Reused as it is:** NimBLE pairing and bonding (`ble_sync`), the `ble_proto`
  encode/decode style, `isotime`, `crc32`, and the console commands.
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
- **Storage:** a LittleFS partition of about 2 MB in internal flash, holding:
  - `feed.json` and `feed.tmp`, swapped atomically after the CRC checks out
  - the outbox records
  - up to 20 cached detail blobs, evicted oldest first
- **Sleep:** with the screen idle for 60 s and no BLE link, the T5 goes into light sleep. The
  e-paper keeps the image. Touch or a button wakes it. Deep sleep with periodic wake
  for an advertising window is milestone 5.

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
  report the MTU, 420 is assumed. The T5 accepts frames of up to 512 bytes.
  On `ERROR bad_crc` at `PUT_END` the relay sends the blob again, 3 times in all.
- **Stay connected:** the relay waits for `status` notifications and also reads `status`
  every 30 s. It refreshes the feed every 5 min, and sends `BYE` once bit4 (user active)
  is clear.
- **Token:** the PC relay uses the server's `CONVO_INGEST_TOKEN`, which every `/v1` route
  accepts. It goes only in the `Authorization` header.

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

The PC relay's formats (v1):

- **`transcript:<id>`**: `{"v": 1, "ref", "title", "from", "to", "next", "segments": […]}`.
  - Without a range it sends the first page, 0–600 s.
  - A page holds the segments with `from ≤ o < to`.
  - `next` is the ref of the following page (`transcript:<id>@600-1200`), or `null` at the
    end.
- **`conv:<id>`**: `{"v": 1, "ref", "title", "at", "people", "summary", "points",
  "actions": [{"id", "text", "status", "due"}]}`.

## 5. Screens

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

The PC relay (`firmware/recorder/tools/barvis_relay`) gets a `pda` subcommand in milestone 2.
That way the protocol can be tested without the phone, and in the sandbox with fictional
data only. **Built 2026-10-20:** `ble_relay.py pda` (`--list`, `--once`, stay-connected by
default). It is tested against a fake T5, a fake server and the real API with seeded
fictional data. It hasn't run against the T5 yet.

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
