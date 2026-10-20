# Plan: LilyGO T5 E-Paper Pro as an odysseus-sound PDA

Status: **plan, nothing built.** Proposed 2026-10-20. Owner decisions are open (§11).

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

The PC relay (`firmware/recorder/tools/barvis_relay`) gets a `pda` subcommand in milestone 2.
That way the protocol can be tested without the phone, and in the sandbox with fictional
data only.

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
