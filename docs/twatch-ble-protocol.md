# T-Watch Ultra ↔ phone: BLE chunk-transfer protocol (v1)

Status: **v1, implemented** by the recorder firmware (`firmware/recorder/main/ble_*`), the
PC test relay (`firmware/recorder/tools/ble_relay.py`) and the Android relay app
(`android-relay/`). Nothing here is verified on hardware yet (see the end).

This document is the contract between those three. If code and this document disagree,
fix one of them in the same PR. Plan context: [t-watch-ultra-plan.md](t-watch-ultra-plan.md)
(§4 time and sessions, §5 phone app, §7 power) and
[xiao-esp32s3-sense-plan.md](xiao-esp32s3-sense-plan.md) (§3 BLE design). The XIAO pendant
will speak the same protocol later; only `device` differs.

## 1. Data path and the delivery rule

```
mic → Opus encoder → 30 s Ogg/Opus chunk on the SD card (queue)
    → BLE (this protocol) → phone / PC relay
    → POST /v1/ingest/chunk on the owner's server (LAN or Tailscale)
```

- The watch has **no Wi-Fi** in its image and never talks to the server. The server token
  lives only on the relay (phone or PC).
- Nothing in this path touches the cloud: no analytics, no Firebase, no Omi app or backend.
- **Delivery rule** (end to end, no step may be skipped):
  1. The watch deletes a chunk **only** after the relay sends `ACK` for it.
  2. The relay sends `ACK` **only** after the server answered `200` with `"ok": true` for
     that chunk (a `"duplicate": true` answer counts: the server already has it), and, for
     the last chunk of a session, after `POST /v1/sessions/{id}/end` also answered `200`.
  3. Every step is idempotent. The server dedupes on `(session_id, sequence)`, a lost
     `ACK` only means the chunk is uploaded again, and `ACK` of an unknown chunk succeeds.
- The relay does not keep its own copy of the audio in v1: a chunk lives on the watch until
  the server has it. A relay that can't reach the server doesn't pull chunks (§6 step 5).

## 2. Chunks

### 2.1 Audio format

- One chunk = one standalone **Ogg/Opus file** (RFC 7845), one logical stream, uploaded
  unchanged with `codec=ogg`. ffmpeg on the server decodes it; no server change.
- Encoder: libopus (`78/esp-opus`, fixed point), **16 kHz mono, 20 ms frames, ~16 kbps
  VBR**, application VOIP, complexity 1 (chosen for the CPU at 80 MHz; see the firmware
  README). DTX off in v1.
- Each chunk is encoded as its own stream (the encoder is reset per chunk), so it decodes
  on its own. A full chunk holds **1501 packets**: the 1501st carries the encoder's
  lookahead and its last granule trims it, so the chunk decodes to exactly 30.000 s and the
  next chunk starts where it ends. `pre_skip` is 312 (104 samples at 16 kHz).
- `OpusHead`: version 1, 1 channel, `pre_skip` = the encoder's lookahead in 48 kHz samples,
  input sample rate 16000, output gain 0, mapping family 0.
- `OpusTags`: vendor `barvis-sound recorder`, and three comments for debugging a card
  read on a PC: `BARVIS_SESSION=<session id>`, `BARVIS_SEQUENCE=<n>`,
  `BARVIS_START_MS=<start_ms>`. The relay never parses them; the index (§4.3) is the truth.
- Audio pages: one page per second of audio (50 packets) at most, so a power cut loses at
  most ~1 s. Granule positions count 48 kHz samples including `pre_skip`. The last page
  has the end-of-stream flag. A chunk recovered after a power cut may end without it
  (§2.4); ffmpeg decodes that.
- Length: **30 s**, except the last chunk of a session, which is shorter. About 60 KB per
  full chunk at 16 kbps. `duration_ms` is what the chunk decodes to; for a chunk recovered
  after a power cut it is (last granule − `pre_skip`) / 48, not a multiple of 20 ms.

### 2.2 Sessions

