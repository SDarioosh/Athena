# athena-pda: the K230 PDA's UI

The screen program of the T-Display K230 PDA ([docs/k230-pda-plan.md](../../../docs/k230-pda-plan.md)):
C11, LVGL 9, one thread. It owns the feed, the detail cache and the outbox on disk and is the
server end of `glance.sock` (syncd, Contract 1) and `vision.sock` (athena-vision, Contract 2).

## Build, test, screenshots

```bash
cmake -S firmware/pda-k230/ui -B build-k230-ui && cmake --build build-k230-ui -j
ctest --test-dir build-k230-ui                  # host tests + the screenshot run
build-k230-ui/pda_screenshots                   # or: PNGs into ./screens (OUT_DIR=… to move them)
```

ctest's `pda_screenshots` writes `build-k230-ui/screens/*.png`. Run the program on a PC:
`ATHENA_PDA_DIR=/tmp/pda ATHENA_PDA_RUN=/tmp/pda-run build-k230-ui/athena-pda` (an SDL window
when SDL2 was found, else `ATHENA_PDA_BACKEND=headless`).

| Option | Default | |
|---|---|---|
| `PDA_DEVICE` | OFF | DRM/KMS + evdev (needs libdrm): the K230 |
| `PDA_SDL` | ON if SDL2 is found (not when cross-compiling) | a window on a PC (`ATHENA_PDA_ZOOM`, 0.6) |
| `PDA_TESTS` | ON | host tests and `pda_screenshots` |
| `PDA_WERROR` | ON | `-Wall -Wextra -Werror` on our code (LVGL is built with `-w`) |

- **LVGL** is fetched (never vendored) at `59dc7e436ae97a25e32656739ea6a943f9f11b6a`, 9.5-dev, the
  commit LilyGO's Linux image uses, by `cmake/fetch_commit.cmake` (a shallow `git fetch` of the
  SHA). Offline: `-DFETCHCONTENT_SOURCE_DIR_LVGL=<checkout> -DFETCHCONTENT_FULLY_DISCONNECTED=ON`.
  We build LVGL's `src/*.c` ourselves with `lv_conf.h` (only what differs from LVGL's defaults).
