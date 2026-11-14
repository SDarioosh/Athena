# Plan: LilyGO T-Display K230 as an Athena PDA

Status: **built, not on the board** (2026-11-14) in `firmware/pda-k230/`. M1-M3 are done in
the sandbox: every screen, syncd and the vision core, each with its tests, and the UI and
syncd run together against a fake hub (`firmware/pda-k230/tests/e2e`, in CI). M4's
Buildroot tree resolves in the sandbox; building the image and everything on the board (M4,
M5) is the owner's. What is verified and the owner's checklist are in
`firmware/pda-k230/README.md`.

## Why

- The owner has a **LilyGO T-Display K230**: Kendryte K230 (two 64-bit RISC-V cores, a
  neural accelerator), 1 GB RAM, a 4.1" AMOLED touchscreen (568 × 1232), a camera, a
  microphone, Wi-Fi, an SD card. No Bluetooth.
- It becomes a second, more polished PDA beside the T5 E-Paper Pro
  ([t5-pda-plan.md](t5-pda-plan.md)): the same content (Today, briefing, calendar,
  checklists, conversations), on a colour touchscreen with animation, a keyboard, and
  on-board AI from the camera (hand gestures first).
- The T5 work is the starting point: its feed, actions and details (T5 plan §4), its
  glance protocol (§3), its pure C modules and the Python relay are reused, not rewritten.

## Decisions

- **Linux, not CanMV/RT-Smart.** The device must reach the server over **Tailscale**, and
  Tailscale runs only on the board's Linux (LilyGO's `T-Display-K230` repo: Buildroot on
  Canaan's `k230_linux_sdk`, kernel 6.6). CanMV MicroPython has no Tailscale or WireGuard.
- **The device is its own relay.** The T5 needs a phone to bridge BLE to the server. The
  K230 has Wi-Fi and Tailscale, so the relay runs on the device as a second process and
  talks to the UI over a local socket in place of BLE. Both halves keep the code that is
  already tested: the UI side keeps `pda_proto.c`, the outbox and the feed store; the
  relay side keeps `athena_relay`'s `PdaSync`, feed builder and action runner.
- **Through the hub, with a paired token.** The device reaches Athena only through
  Odysseus' token routes (`/api/codex/athena/*`, calendar, todos, insight), like the
  Android relay. It is paired with the hub's existing **Pair a phone** QR code
  (`odysseus-pair:v1?url=…&code=…` → `POST /api/pair/redeem`), scanned with the K230's own
  camera or typed on its keyboard. No server or hub change is needed for v1.
- **Camera frames never leave the device** and are never stored. Vision runs on the
  K230's accelerator; only events ("swipe left", "someone is here") leave the vision
  process. The screen shows an indicator whenever the camera is on. Vision is off until
  the owner turns it on.
- **The MCP contract doesn't change.**

## Architecture

```
            Odysseus (hub)  ── token routes ──  Athena
                  ▲
                  │ HTTP over Tailscale (tailscaled on the device)
                  │
┌─────────────────┴───────────── T-Display K230 (Linux) ─────────────────────────┐
│  athena-pda-syncd (Python)          athena-pda (C, LVGL 9)       athena-vision   │
│  feed builder, actions,   glance    screens, feed store,  events (C++, nncase)   │
│  pairing, hub client     ◄───────►  outbox, clock        ◄──────  camera, hand   │
│                          glance.sock                    vision.sock  gestures     │
└───────────────────────────────────────────────────────────────────────────────────┘
```

| Process | Language | Directory | Job |
|---|---|---|---|
| `athena-pda` | C, LVGL 9 (DRM + evdev on the device, headless/SDL on a PC) | `firmware/pda-k230/ui/` | the screens; owns the feed, the detail cache and the outbox on disk; the server end of both sockets |
| `athena-pda-syncd` | Python 3 (CPython, in the image) | `firmware/pda-k230/syncd/` | the relay: pairing, hub client, `PdaSync` over the socket |
| `athena-vision` | C++ (nncase runtime, V4L2) | `firmware/pda-k230/vision/` | camera AI: hand gestures, presence, the pairing QR code |
| image | Buildroot external tree, init scripts | `firmware/pda-k230/system/` | packages the three programs and Tailscale into LilyGO's Linux image |

Reused unchanged (compiled or imported from where they are, never copied):

- `firmware/pda/main/`: `json`, `feed`, `pda_time`, `today_plan`, `outbox`, `pda_actions`,
  `feed_store`, `pda_proto`, `ui_nav`, `gesture` (the pure, host-tested modules), and
  `firmware/recorder/main/crc32.c`, `isotime.c`.