- A **session** is one recording: it starts with a PWR short press (or the console
  `session start`), and ends with another PWR short press, with mute (BOOT double-click),
  with the console `session stop`, when the card is full, or with a reboot.
- A **voice note** is a session of its own, started with a PWR hold of ~1 s (or the
  console `note start`). It has a new `session_id`, sequences from 0 and 30 s chunks as
  usual, and **every chunk of it carries the `NOTE` flag** (§4.3). It ends with a PWR press or hold,
  mute, `note stop`, or by itself after 5 min (10 chunks); its last chunk has
  `LAST`. If a conversation session is recording when a note starts, the watch ends that
  session first (its last chunk gets `LAST`), records the note, and then starts a new
  conversation session (unless muted meanwhile). The two never record at once, so a
  conversation session never holds a `NOTE` chunk. The server's gap rule joins the two
  conversation halves; notes are never joined.
- `session_id`: 8 random bytes from the hardware RNG. Its text form, used everywhere
  (index, upload, logs), is the 16 lowercase hex digits of those bytes in order, e.g.
  `3f9a0c11d2e4b507`. It matches the server's id rule (`[A-Za-z0-9._:-]{1,128}`).
- `sequence`: 0, 1, 2, … within the session, no gaps.
- `device`: `twatch-ultra` (from the `info` characteristic, §4.1). The plan's
  `twatch-ultra-<mac4>` suffix is left out while the owner has one watch; the relay always
  uploads whatever `info` says.
- The **last chunk of a session** has the `LAST` flag (§4.3). The firmware never leaves
  a session without one: it closes a full chunk only when the next packet arrives, so the
  chunk open at stop always holds audio. A session with no audio at all never produces a
  chunk, so the server never hears of it.

### 2.3 Time

- `start_ms` is the chunk's start in **UTC epoch milliseconds**, from the system clock,
  which the battery-backed RTC (PCF85063A) sets at boot and the relay corrects with `TIME`
  on every connection (§5.5; the firmware writes it back to the RTC).
- Chunks of one session are contiguous: chunk *k* starts at
  `session_start_ms + 20 ms × (packets in chunks 0..k-1)`, not at a fresh clock reading,
  so the server joins them into one run without gaps.
- If the clock was not valid when the session started (RTC lost power and no relay has
  connected since), the chunk has the `TIME_UNSET` flag and `start_ms` is **milliseconds
  since the session start** instead of an epoch time.
  - On `TIME`, the watch converts every queued `TIME_UNSET` chunk recorded since the
    current boot into epoch time (it knows its monotonic clock at the chunk start) and
    clears the flag.
  - A chunk still `TIME_UNSET` after that (recorded before a reboot, rare: the RTC keeps
    time over reboots) is uploaded with `timestamp = anchor + start_ms`, where `anchor` is
    the relay's clock when it first saw that session. The relay logs a warning.
  - So a chunk never reaches the server without a wall-clock timestamp.

### 2.4 The queue on the watch (informative)

- Files on the SD card in `/sdcard/q/`: `<chunk_id as 8 hex digits>.opus` and a small
  binary `.met` record next to it (session, sequence, times, size, CRC-32, flags, state).
  The in-memory index is rebuilt from the `.met` files at boot, so the queue survives
  reboots and power cuts. `chunk_id` is a 32-bit counter kept in `/sdcard/q/next`, never
  reused on that card.
- A chunk that was being written when the power went (state "open") is recovered at boot:
  the file is cut back to its last complete Ogg page (CRC checked). If any audio is left it
  becomes a closed chunk with `LAST` set; otherwise it is deleted and the session's
  previous queued chunk gets `LAST`. If that previous chunk was already acked, no chunk
  can carry `LAST`: the relay never calls `/end` for that session and the server closes it
  after its idle timeout (`CONVO_SESSION_IDLE_SECONDS`).
- A chunk whose `.met` record is unreadable but whose `.opus` holds audio stays on the card
  unlisted (it is never uploaded or deleted by the watch); copy it off over USB
  (`tools/pull.py --queue`). Files without audio are cleaned up.
