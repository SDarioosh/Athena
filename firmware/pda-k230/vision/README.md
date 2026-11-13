# athena-vision

The camera AI of the K230 PDA ([docs/k230-pda-plan.md](../../../docs/k230-pda-plan.md),
Contract 2 `vision.sock`): hand gestures, presence and the pairing QR code, on the
K230's KPU. It connects to the UI's `vision.sock`, runs only what the UI's `mode` asks
for, and sends back only events.

**Privacy.** Frames never leave the process and are never stored. Pixels exist only in the
driver's capture buffers, the KPU's input tensor and (while QR is on) a small grey copy in
memory. There is no option to dump frames, in any build. With no UI connected, or with
every mode off, the camera stream is stopped. `--record-keypoints` writes hand boxes and
keypoints and a face-seen boolean, never pixels and never QR texts.

Status: the core is host-tested; the device layer compiles and links for riscv64 against
the real nncase runtime but has **not run on the board** (the sandbox has no K230). See
[On-board checklist](#on-board-checklist).

## Layout

| Path | What |
|---|---|
| `src/core/` | the pure core, no camera/model/OpenCV code; host-tested |
| `src/core/hand_shape.*` | open / fist / point / thumbs_up / other from 21 keypoints (geometry) |
| `src/core/canaan_gesture.*` | Canaan's sq_handkp_class rule (fist, five, gun, ...) and its mapping to our shapes |
| `src/core/gesture_tracker.*` | observations -> debounced `gesture` events and the throttled `hand` stream |
| `src/core/presence.*` | debounced present/absent (2 s / 20 s) |
| `src/core/mode.*` | what runs for a `mode` message |
| `src/core/orientation.*` | camera pixels -> display orientation (rotate 0/90/180/270, mirror) |
| `src/core/json.*`, `protocol.*` | tiny JSON reader/writer; Contract 2 lines, <= 1024 bytes |
| `src/core/engine.*` | the core put together: lines in, detector results in, lines out |
| `src/core/track.*` | keypoint track lines (record on the board, replay on a PC) |
| `src/core/config.*` | all settings; `key = value` file and `--key=value` flags |
| `src/net/line_client.*` | non-blocking Unix socket line client (host-tested) |
| `src/device/` | board only (`-DVISION_DEVICE=ON`): camera, KPU models, QR, `main.cpp` |
| `tools/replay.cpp` | `athena-vision-replay`: a track through the core, prints the events |
| `tools/synth_track.cpp`, `tools/synth.*` | synthetic, fictional hands and tracks (tests, examples) |
| `tools/device_compile_check.sh` | riscv64 compile of the device layer without the board's sysroot |
| `tools/fake_ui.py` | the UI's end of vision.sock for testing on the board (Python 3, stdlib) |
| `tests/` | unit tests (own tiny harness, `testlib.h`), `replay_e2e.cmake` |
| `vision.conf.example` | the config file, installed to `share/athena-vision/` |

## Build and test (host)

```sh
cd firmware/pda-k230/vision
cmake -S . -B build -DVISION_WERROR=ON && cmake --build build -j && ctest --test-dir build
# with AddressSanitizer + UBSan
cmake -S . -B build-asan -DVISION_SANITIZE=ON -DVISION_WERROR=ON -DCMAKE_BUILD_TYPE=Debug \
  && cmake --build build-asan -j && ctest --test-dir build-asan
```

11 ctest entries (10 unit test programs, 84 test cases, plus `replay_end_to_end`). No
network, no models, fictional data only.

## Device build

Buildroot (`firmware/pda-k230/system/` packages it) with Buildroot's toolchain file:

```sh
cmake -S . -B build-dev -DCMAKE_TOOLCHAIN_FILE=.../host/share/buildroot/toolchainfile.cmake \
  -DVISION_DEVICE=ON [-DVISION_QR=zxing|opencv|none] && cmake --build build-dev && cmake --install build-dev
```

`VISION_DEVICE=ON` turns tests and tools off by default. `VISION_K230_CPU_FLAGS` (on) adds
the Xuantie C908 flags of Canaan's demos (`-mcpu=c908v ...`, Xuantie GCC only). Installs
`bin/athena-vision` and `share/athena-vision/vision.conf.example`.

Without the board's sysroot, `tools/device_compile_check.sh` type-checks every file for
riscv64 (Ubuntu's `g++-riscv64-linux-gnu`) against the SDK's headers and the nncase
runtime release; see the script's header.

### Runtime needs (for the image)

Build dependencies (Buildroot packages): `libnncase` (nncase 2.11.0 runtime, static),
`vvcam` (libv4l2-drm), `display`, `libdrm`, `libmmz`, `gsl-lite`, and for QR either
`zxing-cpp` (default, `BR2_PACKAGE_ZXING_CPP=y`, readers on) or OpenCV with
`BR2_PACKAGE_OPENCV4_LIB_OBJDETECT=y` (`-DVISION_QR=opencv`; objdetect isn't in LilyGO's
defconfig). Runtime shared libraries: libstdc++, libv4l2-drm, libdisplay, libdrm, and
libZXing (or the OpenCV libraries).

On the board:

- the ISP stack started at boot by the SDK's `S31canaan_isp` (vvcam kernel modules and
  `isp_media_server`); athena-vision opens `/dev/videoN`, N = index of the device whose
  card is `vvcam-video.0.0` plus 1 (the AI channel, as Canaan's demos use), or
  `video_device`;
- the KPU, ai2d and shared-memory nodes the nncase runtime and libmmz open:
  `/dev/k230-gnne`, `/dev/k230-ai2d`, `/dev/mmz`, and `/dev/video*` (read/write): run it as
  root, or as a user in the groups owning those nodes;
- **only one camera user**: LilyGO's launcher must not use the camera at the same time;
- models: `hand_det.kmodel`, `handkp_det.kmodel` in `/usr/share/athena-vision/models/`
  (`model_dir`), and the face model the image already has,
  `/root/app/face_detect/face_detection_320.kmodel` (`BR2_PACKAGE_FACE_DETECT=y`, on in
  LilyGO's defconfig);
- config: `/etc/athena-pda/vision.conf` (optional; `--config FILE` for another);
- socket: `$ATHENA_PDA_RUN/vision.sock`, default `/run/athena-pda/vision.sock` (the UI
  creates it); athena-vision retries every 0.5-5 s until it is there;
- start it after the UI (or in any order) and restart it on exit; it stops on SIGTERM.
  Logs go to stderr.

### The models

From Canaan's kmodel bundle for the SDK's AI demos,
`https://download.kendryte.com/k230/downloads/kmodel/kmodel_v2.10.0.tgz` (sha256
`ec97b61b7a7482af0373ab87ac73d7b5bbdf7540c2571fee0d3c9d54d1f4e40a`, from the SDK's
`ai_demo/common/common.hash`), files `kmodel/ai_poc/kmodel/hand_det.kmodel` (palm
detector, yolov5 + MobileNetV2, 512 x 512) and `handkp_det.kmodel` (21 keypoints,
ResNet50, 256 x 256). Buildroot fetches the bundle with `BR2_PACKAGE_COMMON=y` (into
`$(BUILD_DIR)/common/kmodel/ai_poc/kmodel/`), or `BR2_PACKAGE_SQ_HANDKP_CLASS=y` installs
both to `/root/app/sq_handkp_class/` (then set `model_dir` there). The download server
wasn't reachable from the sandbox, so the bundle's contents and its licence were not
checked: **the kmodels carry no licence statement that we found**; the SDK's code is
BSD-2-Clause. Confirm before redistributing an image.

## Using it

```
athena-vision [--config FILE] [--KEY=VALUE ...] [--list-keys] [--version] [--help]
```

Every config key is also a flag (`--rotate 270`, `--mirror=false`, `--verbose 2`);
`--list-keys` prints all of them with their values. `--verbose 2` logs every frame's hand
(position, shape, confidence) to stderr: useful for checking orientation and shapes.

### Recording keypoints, replaying them

```sh
# on the board: hand keypoints (and face-seen booleans) to a file, while the UI runs
athena-vision --record-keypoints /var/lib/athena-pda/track-$(date +%s).jsonl
# on a PC: the events the core makes from it; tune with any key
athena-vision-replay track.jsonl
athena-vision-replay --hands --swipe_min_speed 0.6 --palm_hold_s 1.0 track.jsonl
synth_track swipe_left --fps 15 | athena-vision-replay -    # a synthetic example
```

The track format is in `src/core/track.h` (a header line with frame size and orientation,
then one line per frame: boxes, scores, 42 keypoint numbers in camera pixels). Replay uses
the header's orientation unless `--rotate`/`--mirror` are given, and `--mode` chooses what
runs (default `gestures,presence`). The file is opened for appending and holds no pixels,
but it does show how a hand moved: delete it when done.

## How the gestures work, and why these thresholds

Coordinates are normalised display coordinates (0..1 after rotation and mirroring); times
are seconds from the frame timestamps, so behaviour doesn't depend on the frame rate (5-30).
All of it is in `TrackerConfig` / `HandShapeConfig` / `PresenceConfig`.

- **Hand shape** (default classifier `geometry`, recommended): per finger the reach
  (tip-to-wrist over knuckle-to-wrist: ~1.8-2.1 straight, ~0.8-1.2 curled) and the bend
  (angle between the first and last bone: ~0 straight, ~180 curled); extended >= 1.45 and
  <= 55 deg, curled <= 1.25 or >= 95 deg, the gap between is "unsure" so a half-curled
  finger never decides a shape. Thumb: tip-to-middle-knuckle over palm length >= 0.75 and
  straight (<= 45 deg) is out, <= 0.55 is folded. Ratios and angles make it scale-,
  rotation- and handedness-independent. thumbs_up also needs the thumb within 50 deg of
  screen-up (a thumb sideways or down is "other"). On synthetic hands with keypoint noise of
  3 % of the palm length: 100 % correct for open/fist/point, 99.8 % thumbs_up; at 5 %:
  94.6-100 %; Canaan's rule (`classifier = canaan`) gets 87 % thumbs_up at 3 % and 72-97 % at 5 %,
  because its fixed angle thresholds have no "unsure" band and it can't tell thumbs up from
  down. Canaan's labels map five -> open, fist -> fist, one and gun -> point,
  thumbUp -> thumbs_up (only if the thumb points up), the rest -> other.
- **Shape debounce**: a new shape counts after 0.15 s (one or two frames), so a single
  misclassified frame doesn't break a hold; confidence < 0.55 counts as "other".
- **Swipes**: >= 0.22 of the width (0.20 of the height) within 0.6 s at >= 0.8 widths/s,
  1.8x more along the axis than across. A deliberate swipe covers 0.3-0.5 of the view in
  0.2-0.4 s (1-2.5 /s); slow hand drift and the cursor moves of pointing stay below
  0.5 /s. At 5 fps a 0.4 s swipe is 2-3 frames, so only two samples are required. Once per
  swipe: the history is cleared when one fires, nothing fires for 0.6 s (refractory), the
  opposite direction is blocked for 1.0 s (the return stroke, also across the hand leaving
  the view). Cost: a real swipe back within 1 s of a swipe is ignored.
- **Holds** need the hand still (within 0.06 of where the hold began) and fire once:
  palm (open) 0.8 s (as the contract says), fist 0.5 s (long enough to pass through a fist
  while forming a thumbs-up, which takes 0.3 s), thumbs_up 0.3 s (the UI runs its own 1 s
  confirm ring and the `hand` stream keeps flowing with `shape: thumbs_up`, so the UI sees
  it end), point 0.3 s and again after the hand moved and settled (to move the focus ring to
  a new item). No hold within 0.6 s after a swipe (a moving open hand isn't a palm).
- **Cursor**: exponential smoothing (tau 70 ms), at most one `hand` per 0.1 s; sent when it
  moved >= 0.004, the shape changed, or 1 s passed. The interval is strict, so at 15 fps the
  rate is 7.5/s (every second frame). `shape: none` once when the hand is unseen for 0.5 s
  (bridges missed detections and dropped frames), also when gestures are turned off.
- **Presence**: a face (or, while gestures run, a hand) seen for 2 s with no gap over 1 s;
  absent after 20 s unseen. Face detection runs at 3 fps alone, every 0.5 s beside gestures.
- **Several hands**: the engine follows the hand nearest to the last one (within 0.15),
  else the biggest box.

## Contract 2 details settled here

- `mode` fields missing or not booleans are `false`; unknown `t` values and keys are
  ignored; lines over 1024 bytes from the UI are dropped whole.
- Until the UI's first `mode` after connecting, nothing runs (camera off). On disconnect the
  camera stops at once.
- `state` is sent on connect, on every camera change (`on`, `off`, `error` with `msg`: camera
  failures and model load failures) and every 5 s while on; `fps` is the processed frame
  rate over the last 2 s, one decimal, 0 when not on.
- `gesture.conf` has 2 decimals; `hand.x/y` 3 decimals, clamped to 0..1. The `hand` position
  is the detector box's centre (steadier than keypoints, and present when keypoints fail).
- `presence` is sent only on a change, starting from absent at connect / when presence is
  turned on.
- `qr`: a code is sent once while it stays in view, again only after it was out of view for
  3 s or when a different code shows. A QR text that doesn't fit in 1024 bytes is not sent
  (cut would be wrong); a long `state.msg` is cut on a UTF-8 boundary.
- Orientation: `rotate = 90` (as LilyGO's camera app turns the sensor image for the portrait
  screen) and `mirror = true` (a camera facing the user) are defaults to check on the board.

## Third-party

| What | From | Licence | How used |
|---|---|---|---|
| `src/core/canaan_gesture.cpp` | `hand_angle()` / `h_gesture()` of k230_linux_sdk `ai_demo/sq_handkp_class/hand_keypoint.cc` | BSD-2-Clause, (c) 2025 Canaan Bright Sight Co., Ltd | rewritten, same rules and thresholds; notice kept in the file |
| `src/device/hand_models.cpp` | the same demo's `hand_detection.cc` / `main.cc` | BSD-2-Clause, (c) 2025 Canaan | facts only: anchors, output layout, pad value, crop factor; own code |
| `src/device/face_model.cpp` | k230_linux_sdk `face_detect/src/face_detection.cc` | BSD-2-Clause, (c) 2024-2025 Canaan | facts only: pad values, score layout; own code |
| `src/device/camera.cpp` | k230_linux_sdk `vvcam/v4l2-drm` API | (SDK, BSD-2-Clause) | linked, not copied |
| nncase runtime | github.com/kendryte/nncase v2.11.0 | Apache-2.0 | linked (Buildroot `libnncase`) |
| zxing-cpp | github.com/zxing-cpp/zxing-cpp 2.3.0 | Apache-2.0 | linked (optional) |

LilyGO's T-Display-K230 repository carries no licence file: nothing is taken from it; it
was only read to learn how its camera app opens the GC2093 (v4l2-drm, 1280 x 720, rotation).

## On-board checklist

Run as root over ssh with the UI stopped unless a step says otherwise. The fake UI
`tools/fake_ui.py` (Python 3, in the image; copy it over) listens on the socket, prints
what athena-vision sends and sends a `mode` for what you type: `python3 fake_ui.py`, then
`g` (gestures), `p` (presence), `q` (QR), combinations like `gp`, or `off`. Start
athena-vision in a second shell.

1. `athena-vision --version` runs (libraries found). `ls /dev/video*` and
   `cat /sys/class/video4linux/video*/name`: note which is `vvcam-video.0.0`.
2. With the fake UI: on connect a `state` line `camera: off`; after the `mode` line
   `camera: on` within 1 s, then `state` every 5 s with `fps`. Record the fps for gestures
   alone, presence alone, and all three (expect >= 10 with gestures; report it).
   `{"t":"mode"}` turns it off: `camera: off` and the ISP stops (no `isp` CPU load).
3. Orientation: `--verbose 2`, hold a hand up: move it right (your right) - the logged x
   must grow; up - y must shrink. If not, try `--rotate 270` / `--mirror false` /
   `--rotate 0`; put the working values into `/etc/athena-pda/vision.conf`.
4. Shapes in the log: open hand, fist, pointing index, thumbs up, two fingers (other), at
   30, 50 and 80 cm, both hands. Note any that are wrong and at what distance.
5. Gestures from the fake UI's output: four swipes each way at a normal speed (one event
   each, no event for the hand coming back), a slow wave (none), palm held 1 s (one `palm`),
   fist (one `fist`), thumbs up (one `thumbs_up` after ~0.3 s, `hand` lines keep coming with
   `shape: thumbs_up`), pointing at two places (two `point`). `hand` lines <= 10 per second,
   one `shape: none` after the hand goes.
6. Presence (`{"t":"mode","presence":true}`): sit in front - `present: true` after ~2 s;
   leave - `present: false` after ~20 s.
7. QR (`{"t":"mode","qr":true}`): show the hub's Pair a phone code at 20-40 cm - one `qr`
   line with the `odysseus-pair:v1?...` text.
8. Kill the fake UI: athena-vision logs "the UI went away", the camera stops, and it
   reconnects when the UI is back. Kill athena-vision (SIGTERM): it exits, camera off.
9. Record for tuning: `--record-keypoints /tmp/t.jsonl`, do the gestures of step 5, copy
   the file to a PC, `athena-vision-replay --hands /tmp/t.jsonl`. Send the file (only
   keypoints: check with `head`) and the replay output with which gestures were intended
   and which fired; thresholds are tuned from it.
10. With the real UI (`athena-pda`): Hands-free on, the camera indicator shows, the cursor
    follows the hand, swipes change tabs; Hands-free off, the camera stops.
