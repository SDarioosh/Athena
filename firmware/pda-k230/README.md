# PDA firmware: LilyGO T-Display K230

The second Athena PDA ([docs/k230-pda-plan.md](../../docs/k230-pda-plan.md)), beside the T5
E-Paper Pro ([../pda/README.md](../pda/README.md)): the same Today, briefing, calendar,
checklists and conversations on the K230's 4.1" AMOLED touchscreen (568 × 1232), with
animation, an on-screen keyboard and hand gestures from the camera. It runs on LilyGO's
Linux image (Buildroot), reaches the hub over **Tailscale** and is its own relay: no phone
in between.

- **Through the hub, with a paired token.** The device talks only to Odysseus' token
  routes, paired with the hub's **Pair a phone** QR code. No server or hub change.
- **Camera frames never leave the device** and are never stored; only events leave the
  vision process. Vision is off until the owner turns Hands-free on.
- **No telemetry.** tailscaled runs with `--no-logs-no-support`; the programs log no
  content, to a size-capped log in RAM.
- **Fictional data only** in this repo (the T5's `test/fixtures/`).
- Nothing has run on the board yet: see "Not verified yet".

## Layout

| Directory | What |
|---|---|
| `ui/` | `athena-pda` (C11, LVGL 9 on DRM + evdev, SDL or headless on a PC): the screens, the feed store, the detail cache and the outbox on disk (the T5's pure modules, compiled from `../pda/main`), the server end of `glance.sock` and `vision.sock`. See `ui/README.md` |
| `syncd/` | `athena-pda-syncd` (Python 3, standard library): pairing, the hub client, `PdaSync` over `glance.sock` with the BLE-free modules of `../recorder/tools/athena_relay`; reports the tailnet state and starts the Tailscale login. Usage and settings in its module docstrings (`__main__.py`, `config.py`, `daemon.py`); tests in `syncd/tests/` |
| `vision/` | `athena-vision` (C++17, nncase on the KPU, V4L2): hand gestures, presence and QR codes as `vision.sock` events; `athena-vision-replay` plays recorded keypoint tracks on a PC. See `vision/README.md` |
| `tests/e2e/` | the programs together on a PC: the real UI (headless, driven through its test control socket `ATHENA_PDA_TEST_CTL`, `ui/src/platform/headless_ctl.h`) and the real syncd against syncd's fake hub, a scripted vision client; skipped unless `ATHENA_PDA_UI_BIN` is set |
| `system/` | the image: a Buildroot br2-external tree on LilyGO's build, boot scripts, Tailscale, Wi-Fi and board helpers |
| `system/BOARD.md` | the board's nodes (display, touch, backlight, camera, battery) and boot facts, each with its source in LilyGO's tree, confirmed or guessed |

`system/`:

| File | What |
|---|---|
| `build-image.sh` | the one build command (below) |
| `gen-defconfig.sh` | LilyGO's defconfig + `br2-external/board/defconfig.fragment` -> our defconfig |
| `br2-external/` | `external.desc`, `external.mk`, `Config.in`; `board/`: the defconfig fragment, the kernel fragment (`CONFIG_TUN=m`, policy routing), `post-build.sh` (moves the launcher's boot script aside) |
| `br2-external/package/athena-pda-system` | the `athena-pda` user, `/etc/default/athena-pda`, the supervisor (restart on crash, output to syslog), `S42athena-wifi` + `athena-wifi`, `athena-board`, `athena-tailnet`, `athena-boot-ui`, the `tailscale` wrapper syncd uses |
| `br2-external/package/athena-tailscale-bin` | Tailscale's static riscv64 build, pinned with its sha256; `S45tailscaled` |
| `br2-external/package/athena-pda-ui`, `athena-pda-lvgl-src` | the UI (`S92athena-pda`) and the LVGL source it builds offline |
| `br2-external/package/athena-pda-syncd` | syncd and the relay modules in `/usr/lib/athena-pda/python` (`S93athena-pda-syncd`) |
| `br2-external/package/athena-vision`, `athena-vision-kmodels` | vision (`S94athena-vision`) and the two hand kmodels from Canaan's archive (downloaded at build time, never in this repo) |
| `dev/run-local.sh` | the UI, syncd and the vision replay on a PC with temp directories |

On the device: data in `/var/lib/athena-pda` (UI: `feed.json`, `d/`, `ob/`, `ui.json`;
syncd: `token` (0600), `pda.json`, `config.json`), sockets in `/run/athena-pda` (0700), the
tailnet state in `/var/lib/tailscale`, Wi-Fi in `/var/lib/athena-wifi` (root only), settings
in `/etc/default/athena-pda` and `/etc/athena-pda/vision.conf`. LilyGO's image has no data
partition, so all of it is on the root file system; reflashing the card erases it.

Boot order (BusyBox init, `/etc/init.d`): `S31canaan_isp` (camera ISP, LilyGO/SDK),
`S40<defconfig>` (Wi-Fi driver), `S42athena-wifi`, `S45tailscaled`, `S48sntp`/`S49ntp`,
`S92athena-pda` (UI), `S93athena-pda-syncd`, `S94athena-vision`. Each of our programs runs
under `athena-supervise`: started again after a crash (1 s, doubling to 60 s), output to
syslog. The UI and syncd run as the user `athena-pda`; tailscaled and vision as root
(TUN and routes; the camera and KPU nodes are root-only, `system/BOARD.md`).

## Build the image

On an Ubuntu 22.04/24.04 PC (or WSL2), with ~50 GB free; the first build takes hours:

```bash
git clone --recurse-submodules https://github.com/Xinyuan-LilyGO/T-Display-K230.git ~/t-display-k230
cd ~/t-display-k230 && ./scripts/setup_ubuntu.sh      # LilyGO's: toolchain in /opt/toolchain
<athena>/firmware/pda-k230/system/build-image.sh ~/t-display-k230
```

`build-image.sh` runs LilyGO's `apply_to_sdk.sh`, writes
`k230_linux_sdk/buildroot-overlay/configs/k230_canmv_t_display_athena_pda_defconfig`
(LilyGO's defconfig plus `defconfig.fragment`), builds it with `BR2_EXTERNAL` pointing here
and exports the image to `k230_bsp/images-athena-pda/sysimage-sdcard.img`. LilyGO's own
defconfig and output directory are left as they are. A rebuild after changing PDA code:
`make -C ~/t-display-k230/k230_linux_sdk athena-pda-ui-rebuild all` (or `-syncd-`,
`athena-vision-`), then the export step of the script (`SKIP_APPLY=1` skips step 1).

Downloads during the build, besides LilyGO's: Tailscale's tarball (pkgs.tailscale.com),
LVGL at 59dc7e4 (GitHub, the same file LilyGO's lvgl package uses) and Canaan's
`kmodel_v2.10.0.tgz` (download.kendryte.com, the same file the SDK's ai_demo uses).

## Flash the SD card

As LilyGO's README: `sudo dd if=k230_bsp/images-athena-pda/sysimage-sdcard.img of=/dev/sdX
bs=8M status=progress conv=fsync` (Linux; check `/dev/sdX` is the card), or Rufus in DD
mode on Windows. Windows offers to format the Linux partitions: cancel.

## First boot

1. **Shell.** Plug USB-C into the PC: `adb shell` (LilyGO's image runs `adbd`), or the
   serial console. You are root.
2. **Wi-Fi.** `athena-wifi add "<your SSID>"` and type the passphrase (stored as its WPA
   hash, root only); it joins at once and at every boot. `athena-wifi status` shows the
   address. If LilyGO's launcher already has the network: `athena-wifi import-launcher`.
3. **Tailscale.** The Status screen shows a login QR code (syncd starts `tailscale login`
   when the device needs one); scan it with the phone and approve the device in the
   tailnet. Without the screen: `athena-tailnet login` prints the link. `athena-tailnet
   status` shows the state. The device only connects out: no Funnel, Serve or exit node.
4. **Pair with the hub.** On the hub, **Pair a phone**; on the PDA, Status → Pair, then
   scan the QR code with the K230's camera or type the code. Development without the UI:
   `athena-pda-syncd --pair '<link>'`.
5. The PDA syncs; Status shows the hub, the last sync and the tailnet name.

Switches (all in `/etc/default/athena-pda`, one line each, applied at the next boot or by
restarting the script named there):

- **Back to LilyGO's launcher:** `athena-boot-ui launcher` (sets
  `ATHENA_PDA_BOOT_UI=launcher`; syncd and vision stay off); `athena-boot-ui athena-pda` to
  return. The PDA's Status can also start the launcher (until the next boot).
- **Tailscale without TUN:** `athena-tailnet mode userspace` (sets `ATHENA_TS_MODE` and
  syncd's `"proxy": "http://127.0.0.1:1055"` in `config.json`, restarts both);
  `athena-tailnet mode tun` undoes it.
- **No camera AI:** `ATHENA_VISION=off`.
- **UI as root** (if the display or touch refuses the `athena-pda` user):
  `ATHENA_PDA_USER=root` (syncd follows: both must share the socket's owner).

Logs: `/var/log/messages` (tags `athena-pda`, `athena-pda-syncd`,
`athena-vision`, `tailscaled`, `athena-supervise`); RAM only, 200 KB plus one old file.

## On a PC

Quick start, as run in the sandbox (build directories anywhere outside the repo; `$B` here):

```bash
# the UI: host tests and the screenshots of every screen (into $B/ui/screens)
cmake -S firmware/pda-k230/ui -B $B/ui && cmake --build $B/ui -j && ctest --test-dir $B/ui
# vision: the core's tests, also under ASan + UBSan
cmake -S firmware/pda-k230/vision -B $B/vision -DVISION_WERROR=ON && cmake --build $B/vision -j \
  && ctest --test-dir $B/vision
cmake -S firmware/pda-k230/vision -B $B/vision-asan -DVISION_SANITIZE=ON -DVISION_WERROR=ON \
  -DCMAKE_BUILD_TYPE=Debug && cmake --build $B/vision-asan -j && ctest --test-dir $B/vision-asan
# syncd and the relay modules it uses (pytest; ruff with the server's config)
python -m pytest -q firmware/recorder/tools firmware/pda-k230
ruff check --config server/pyproject.toml firmware/pda-k230
# end to end: the real UI and syncd against the fake hub (+ the vision replay tools)
ATHENA_PDA_UI_BIN=$B/ui/athena-pda ATHENA_VISION_TOOLS=$B/vision \
  python -m pytest -q firmware/pda-k230/tests/e2e
# riscv64 (Ubuntu's gcc-riscv64-linux-gnu; tests under qemu-riscv64 when installed)
cmake -S firmware/pda-k230/ui -B $B/ui-rv \
  -DCMAKE_TOOLCHAIN_FILE=$PWD/firmware/pda-k230/ui/cmake/riscv64-linux-gnu.cmake \
  && cmake --build $B/ui-rv -j && QEMU_LD_PREFIX=/usr/riscv64-linux-gnu ctest --test-dir $B/ui-rv
# the image's scripts
grep -rlE '^#!.*\b(ba|da)?sh\b|^# shellcheck shell=' firmware/pda-k230/system | xargs shellcheck -x
```

To try it by hand: `firmware/pda-k230/system/dev/run-local.sh [--headless] [--track FILE]
[--pair LINK]` builds the UI (an SDL window if SDL2 is installed), starts it and syncd with
temp directories, and with `--track` replays a keypoint track through the vision core. The
device-only parts (DRM, KPU, camera, Tailscale) aren't there. CI runs all of the above
(`.github/workflows/ci.yml`, jobs `firmware-k230-*`).

## Verified

Nothing on the board. In the sandbox (2026-11-13/14), with the commands of "On a PC":

- The UI on the host: warning-clean (`-Wall -Wextra -Werror`), 12 ctest entries pass (11
  test programs and the screenshot run, 79 PNGs, looked at); cross-compiled for riscv64
  with Ubuntu's `gcc-riscv64-linux-gnu`, warning-clean, the same 12 pass under
  `qemu-riscv64` (all but the DRM backend, which needs libdrm in a sysroot).
- vision: 11 ctest entries pass, also under ASan + UBSan.
- syncd: its tests and the PC relay's (`python -m pytest -q firmware/recorder/tools
  firmware/pda-k230`) pass on Python 3.10 and 3.13; ruff is clean.
- End to end (`tests/e2e`, 5 tests): the real UI (headless) and the real syncd with the fake
  hub: pairing by a QR code sent on `vision.sock` and by typing on the keyboard, the first
  sync writes `feed.json`, a to-do tick reaches the hub and is acked, a transcript `fetch`
  lands in `d/`, a revoked token shows "Pairing lost" (`auth_failed`), syncd reconnects and
  syncs after the UI restarts; Contract 1's sockets (0600, INFO, a 16-byte STATUS, a second
  client refused); gestures on `vision.sock` (swipe, palm, fist) and a swipe made by
  `synth_track | athena-vision-replay` move the UI.

- Buildroot 2025.02.1 (the SDK's version), with the SDK's and LilyGO's overlays applied as
  their scripts do: `make k230_canmv_t_display_athena_pda_defconfig` with this
  br2-external tree resolves every package and option of the fragment; the local packages
  copy only their directories (`<pkg>-rsync`); `utils/check-package` is clean.
- Tailscale 1.104.7 riscv64: the tarball's sha256 matches Tailscale's `.sha256` file and
  Buildroot's hash check; both binaries are static and print their version under
  `qemu-riscv64`. tailscaled in userspace mode under qemu: `tailscale set
  --operator --hostname --netfilter-mode=off` works before login, `status --json` reports
  `NeedsLogin` and the `AuthURL` once a login starts, and `--no-logs-no-support` stops the
  connection to Tailscale's log server. A `tailscale login` without flags resets the
  operator, host name and netfilter mode, which is why syncd's `tailscale` is a wrapper.
- `athena-supervise` (dash and BusyBox ash): restart with back-off, the user change with
  supplementary groups, stop by TERM, the stop file. `athena-wifi`: the PSK hash against
  the IEEE 802.11i test vector, the launcher's profile format. The syncd install layout runs
  (`athena-pda-syncd --version` with `ATHENA_RELAY_PATH`). `dev/run-local.sh --headless`:
  the UI builds and runs, syncd connects to it.
- `shellcheck` and `sh -n` on every script.

## Not verified yet

Owner, on the build PC and the board. Each step with what you should see:

1. **Build.** `build-image.sh ~/t-display-k230` ends with `Image: …/sysimage-sdcard.img`.
   If the LVGL or kmodel download fails, the error names the URL; paste it.
2. **Boot.** After flashing and power-on, the PDA's Today screen appears instead of LilyGO's
   launcher, within about 20 s. `adb shell ls /usr/libexec/athena-pda/k230_phone_ui.init`
   exists; `ls /etc/init.d/S99zz*` finds nothing.
3. **Services.** `ps | grep -E 'athena|tailscaled'` lists `athena-pda` and
   `athena-pda-syncd` (user athena-pda), `athena-vision` and `tailscaled` (root), each
   under an `athena-supervise`. `grep -E 'athena|tailscaled' /var/log/messages` has no repeated
   "ended … starting again". `ls -ld /run/athena-pda` is `drwx------ athena-pda`;
   `ls -l /run/athena-pda` shows `glance.sock` and `vision.sock` (`srw-------`).
4. **Display and touch as non-root.** Taps work. If the screen stays black or taps do
   nothing, `grep athena-pda /var/log/messages` and `athena-board nodes`; then try
   `ATHENA_PDA_USER=root` and reboot, and report which worked. The picture fills the panel
   upright (the UI assumes 568 × 1232 portrait; the log line `DRM: WxH` names the mode), and
   a tap lands where the finger is, also in the corners (the GT9895's axes and scaling).
   Scrolling a long list and a tab change look smooth: report the frame rate and the UI's
   CPU load from `top` while scrolling (LVGL renders in software at this size).
5. **Restart on crash.** `kill $(pidof athena-pda)`: the screen comes back within ~2 s;
   the log says "ended (…) after … s; starting again in 1 s".
6. **Brightness and blank.** `athena-board brightness 40` dims, `athena-board blank on`
   turns the panel dark, `blank off` restores it; and the same from the PDA's Settings.
   With no touch for `dim_after_s` (Settings, default 2 min) the ambient screen (clock, next
   event) shows dimmed, then the panel goes off after `off_after_s`; a touch wakes it, also
   from brightness 0.
7. **Wi-Fi at boot.** After `athena-wifi add`, reboot: `athena-wifi status` shows
   `wpa_state=COMPLETED` and an address without touching anything; `date` is right within
   a minute (ntp). Status' Wi-Fi row shows the network's signal (read from
   `/proc/net/wireless` with the 8189fs driver; "No Wi-Fi device" means it reads nothing).
8. **TUN.** `lsmod | grep tun` (or `ls -l /dev/net/tun`), `ip addr show tailscale0` after
   login with a 100.x address; `grep tailscaled /var/log/messages` has the line "set: operator
   athena-pda, hostname athena-pda, netfilter off".
9. **Login QR.** Before login, Status shows a QR code; scanning it opens
   `login.tailscale.com`; after approval Status shows the tailnet name
   (`athena-pda.<tailnet>.ts.net`). `tailscale debug prefs` still has `"OperatorUser":
   "athena-pda"` and `"NetfilterMode": 0` after the login.
10. **MagicDNS.** `nslookup <hub>.<tailnet>.ts.net` resolves; after a Wi-Fi reconnect (new
    DHCP lease) it still does (tailscaled rewrites `/etc/resolv.conf`).
11. **Pairing and sync** over Tailscale: Status → Pair with Odysseus → Scan the QR code:
    the camera indicator shows, the hub's Pair a phone code is read at 20-40 cm and the
    screen says Paired. Again with Type the code: the keyboard types the mixed-case code.
    Then Status shows the hub and "Synced …"; a to-do ticked on the PDA is done in the hub
    after the next sync; a conversation's transcript opens after "Read the transcript"; "→
    Calendar" on an action item with a due date adds the event to the hub's calendar.
    Revoke the token in the hub: Status says "Pairing lost" after the next sync.
12. **Userspace fallback.** `athena-tailnet mode userspace`, reboot: no `tailscale0`, sync
    still works (syncd through `127.0.0.1:1055`); `athena-tailnet mode tun` back.
13. **Launcher switch.** `athena-boot-ui launcher`, reboot: LilyGO's launcher, and
    `ps` shows no athena-pda-syncd or athena-vision; `athena-boot-ui athena-pda`, reboot:
    the PDA. From the PDA's Status "Start launcher": the launcher appears without a reboot.
14. **Camera and gestures.** First vision's own checklist (`vision/README.md` "On-board
    checklist", with its fake UI). Then with the PDA: Hands-free on (Status): the camera
    indicator shows and the hand cursor follows the hand; swipes change tabs, `point` moves
    the focus ring, a thumbs-up held on a focused to-do fills the ring and ticks it, palm
    goes to Today, fist to the ambient screen. With presence wake on, the screen wakes when
    you sit down and dims after you leave. Hands-free off: the camera stops.
15. **Battery (keyboard base only).** `athena-board battery` prints a SoC; if it prints
    `gauge=none` on a board with the base, the kernel bus guess is wrong (BOARD.md).
16. **Logs stay small and content-free.** After a day, `ls -l /var/log/` stays under ~400 KB;
    `grep -i -E 'token|transcript' /var/log/messages` finds nothing.
17. **Power:** current with the screen on, dimmed and off; battery hours with Wi-Fi and
    Tailscale up. On the bare board (no keyboard base) there is no fuel gauge: the header
    shows no battery, which is expected.