- `firmware/recorder/tools/athena_relay/`: `pda_sync`, `pda_feed`, `pda_actions`,
  `pda_todos`, `pda_insight`, `pda_calendar`, `pda_protocol`.

If one of them needs a change to work here, the change is made there in a way that keeps
the T5 and the PC relay working (their tests still pass), and is listed in the PR. So far
one: `pda_sync` reports a 401/403 from `health()` as `auth_failed` (the hub's health route
needs the token; the PC relay's `/health` doesn't, so nothing changes for it).

## Contract 1: `glance.sock` (UI ↔ syncd)

The glance protocol of T5 plan §3 over a Unix stream socket in place of GATT. The UI
listens on `$ATHENA_PDA_RUN/glance.sock` (default `/run/athena-pda/`, mode 0600); syncd
connects, one client at a time (a second is refused). A connected client is "the link is
up" (T5: a phone connected).

Frame: `type:u8` `len:u16 LE` `payload` (`len` ≤ 512).

| Type | Direction | Payload |
|---|---|---|
| `0x01` CTRL | syncd → UI | one command of §3.3 (`TIME`, `PUT_*`, `OB_LIST`, `OB_ACK`, `BYE`), bytes as on BLE |
| `0x02` DATA | UI → syncd | one response of §3.3 (a BLE notification) |
| `0x03` STATUS_REQ | syncd → UI | empty |
| `0x04` STATUS | UI → syncd | the 16-byte `status` of §3.2: the answer to STATUS_REQ, and sent unasked when it changes (the notify) |
| `0x05` INFO_REQ | syncd → UI | empty |
| `0x06` INFO | UI → syncd | `proto=1;device=k230-pda;fw=<version>;board=t_display_k230` |
| `0x10` LOCAL | both | one UTF-8 JSON object (below); not part of glance v1 |

- The MTU is taken as 517: the checks and the 507-byte `PUT_DATA` payload of §3.6 apply
  unchanged. Pairing, bonding and the pairing window don't exist here: the socket's file
  mode is the access control.
- Status flags: bit1/bit2 (charging, USB power) from the board when known; bit3 (pairing
  window) is always 0; bit4 (user active) as on the T5. The bare board has no fuel gauge
  (only the keyboard base has one, read by `athena-board battery`), so there the battery is
  unknown.
- `BYE` ends a sync, not the connection: syncd stays connected and syncs again later.
- `TIME` still sets the display offset; the system clock is NTP's, so the UI doesn't set
  it (it only refuses an out-of-range time as §3.6 says).

LOCAL messages (`"t"` names the type; unknown types and keys are ignored):