- Only closed chunks are listed over BLE. The chunk being recorded is not visible yet.
- When the card has less than 8 MB free, recording stops (the session ends normally, 3
  buzzes). Nothing queued is ever dropped by the watch on its own.

## 3. GATT service

The watch is the peripheral (GATT server, NimBLE), the relay the central. All UUIDs are
ours, random, base `f806xxxx-61c0-4fda-8b7b-26b08e916438`:

| Name | UUID | Properties | Security | Value |
|---|---|---|---|---|
| service | `f8060000-61c0-4fda-8b7b-26b08e916438` | primary | | |
| `info` | `f8060001-61c0-4fda-8b7b-26b08e916438` | read | none | UTF-8 `key=value;…` (§4.1) |
| `status` | `f8060002-61c0-4fda-8b7b-26b08e916438` | read, notify | encrypted | 24-byte struct (§4.2) |
| `ctrl` | `f8060003-61c0-4fda-8b7b-26b08e916438` | write (with response) | encrypted | one command (§5) |
| `data` | `f8060004-61c0-4fda-8b7b-26b08e916438` | notify | encrypted | responses and chunk bytes (§5) |

- All integers are **little-endian**. `session_id` is the 8 raw bytes in order (§2.2).
- "Encrypted" means the link must be encrypted with a bonded, authenticated key (LE Secure
  Connections, passkey entry, §3.1).
  Accessing those characteristics on an unencrypted link fails with *Insufficient
  Encryption*, which makes Android and BlueZ start pairing.

### 3.1 Pairing and bonding

- **Passkey entry**, LE Secure Connections, bonding. The watch's IO capability is
  `DisplayOnly`: it shows a random 6-digit code on the AMOLED (and logs it on the USB
  console), and the user types it on the phone or PC. This protects against a
  man-in-the-middle during pairing. The code stays on screen for 30 s, the SMP timeout.
- `status`, `ctrl` and `data` need an **authenticated** (passkey) key, not just
  encryption: a link encrypted with an unauthenticated key is disconnected.
- **New bonds are accepted only in the sync window**: for 2 minutes after a BOOT hold of
  ~1 s (or `ble pair` on the console). Outside the window, a peer that wasn't bonded
  before this connection is disconnected and its new bond is deleted as soon as
  encryption comes up. So someone nearby can't pull or delete recordings.
- Outside the window the watch doesn't store new bonds at all, and re-pairing a known
  peer (a phone that lost its keys) is refused too: open the window for it.
- `ble forget` on the console deletes all bonds. Up to 4 bonds are kept; a fifth replaces
  the oldest.
- A phone that paired outside the window keeps a bond the watch has deleted, so its later
  connections fail with an authentication error. Remove `barvis-tw` in the phone's
  Bluetooth settings and pair again in the window. The Android app shows this hint.
- PC test: bleak can't type a passkey, so the operating system pairs, once, during the
  window: Linux `bluetoothctl` (`agent on`, `default-agent`, `scan on`, `pair <addr>`, type
  the code), Windows *Settings → Bluetooth & devices → Add device*, macOS the system prompt
  on first connect. Android: the app calls `createBond()` and Android asks for the code.

## 4. Characteristic values

### 4.1 `info` (read, no encryption)

UTF-8, `;`-separated `key=value`, keys in any order, unknown keys ignored. Example:

```
proto=1;device=twatch-ultra;fw=0.2.0;board=twatch_ultra;reset=poweron
```

- `proto`: protocol version. A relay refuses (and shows an error) if it isn't `1`.
- `device`: the upload's `device` field.
- `fw`, `board`: for the status screen and logs.
- `reset` (added 2026-10-20, optional): why the watch last restarted, for the status
  screen and logs: `poweron` (also the PWR key turning it on), `sw` (console reboot, a
  flash), `usb`, `panic` (a crash; the console `coredump` shows where), `task_wdt`,
  `int_wdt`, `wdt`, `brownout`, `pwr_glitch`, `cpu_lockup`, `ext`, `deepsleep`, `jtag`,
  `efuse`, `sdio` or `unknown`. Relays show it and never act on it.