- **Cross-compiling:** `-DCMAKE_TOOLCHAIN_FILE=firmware/pda-k230/ui/cmake/riscv64-linux-gnu.cmake`
  (Ubuntu's `gcc-riscv64-linux-gnu`, or `-DCROSS=<prefix>`). Checked: everything but the
  device backend builds warning-clean, and the tests and the screenshot run pass under
  `qemu-riscv64` (`QEMU_LD_PREFIX=/usr/riscv64-linux-gnu`).
- **Fonts** are embedded in the binary (`cmake/embed_file.cmake`): nothing to install.

## Environment

| Variable | Default | |
|---|---|---|
| `ATHENA_PDA_DIR` | `/var/lib/athena-pda` | `feed.json`, `d/`, `ob/`, `ui.json`; at most 44 bytes (the T5 outbox keeps `<dir>/ob` in 48) |
| `ATHENA_PDA_RUN` | `/run/athena-pda` | `glance.sock`, `vision.sock` (0600), `start-launcher` |
| `ATHENA_PDA_BACKEND` | the best compiled in | `headless`, `sdl`, `device` |
| `ATHENA_PDA_DRM` | `/dev/dri/card0` | device |
| `ATHENA_PDA_TOUCH` | the input device named `goodix_ts` | device |

`ui.json`: `{"v":1,"hands_free":false,"presence_wake":false,"brightness":80,"tz_min":120}`
(`tz_min` is the display offset of the last `TIME`).

## Layout

| Path | What |
|---|---|
| `src/main.c` | the process: state dir, backend, the poll loop over LVGL's timers and both sockets, power every 60 s, the launcher hand-over |
| `src/core/` | no LVGL; linked alone by the tests |
| `core/app_state.*` | feed, outbox, pending / failed marks, details, clock offset, link / net / tailnet / vision state, settings; the `pda_store_t` for `pda_proto.c`; `app_todo_tap()` (tap, undo, reverse); listeners |
| `core/glance_frame.*`, `glance_server.*`, `local_msg.*` | Contract 1: framing, the socket (one client, CTRL through the T5's `pda_proto.c` at MTU 517, STATUS notify at most 1/s), LOCAL in and out |
| `core/vision_msg.*`, `vision_server.*` | Contract 2: lines, the socket, `mode` on connect and on change |
| `core/handsfree.*` | the gesture table as commands; the 1 s thumbs-up hold |
| `core/today_text.*` | Today's second lines and time column: the rules of the T5's `ui_today.c` (which draws with epdiy and can't be linked) |
| `core/power.*`, `board.*` | power_supply sysfs; the board: backlight `rm69a10` (screen off = 0), the `goodix_ts` node, `athena-board battery`, `start-launcher` |
| `src/platform/` | headless (memory framebuffer, PNG, a virtual finger, a manual clock), SDL, device (DRM + evdev) |
| `src/ui/theme.*` | design tokens (colours, spacing, type scale, motion, icons) and the fonts |
| `src/ui/ui.*`, `ui_internal.h`, `chrome.c`, `handsfree_ui.c` | the frame: pages per tab, tab slides and swipes, detail push/pop, the sheet, toasts, screen off; the header and tab bar; focus ring, hand cursor, hold ring |
| `src/ui/widgets.*` | scroller, page title, card, section head, text with a line limit, rows, the animated tick, the to-do row, chips, empty states |
| `src/ui/screen_today.c`, `screen_tasks.c`, `sheet_event.c` | Today, Tasks, an event's sheet |
| `src/ui/screen_stub.c`, `ui_screens.c`, `screens.h` | placeholders for Brief, Calendar, Talks, Status; the tab table |
| `fonts/` | Inter 4.0 (SIL OFL, `LICENSE-Inter.txt`) Regular/SemiBold/Bold cut to Latin-1 + punctuation, € ™ arrows ✓ ★, with the icons merged in; Lucide icons (ISC, `LICENSE-Lucide.txt`); `make_fonts.py` remakes them |
| `test/` | `test_app_state`, `test_glance` (real socket, the T5's byte frames), `test_vision`, `test_today_text`, `test_board`, `pda_screenshots` |

## Adding a screen (Brief, Calendar, Talks, Status)

1. Write `src/ui/screen_<name>.c` defining `const ui_screen_t screen_<name>` (`label`, `icon`,
   `build`, `update`, `scroller`); remove its `STUB(...)` line in `screen_stub.c`; add the file
   to `pda_ui` in `CMakeLists.txt`. The declaration in `screens.h` and the entry in
   `ui_screens.c` are already there.
2. `build(page)`: once; make `ui_page_scroller(page)`, keep it in a static and return it from
   `scroller()`. Put the content in a `rebuild()` that `lv_obj_clean`s the scroller and keeps
   its scroll position (see `screen_tasks.c`).
3. `update(page, what)`: `what` is `UI_DIRTY_*`. Rebuild on `FEED` (and `CLOCK` when the day
   matters); on `OUTBOX` only refresh pending marks in place so animations survive.
   `NET`/`VISION`/`SETTINGS`/`POWER` are for Status; `DETAIL` when a fetched blob arrived.
4. Data: `app_feed()` (NULL: show `ui_no_feed()`), `app_today()`, `app_tz_min()`, `app_now()`,
   `app_pending_*()`, `app_failed_*()`, `app_load_detail(ref)`, `app_net()`, `app_tailnet()`,
   `app_link_up()`, `app_vision()`, `app_settings()` / `app_set_settings()`,
   `app_battery_pct()`, `app_outbox_count()`, `app_status()`.
5. Actions: build the JSON with the T5's `pda_actions.h` (`pda_action_item_status`,
   `pda_action_cal_add`, `pda_action_fetch`, `pda_action_digest_refresh`, …), queue with
   `app_queue()`, undo with `app_cancel()` before syncd listed it (`app_pending_t.listed`).
   To syncd directly: `ui_send_local()` with `local_pair()` / `local_unpair()`;
   `ui_request_sync()`. The pairing scan: `app_set_qr_scan(true)` and read `VIS_QR` events
   (add a hook in `ui_vision_event` or a listener). Launcher: `ui_start_launcher()`.
6. Navigation: `ui_show_tab()`, `ui_push(title, &detail, arg)` / `ui_pop()` for a
   conversation or transcript (`ui_detail_t.next` handles swipe left), `ui_sheet_open()`,
   `sheet_event_open(event_idx, why)`, `ui_toast()`.
7. Look: only `theme.h` tokens and `F.*` fonts; `ui_card()` + `ui_section_head()` +
   `ui_row()` (+ `ui_hairline()` inset like Today) for lists; `ui_text(…, max_lines)` for
   anything that can be long; `ui_show()` to hide/show (it resizes content-sized parents);
   `ui_focusable(obj, false)` on every tap target so hands-free can point at it.
8. A state worth seeing goes into `test/pda_screenshots.c` (find labels, `headless_tap`,
   `step(ms)`); open the PNGs and look.

## What hands-free does here

Only with `hands_free` on (it also turns on vision's `gestures` mode): swipes = next/previous
tab (in a sheet: right closes it; in a detail: next / back), up/down = scroll by ¾ page,
`point` = focus ring to the tap target under the hand cursor, thumbs-up (hand shape or
gesture) on a focused to-do = a filling ring for 1 s, then the tick (anything else aborts),
`palm` = Today (and wakes the screen), `fist` = screen off. Presence wake (`presence_wake`):
on/off with `presence`. The camera dot shows while vision reports `camera: on`.

## Not verified (needs the board)

DRM mode and orientation on the RM69A10 (the UI assumes 568 × 1232 portrait and logs
otherwise), evdev touch scaling/axes on the GT9895, the backlight writes, the battery helper,
frame rate and CPU load of the software renderer at this size, real gestures.