| `t` | Direction | Fields | Meaning |
|---|---|---|---|
| `sync_now` | UI → syncd | | sync as soon as possible (pull to refresh, a new outbox entry is already signalled by STATUS) |
| `pair` | UI → syncd | `url`, `code` | redeem a pairing code at the hub; a scanned QR code goes as its raw text in `url` with an empty `code` (syncd parses `odysseus-pair:v1?…` itself) |
| `unpair` | UI → syncd | | forget the token |
| `net` | syncd → UI | `state` (`unpaired`, `offline`, `syncing`, `ok`, `auth_failed`), `hub` (host shown on Status, or null), `last_ok` (epoch s, 0 never), `msg` (one short line for Status, or "") | sent on connect and on every change |
| `pair_result` | syncd → UI | `ok`, `msg` | answer to `pair` |
| `tailnet` | syncd → UI | `state` (`unknown`, `stopped`, `needs_login`, `running`), `url` (the login link while `needs_login`, else null), `name` (this device's tailnet name, or null) | from `tailscale status --json` (read only); the UI shows the login link as a QR code |

## Contract 2: `vision.sock` (UI ↔ vision)

The UI listens on `$ATHENA_PDA_RUN/vision.sock` (mode 0600); `athena-vision` connects. One
JSON object per line (UTF-8, `\n`), at most 1024 bytes.

| `t` | Direction | Fields | Meaning |
|---|---|---|---|
| `mode` | UI → vision | `gestures`, `presence`, `qr` (booleans) | what to run; all false = camera off (the stream is stopped, not just ignored). Sent on connect and on every change |
| `state` | vision → UI | `camera` (`on`/`off`/`error`), `fps` (number), `msg` | sent on every change and every 5 s while on |
| `gesture` | vision → UI | `g`, `conf` (0–1) | one recognised gesture, already debounced: `swipe_left`, `swipe_right`, `swipe_up`, `swipe_down`, `palm` (open hand held), `fist`, `thumbs_up` (held), `point` |
| `hand` | vision → UI | `x`, `y` (0–1, mirrored so moving the hand right moves right on screen), `shape` (`none`, `open`, `fist`, `point`, `thumbs_up`, `other`) | at most 10 per second while a hand is seen, for the on-screen hand cursor; `shape: none` once when the hand goes |
| `presence` | vision → UI | `present` (boolean) | a person appeared or left (debounced: 2 s to appear, 20 s to leave) |
| `qr` | vision → UI | `text` | a decoded QR code (only while `qr` is on) |

What the UI does with gestures (only while Hands-free is on in Settings):

| Gesture | Action |
|---|---|
| `swipe_left` / `swipe_right` | next / previous tab; in a detail: next page / back |
| `swipe_up` / `swipe_down` | page down / up |
| `point` | move the focus ring to the item under the hand cursor |
| `thumbs_up` held 1 s | tick the focused to-do (the only gesture that changes data; shown with a filling ring so it can be aborted) |
| `palm` held | back to Today |
| `fist` | screen off (ambient) |

Presence: the screen wakes when someone appears and dims to the ambient screen after they
leave (a setting, off by default).

## Storage

`$ATHENA_PDA_DIR` (default `/var/lib/athena-pda`, on the root filesystem: LilyGO's image has
no data partition):

- UI: `feed.json`, `d/` (details), `ob/` (outbox), as on the T5; `ui.json` (settings).
- syncd: `token` (the hub token and URL, mode 0600), `pda.json` (`PdaState`: last pushed
  hash, calendar dedupe, last good weights), `config.json` (time zone, calendar,
  templates, proxy).

## Tailscale

- `tailscaled` from Tailscale's static riscv64 build, started at boot, state in
  `/var/lib/tailscale`. Default: a normal tailnet interface (`CONFIG_TUN=m` added to the
  kernel fragment). Fallback without TUN: `--tun=userspace-networking` and syncd's
  `proxy` setting (`http://127.0.0.1:1055`).
- First login: root's `athena-tailnet watch` (run beside tailscaled) starts `tailscale
  login` whenever Tailscale needs one; syncd reads the link from `tailscale status --json`
  and passes it to the UI (`tailnet` message), and the Status screen shows it as a QR code
  to scan with the phone.
- The PDA user (UI and syncd) is **no Tailscale operator**: it can read `status` but not
  change tailscaled (no serve, funnel, exit node, ssh, login flags). Nothing it sends or
  writes reaches a `tailscale` command line.
- No Funnel or other public exposure, ever.

## Screens

Portrait 568 × 1232, dark (true black, for the AMOLED), touch first. The T5's tabs and
rules (T5 plan §5: Today's sections and order, the pending marks, "couldn't sync", swipes)
carry over; what is new:

- animation between screens and on ticks, pull down to sync, momentum scrolling in place
  of pages;
- an on-screen keyboard (keys about 50 × 84 px, ten in a row; a held key pops up above the
  finger): add an event with a typed title (letters, `123`, `#+=`), type the pairing code
  and the hub's address (a code mode: both cases, digits, `/ : . - _`, no automatic capital,
  one row taller);
- an ambient screen (clock, next event) in place of the T5's Asleep screen;
- Settings in Status: brightness, Hands-free, presence, pairing, Tailscale login. Wi-Fi is
  not set up in the UI: `athena-wifi` over adb (the image's first-boot step); Status only
  shows the link;
- the camera indicator and the hand cursor.

Adding a to-do item by keyboard needs a hub write the relay doesn't have yet; it is not
in v1.

## Milestones

| | What | Verified where |
|---|---|---|
| M1 | UI on a PC: every screen from the fictional fixtures, rendered to PNG; the store and `glance.sock` with the T5's protocol tests | sandbox, CI: **done** |
| M2 | syncd: `PdaSync` over the socket against the UI, a fake hub, pairing | sandbox, CI: **done**, also end to end with the real UI |
| M3 | vision: gesture logic from recorded fictional keypoint tracks; the nncase/V4L2 pipeline compiled for riscv64 | logic in the sandbox: **done**; the pipeline only on the board |
| M4 | image: Buildroot external tree, Tailscale, autostart | tree written, defconfig resolves in the sandbox; the image on the owner's build machine |
| M5 | on the board: touch, sync over Tailscale, gestures, frame rate, power | the owner |

## Not in v1

Recording from the microphone (the Wi-Fi recorder idea), LoRa, wake word, adding to-do
items, face recognition, whiteboard capture.

## Open questions for the owner

1. Does the camera face the user (needed for gestures while reading the screen)?
2. LilyGO's Linux repo has no prebuilt image: the owner builds it (hours, ~50 GB). OK?
3. The PDA starts in place of LilyGO's launcher at boot (the launcher stays installed and
   can be started from Status). OK?