### 4.2 `status` (read, notify; 24 bytes)

| Offset | Type | Field |
|---|---|---|
| 0 | u8 | `proto` (1) |
| 1 | u8 | flags: bit0 recording, bit1 muted, bit2 time valid, bit3 charging, bit4 USB power, bit5 SD card OK, bit6 sync window open |
| 2 | i8 | battery %, -1 if unknown |
| 3 | u8 | reserved, 0 |
| 4 | u16 | battery mV, 0 if unknown |
| 6 | u16 | queued chunks (closed, listable) |
| 8 | u32 | queued bytes |
| 12 | u32 | queued audio in ms |
| 16 | u32 | watch clock, UTC epoch seconds, 0 if not valid |
| 20 | u32 | `boot_id`, random per boot |

The watch notifies `status` (if subscribed) when the queue or the recording state
changes, at most once per second.

### 4.3 Chunk entry (40 bytes, in `ENTRY` responses)

| Offset | Type | Field |
|---|---|---|
| 0 | u8 | opcode `0x81` |
| 1 | u8 | flags: bit0 `LAST` (last chunk of its session), bit1 `TIME_UNSET` (§2.3), bit2 `NOTE` (chunk belongs to a voice note; every chunk of a note session has it, §2.2). Other bits are 0; relays ignore bits they don't know |
| 2 | u16 | reserved, 0 |
| 4 | u32 | `chunk_id` |
| 8 | u8[8] | `session_id` |
| 16 | u32 | `sequence` |
| 20 | u64 | `start_ms` (§2.3) |
| 28 | u32 | `duration_ms` |
| 32 | u32 | `bytes` (file size) |
| 36 | u32 | `crc32` of the whole file (IEEE 802.3, the same as zlib's `crc32`) |

## 5. Commands and responses

The relay writes one command to `ctrl` (Write Request, ≤ 20 bytes) and reads the answer
from `data` notifications. **One command at a time**: the relay waits for the command's
final response before writing the next. A command written while another runs gets
`ERROR busy`. Every `data` notification starts with a one-byte opcode.

Requirements before the first command: the relay is subscribed to `data` (and may
subscribe to `status`), and the ATT MTU is **≥ 64** (Android requests 517, BlueZ and
Windows negotiate on their own). With a smaller MTU every command answers
`ERROR mtu_too_small`.

| Command (relay → `ctrl`) | Bytes | Responses (`data`) |
|---|---|---|
| `LIST` | `01` `after_id:u32` `max:u16` | 0..`max` × `ENTRY` (chunks with `chunk_id > after_id`, ascending), then `LIST_END` |
| `READ` | `02` `chunk_id:u32` `offset:u32` `length:u32` | `DATA` frames, then `READ_END` |
| `ACK` | `03` `chunk_id:u32` | `ACKED` |
| `TIME` | `04` `00 00 00` `epoch_ms:u64` | `TIME_OK` |
| `BYE` | `05` | `BYE_OK`, then the watch disconnects after ~1 s if the relay hasn't |

| Response (`data`) | Bytes |
|---|---|
| `ENTRY` | 40 bytes, §4.3 |
| `DATA` | `82` `chunk_id:u32` `offset:u32` `payload…` (payload ≤ MTU − 3 − 9) |
| `READ_END` | `83` `status:u8` `00 00` `chunk_id:u32` `total_bytes:u32` `crc32:u32` (16 bytes) |
| `ACKED` | `84` `result:u8` `00 00` `chunk_id:u32` (8 bytes); result 0 = deleted, 1 = was not queued (already deleted) |
| `LIST_END` | `85` `00` `count:u16` `remaining:u32` (8 bytes); `remaining` = listable chunks after the last one sent |
| `TIME_OK` | `86` `00` `fixed:u16` (4 bytes); `fixed` = `TIME_UNSET` chunks converted |
| `BYE_OK` | `87` (1 byte) |
| `ERROR` | `EE` `cmd:u8` `code:u8` `00` `arg:u32` (8 bytes) |

### 5.1 `LIST`

- Lists closed chunks with `chunk_id > after_id` in ascending order, at most `max`
  (the watch caps it at 32). Start with `after_id = 0`, continue with the last id seen
  while `remaining > 0`.
- Chunks closed while the relay is connected show up in the next `LIST`.

### 5.2 `READ`

- Streams bytes `[offset, offset + length)` of the chunk file (`length = 0`: to the end) as
  `DATA` notifications in order, then `READ_END` with `status` 0, the chunk's full size
  and its CRC-32 (of the whole file, not the range).
- Resume after a disconnect with `offset` = bytes already received. An `offset` at or past
  the end sends no `DATA`, only `READ_END`.
- `READ_END.status` uses the error codes of §5.6: 0 = complete, 6 = card error, 3 = the
  chunk disappeared during the read. A `READ` with no card answers `READ_END` status 6.
- The relay checks: every `DATA` offset is the next expected byte, the total equals
  `ENTRY.bytes` and `READ_END.total_bytes`, and the CRC-32 of the whole file equals
  `ENTRY.crc32`. On a mismatch it discards the bytes and reads the chunk again: 4 reads in
  total (the first plus 3 re-reads), then it skips the chunk for this connection and
  reports it.
- A `READ_END` with `status` ≠ 0, or `ERROR io_error`, means the card failed: the relay
  stops the drain, sends `BYE` and retries later with the backoff (§6 step 5). `ERROR
  not_found` on `READ` (the chunk went away) skips that chunk and the drain goes on.
- A relay keeps a partly read chunk in memory and resumes it with `offset` on the next
  connection, if the chunk's entry (session, sequence, size, CRC) is unchanged.

### 5.3 `ACK`

- Deletes the chunk (both files) and answers `ACKED` result 0. Unknown `chunk_id`: result
  1, which is success too (a lost `ACKED` makes the relay ack again).
- Only after the server accepted the chunk (§1).

### 5.4 `BYE`

- The relay has nothing more to do. The watch answers `BYE_OK`, goes back to its idle
  advertising rules (§7.1) and disconnects if the relay hasn't within ~1 s.

### 5.5 `TIME`

- Sets the watch clock and RTC to `epoch_ms` (UTC). The watch refuses times before
  2024-01-01 or after 2100 with `ERROR bad_time`.
- Converts queued `TIME_UNSET` chunks of the current boot (§2.3); `TIME_OK.fixed` says how
  many listed chunks changed. The chunk being recorded is converted too but not counted,
  and a running session continues in epoch time from its next chunk.
- The relay sends it once per connection, before `LIST`.

### 5.6 Error codes

| Code | Name | Meaning |
|---|---|---|
| 1 | `bad_length` | command too short or too long |
| 2 | `unknown_cmd` | opcode not known (`arg` = 0) |
| 3 | `not_found` | `READ` of a chunk that isn't queued (`arg` = chunk id) |
| 4 | `busy` | another command is still running |
| 5 | `mtu_too_small` | ATT MTU < 64 (`arg` = current MTU) |
| 6 | `io_error` | SD card unavailable or a read failed |
| 7 | `bad_time` | `TIME` outside 2024–2100 |

`arg` is 0 unless the table says otherwise. A command too short to have an opcode gets
`cmd` = 0. An `ERROR busy` for a command written during a `READ` arrives between that
`READ`'s `DATA` frames; relays never write during a `READ`, so they don't see it.

## 6. Relay algorithm (phone app and `ble_relay.py`)

1. **Scan** for the service UUID (scan filter). Connect.
2. Request **MTU 517** (Android; on a PC the OS negotiates it and the watch's `ERROR
   mtu_too_small` tells if it's too low), 2M PHY if available, and high connection priority for the
   transfer (Android `CONNECTION_PRIORITY_HIGH`).
3. **Encrypt / bond**: subscribe to `data` and `status` (both need encryption, so a new
   phone pairs here; §3.1).
4. Read `info` (check `proto=1`, keep `device`) and `status`. Send `TIME` with the relay's
   clock.
5. **Check the server** with `GET <server>/health` (no token needed). If it isn't
   reachable, send `BYE`, disconnect and retry later (backoff: 1, 2, 5, 10, 15 min). Don't
   pull chunks the server can't take.
6. **Drain**: `LIST` from `after_id = 0`. For each entry, in ascending `chunk_id` order:
   1. `READ` the whole chunk and verify it (§5.2).
   2. `POST <server>/v1/ingest/chunk` with `Authorization: Bearer <token>`, multipart
      form fields:

      | Field | Value |
      |---|---|
      | `audio` | the file, filename `<session_id>-<sequence>.opus`, type `audio/ogg` |
      | `device` | `info.device` |
      | `session_id` | 16 hex digits |
      | `sequence` | decimal |
      | `timestamp` | `start_ms` (or `anchor + start_ms`, §2.3) as ISO-8601 UTC with milliseconds, e.g. `2026-10-19T12:34:56.789Z` |
      | `codec` | `ogg` |
      | `kind` | `note` if the entry has `NOTE`; otherwise the field is left out (the server defaults to `conversation`), so ordinary uploads are unchanged |

   3. On `200` with `"ok": true`: if the entry has `LAST`, `POST
      <server>/v1/sessions/<session_id>/end` (same token) and wait for `200`. Then send
      `ACK` and wait for `ACKED`.
   4. The server's `"stop"` field is ignored: the watch decides when a session ends.
   5. Errors:
      - network error, timeout, `5xx`, `408` or `429`: stop draining, `BYE`, disconnect,
        retry later with the backoff above. Nothing is acked.
      - `401`/`403`: stop, show "token rejected" until the settings change.
      - other `4xx` (`400` bad field, `413` too large, `422` undecodable): the chunk
        stays on the watch. The relay shows the error, skips the chunk for this
        connection and goes on with the next one. The owner decides (the watch's console
        `queue drop <id>` deletes it). `422` uploads are kept by the server in
        `data/rejected/` for inspection. A `4xx` from `/end` is handled the same way.
      - HTTP redirects are not followed (they would carry the token elsewhere); a `3xx`
        counts as retry-later.
   6. Continue with `LIST` after the last id until it returns no entries and
      `remaining = 0`.
7. `BYE`, disconnect, drop back to scanning.

The relay never logs the token, and logs no audio content. It may log chunk ids, sizes,
sessions and server answers.

## 7. Link and power choices

The watch spends almost all of its time not connected. Plan §7 and XIAO §3.1 favour
batching: the radio is on for seconds per hour.

### 7.1 Advertising (watch)

The watch advertises **only when there is something to do**:

- the sync window is open (2 min after a BOOT hold of ~1 s or `ble pair` / `sync`), or
- a finished session was queued since the last `BYE` (a chunk with `LAST`), or
- at least **10 min of audio** was queued since the last `BYE` (a long recording still
  running), or
- chunks are queued and the last successful sync (a `BYE`) was more than 60 min ago, or
  there was none since boot.

"Since the last `BYE`" matters for chunks a relay left on the watch (§6 step 6.5, e.g. the
server answered `422`): they bring advertising back only through the hourly rule, so a
rejected chunk can't keep the radio on.

Otherwise it doesn't advertise, and while connected it doesn't advertise either (one
relay at a time). Advertising is connectable, undirected, **interval 1000–1250 ms**, or
**152.5–211.25 ms while the sync window is open** (someone is connecting right then; at
~1 s Windows' connection scan sometimes missed the watch for its whole 20 s timeout),
advertising data = flags + the 128-bit service UUID, scan response = name `barvis-tw`.

So a recording that runs on reaches the phone every ~10 min, and a stopped one within
seconds of the phone being in range.

### 7.2 Connection (watch and relay)

- Once the link is encrypted, the watch asks for **interval 15–30 ms, latency 0,
  supervision timeout 4 s** for the burst; the Android relay sets
  `CONNECTION_PRIORITY_HIGH`. 2M PHY and data-length extension (251 bytes) are preferred
  when both sides support them; the watch sets them as controller defaults, so it sends no
  per-connection request at connect time (one raced Windows' encryption start and the
  controller stopped answering).
- **MTU**: the watch supports up to 517; at 517 a `DATA` frame carries 505 bytes, so a
  60 KB chunk is ~120 notifications.
- The watch disconnects a relay that sent no command for **30 s**, so a stuck relay can't
  keep the radio busy. Relays use an HTTP timeout below that (25 s). If an upload still
  takes longer, the link drops before the `ACK`; that is safe: the next connection
  uploads the chunk again, the server answers `duplicate`, and the relay acks it.
- A connection costs a few mA for a few seconds; idle advertising at ~1 s costs tens of
  µA. There is no idle long-interval connection in v1 (XIAO §3.1 option): the drain-then-
  disconnect pattern is simpler and lets the watch control when the radio is on.
  Milestone 2 measures both.

## 8. Test vectors

Hex, as sent on the air. All three implementations test against these.

| What | Hex |
|---|---|
| `LIST after=0 max=32` | `01 00000000 2000` |
| `READ chunk 7 from 0, all` | `02 07000000 00000000 00000000` |
| `ACK chunk 7` | `03 07000000` |
| `TIME 2026-10-19T12:00:00Z` (1792411200000 ms) | `04 000000 006a0854a1010000` |
| `BYE` | `05` |
| `ENTRY` chunk 7, `LAST`, session `3f9a0c11d2e4b507`, seq 2, start 1792411260000 ms, 12340 ms, 24680 bytes, crc `0xcbf43926` | `81 01 0000 07000000 3f9a0c11d2e4b507 02000000 60540954a1010000 34300000 68600000 2639f4cb` |
| `ENTRY` chunk 8, `LAST`+`NOTE`, session `3f9a0c11d2e4b507`, seq 0, start 1792411260000 ms, 12340 ms, 24680 bytes, crc `0xcbf43926` | `81 05 0000 08000000 3f9a0c11d2e4b507 00000000 60540954a1010000 34300000 68600000 2639f4cb` |
| `DATA` chunk 7 offset 505, payload `aabb` | `82 07000000 f9010000 aabb` |
| `READ_END` ok, chunk 7, 24680 bytes, crc `0xcbf43926` | `83 00 0000 07000000 68600000 2639f4cb` |
| `ACKED` deleted, chunk 7 | `84 00 0000 07000000` |
| `LIST_END` 3 sent, 5 remaining | `85 00 0300 05000000` |
| `TIME_OK` 2 fixed | `86 00 0200` |
| `ERROR` on `READ`, `not_found`, chunk 9 | `ee 02 03 00 09000000` |
| `status`: recording + time valid + SD OK, 81 %, 3950 mV, 2 chunks, 120000 bytes, 60000 ms, clock 1792411200, boot 0x01020304 | `01 25 51 00 6e0f 0200 c0d40100 60ea0000 4006d66a 04030201` |

The `info` example is `proto=1;device=twatch-ultra;fw=0.2.0;board=twatch_ultra`.

## 9. Versioning

- `proto` in `info` and `status` is 1. A change that an old relay would misread
  (new mandatory command, changed layout) bumps it, and the relays refuse other versions.
- Adding a response field at the end of a struct, a new flag bit, a new `info` key or a
  new command is compatible and keeps `proto=1`.
- The `reset` key in `info` (§4.1, 2026-10-20) is such an additive change; older relays
  ignore it.
- The `NOTE` flag bit (§4.3, 2026-10-19) is such an additive change: it stays within
  `proto=1`. Relays must ignore flag bits they don't know. An old relay that ignores
  `NOTE` uploads a voice note as an ordinary conversation, without `kind=note`.

## Not verified on hardware

- BLE throughput between the watch and real phones/PCs (expected tens of KB/s with 2M
  PHY and MTU 517; a 30 s chunk in a second or two).
- Pairing behaviour with the sync window on Android and BlueZ.
- How Android scheduling and background limits affect scan-and-connect in the app.
- Power: advertising and burst-connection current (milestone 2).
