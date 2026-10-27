# Zoom bot spike (Phase 0): findings

Status: **template, prepared by lane 0A; the owner's run fills it in.** Fields to fill are
marked `____`. Plan: [zoom-meeting-bot-plan.md](zoom-meeting-bot-plan.md), Phase 0, as
updated in PR #118 (the bot is mainly for other people's meetings) and by the owner's rule
that the bot never asks for recording permission.

The spike code is in [`meeting-bot/spike/`](../meeting-bot/spike/) on branch
`feature/meeting-bot-0a`. It is throwaway: Phase 1 lanes take the findings, not the code.
The Zoom Meeting SDK is proprietary and never enters the repo: the owner downloads it,
and the build reads it from a folder outside git.

| File | What it is |
| --- | --- |
| `cpp/athena_spike.cpp`, `cpp/CMakeLists.txt` | Minimal C++ bot, builds with `-DZOOM_SDK_DIR=...` |
| `python/athena_spike.py` | The same bot through the `py-zoom-meeting-sdk` binding |
| `sign_jwt.py` | Signs the SDK JWT from `CONVO_ZOOM_SDK_KEY` / `CONVO_ZOOM_SDK_SECRET` (stdlib only) |
| `run.sh` | Starts one bot; with `ATHENA_SPIKE_PULSE=1` also a private PulseAudio for that run only |
| `Dockerfile` | Ubuntu 24.04 image; the SDK comes in as `--build-context zoomsdk=<folder>` |
| `measure.sh` | CPU (`pidstat 5`), peak RSS, start → `STATE recording`, leftovers after exit |
| `wer.py` | Opus 32 kbps vs WAV word error rate through whisper-server `/inference` |
| `tests/` | Tests for `sign_jwt.py`, `measure.sh` and `wer.py` (no SDK needed) |

**What the bot does.** Joins one meeting by id and passcode as "Athena", video off, and
never subscribes to video or screen share. It records whatever per-participant raw audio
Zoom delivers: one raw PCM file per Zoom user id (`track-<id>.pcm`, s16le mono) and
`participants.json` (user id → display name, sample rate, bytes). Chat messages go to
`chat.jsonl` (`{at, from, zoom_user_id, text, to_all}`). It leaves after `--minutes`, when
the meeting ends, when removed, or on SIGINT/SIGTERM. It never asks for recording
permission, posts nothing in chat and takes no commands. If Zoom delivers no raw audio it
stays and says so. Status lines on stdout:

```text
STATE starting
STATE waiting_room
STATE recording
STATE recording no_audio      (in the meeting, but no raw audio for --no-audio-seconds)
STATE ended <reason>          (duration | signal | meeting_ended_<MeetingEndReason>)
STATE failed <reason>         (sdk_auth_<n> | join_failed_<MeetingFailCode> | ...)
```

**Unverified.** All bot code was written in a cloud sandbox without the SDK, a Zoom account
or a meeting, from the Linux SDK API as known from Zoom's public Linux samples and the
binding's sample bot. The C++ bot compiled and ran only against a stand-in SDK made in the
sandbox (stub headers and a fake `libmeetingsdk.so` that simulates a meeting; not
committed). That checked the bot's own logic (states, tracks, sidecar, chat log, leaving,
signals), `run.sh`, `measure.sh` and the Docker build, **not** the real SDK's names or
behaviour. Expect to fix some names: event interfaces gain pure virtual methods between
SDK releases. If the compiler says a class is abstract, add an empty override for each
method it lists, copying the signature from the SDK header. Note every fix in
[§ 10](#10-fixes-to-the-spike-code).

## 1. Zoom's rules for a bot in other people's meetings (answer first)

The bot is mainly for meetings hosted by other people, in other Zoom accounts. These rules
decide whether the plan works at all, so check them before measuring anything.

What lane 0A found on 2026-10-27 (public pages, not legal advice; recheck, they move):

- **Zoom's Meeting SDK pages say it is not for bots.** The Linux Meeting SDK page and the
  Web SDK README both say: "The Meeting SDK is designed for human use cases and does not
  support bots or AI notetakers", and point to Zoom RTMS for notetakers and realtime media
  ([Linux SDK page](https://developers.zoom.us/docs/meeting-sdk/linux),
  [Web SDK README](https://cdn.jsdelivr.net/npm/@zoom/meetingsdk@6.0.0/README.md)). A Zoom
  forum moderator knew of no Linux-specific rule
  ([forum](https://devforum.zoom.us/t/question-on-linux-sdk-usage-policy/141545)). This is
  the biggest open risk of the plan: the owner needs Zoom's answer for this use (a personal
  assistant joining as a named guest for its owner).
- **Meetings in other accounts need an on-behalf-of (OBF) token since 2026-03-02.** A
  Meeting SDK app joining a meeting hosted by an external account must join tied to a Zoom
  user who authorized the app (OAuth, scope `user:read:token`) and who is **already in the
  meeting**. Token: `GET https://api.zoom.us/v2/users/me/token?type=onbehalf&meeting_id=<id>`.
  If that user leaves, Zoom's FAQ says the SDK session ends. ZAK tokens (join as the user)
  are the other accepted option. Meetings in the app owner's own account need only the JWT.
  Minimum SDK version 5.17.5; from 6.6.10 a join without the user present fails with
  `MEETING_FAIL_AUTHORIZED_USER_NOT_INMEETING` (retry after 1–5 s)
  ([OBF FAQ](https://developers.zoom.us/docs/meeting-sdk/obf-faq/),
  [OBF transition blog](https://developers.zoom.us/blog/transition-to-obf-token-meetingsdk-apps/)).
  So Athena would join **on behalf of the owner, only while the owner is in the meeting**,
  and is dropped when the owner leaves: the plan's conditional leave rule applies, and
  Phase 1B needs the owner's one-time Zoom sign-in.
- **Review.** Apps that add the `user:read:token` scope must submit a review request; apps
  used only inside the developer's own account need no Marketplace submission (blog above).
  Whether an unpublished app authorized only by the owner may fetch OBF tokens for meetings
  in other accounts is unclear; one developer could not test external joins with an
  unpublished app ([forum](https://devforum.zoom.us/t/how-to-test-obf-token-implementation-with-external-zoom-accounts-if-unpublished-app-cant-be-installed-outside-developer-account/145012)).
  Start the review early; it can take weeks.
- **Raw audio needs recording rights.** Forum answers say a guest gets raw audio only as
  host, co-host, with the host's recording permission, or with a host-issued
  local-recording join token
  ([forum](https://devforum.zoom.us/t/pricing-feasibility-validation-rtms-vs-linux-sdk-for-external-meeting-capture/141332)).
  The bot never asks (owner's rule), so in other people's meetings expect `STATE recording
  no_audio` unless the host grants recording on their own. Verify on the real run.

To fill in (date and where each answer came from):

| Question | Answer | Source, date |
| --- | --- | --- |
| Does Zoom allow this use of the Meeting SDK (personal assistant guest, owner present)? | ____ | ____ |
| Is Marketplace review or publishing needed to join other accounts' meetings? Submitted on, approved on | ____ | ____ |
| Must the bot join with an OBF token (or ZAK) from the owner's account for other accounts' meetings? | ____ | ____ |
| Does Zoom end the bot's session when the owner leaves? (seen in test B) | ____ | ____ |
| Does Zoom deliver raw audio to the bot without any recording permission? (test B) | ____ | ____ |
| Own-account meetings: JWT only, no OBF? (test A) | ____ | ____ |
| Is Zoom RTMS a usable fallback (plan, cost, public endpoint)? | ____ | ____ |

## 2. Meeting in another account

Filled from test B ([Owner steps](#owner-steps), step 8): a meeting hosted from a different
Zoom account, the owner joined first.

| Question | Answer |
| --- | --- |
| How "Athena" shows in the host's participant list (name, any "app" / "on behalf of" label, avatar) | ____ |
| Any banner, notice or prompt the host or participants see when Athena joins | ____ |
| Any recording prompt shown to the host (the bot never requests one; expect none) | ____ |
| Did raw audio arrive? (`participants.json` bytes > 0 / `STATE recording no_audio`) | ____ |
| `CanStartRawRecording` result logged (`LOG raw audio not allowed by Zoom (...=N)`) | ____ |
| Join needed a token from the owner's account (OBF/ZAK)? Without it, the error was | ____ |
| When the owner left: bot dropped? after how many seconds? `STATE ended ...` line | ____ |
| Waiting room: did the host have to admit Athena? | ____ |
| Chat events arrived (`chat.jsonl` lines)? | ____ |

## 3. SDK version and size

| Item | Value |
| --- | --- |
| SDK file downloaded (name, date) | ____ |
| SDK version (`version.txt` or the file name) | ____ |
| Download size | ____ |
| Unpacked size (`du -sh ~/zoom-sdk`) | ____ |
| Main library size (`ls -la ~/zoom-sdk/libmeetingsdk.so`) | ____ |
| Docker image size (`docker image ls athena-meeting-spike`) | ____ |
| At or above Zoom's minimum version (5.17.5)? | ____ |

## 4. What the SDK needs in the process

Test from [Owner steps](#owner-steps), step 7, inside the container (no X server, no
`DISPLAY`).

| Setup | Joins? | Raw audio? | Notes |
| --- | --- | --- | --- |
| No display, no PulseAudio (`ATHENA_SPIKE_PULSE=0`) | ____ | ____ | ____ |
| No display, private PulseAudio (`ATHENA_SPIKE_PULSE=1`) | ____ | ____ | ____ |

- Needs a display / Xvfb: ____ (Zoom's headless Linux sample runs without one)
- Needs PulseAudio or another audio device: ____ (Zoom's sample starts PulseAudio with a null
  sink and writes `~/.config/zoomus.conf`; if needed, the bot starts it inside its own scope)
- Other helpers (D-Bus, crash handler, extra processes it spawns; see `measure.sh`
  "Processes seen"): ____
- Files it leaves outside its output folder (`~/.zoomsdk/logs`, `~/.config/pulse`, ...): ____

## 5. C++ or Python binding

| | C++ (Zoom SDK directly) | Python (`py-zoom-meeting-sdk`) |
| --- | --- | --- |
| License of our code's dependency | Only the Zoom SDK (Zoom's terms) | Binding: PyPI metadata says "BSD License"; no LICENSE file in the GitHub repo when checked (2026-10-27) |
| Project status | Zoom's own Linux samples (MIT), raw-recording sample pinned to SDK 6.6.10 in its `version.txt` | PyPI `zoom-meeting-sdk` 0.0.27 (2026-03-02), "Development Status :: 4 - Beta"; README: early development, limited support |
| Where the SDK binary comes from | Owner's download | PyPI wheels **bundle their own copy of libmeetingsdk** (~207 MB), redistributed by a third party, version not stated. The spike builds the binding from source against the owner's SDK instead (`WITH_PYTHON=1`) |
| Builds against our SDK version? | ____ | ____ |
| Joined and recorded in test A? | ____ | ____ |
| Crashes, hangs, stuck exits seen | ____ | ____ |
| CPU / RSS (from § 6) | ____ | ____ |
| Lines of code for the same behaviour | ~550 | ~300 |
| Upgrade effort when Zoom ships a new SDK | Fix overrides, rebuild | Wait for or patch the binding, then rebuild |

Not verified here: the binding's real API on the owner's SDK version, whether its build
from source still works, and who maintains it beyond the one author. Lane 0A's leaning,
before any run: **C++**, since nothing sits between us and Zoom's SDK and no third-party
copy of Zoom's binary is involved, unless the run shows the binding is clearly more
reliable. Decision: ____

## 6. Measurements (30-minute meeting)

`measure.sh` rows (100 % CPU = one core; RSS = max VmHWM over the bot's processes; start →
recording = process start to the first `STATE recording` line). Test A: own meeting, 2–3
fictional participants talking most of the time.

| Run | Duration (min) | CPU avg % | CPU peak % | Peak RSS MB | Start → recording s | Left behind |
| --- | --- | --- | --- | --- | --- | --- |
| cpp, test A | ____ | ____ | ____ | ____ | ____ | ____ |
| python, test A | ____ | ____ | ____ | ____ | ____ | ____ |

- Disk per speaking participant per minute (raw PCM; Opus 32 kbps would be ~0.24 MB): ____
- More than 2 cores sustained? ____
- Start → in meeting over 30 s? ____ (if yes, § 9 proposes a standby process)
- After exit: `docker ps`, `ss -tlnp`, `systemd-cgtop` show nothing new? ____

## 7. Opus 32 kbps vs WAV

`wer.py` on the fixture tracks of test A (each fictional participant reads their script in
[Owner steps](#owner-steps), step 6), through the server's whisper-server and model.

| Track | Words | WAV WER % | Opus 32k WER % | Δ (Opus − WAV) pp | Opus kbps |
| --- | --- | --- | --- | --- | --- |
| ____ | ____ | ____ | ____ | ____ | ____ |
| **all** | ____ | ____ | ____ | ____ | |

Whisper model on the server: ____. Opus costs accuracy (Δ more than ~1 pp)? ____ → Phase 1
codec default (`CONVO_MEETING_TRACK_CODEC`): ____

Sandbox plumbing check only (not a quality result): three synthetic espeak-ng voices
reading fictional lines, whisper.cpp `base.en` on CPU, jiwer: WAV 6.5 %, Opus 32k 9.3 %
(108 words). Robotic TTS voices exaggerate codec loss; the real number comes from test A.

## 8. Gate to Phase 1 (from the plan)

- [ ] Joins as "Athena" without a display server: ____
- [ ] Each participant's audio arrives with their name (`participants.json`): ____
- [ ] Chat events arrive (`chat.jsonl`): ____
- [ ] Not more than 2 cores sustained (else it is a gate): ____
- [ ] Start → joined ≤ 30 s, or a standby proposal in § 9: ____
- [ ] If a fake audio device is needed: PulseAudio runs only inside the bot's scope: ____
- [ ] Zoom's rules (§ 1) allow joining the meetings the owner needs: ____

## 9. Decision

- Binding: ____ (why)
- Audio device: ____
- Join path for other accounts' meetings (OBF from the owner's sign-in / review status): ____
- Leave when the owner leaves: ____
- Track codec: ____
- Cold start: on demand / standby process (≤ 50 MB RAM, no CPU while waiting): ____
- Changes the plan needs (as a separate docs PR): ____

## 10. Fixes to the spike code

Names or signatures that differed from the real SDK (so Phase 1A starts from the truth):

| File | Was | Real SDK |
| --- | --- | --- |
| ____ | ____ | ____ |

## Owner steps

Everything below runs on the owner's machine with fictional meetings only. Nothing here
touches production Odysseus or Athena's running services. Tokens stay in shell variables,
never in files in the repo.

**0. Zoom's answer on bots** (§ 1). Ask Zoom (developer support or forum) whether this use
is allowed and which join path they expect. Fill in § 1.

**1. Zoom app.** In the Zoom App Marketplace, create a General app with the Meeting SDK
(Embed) feature on. Copy its Client ID and Client Secret into the `server/.env` of your
Athena install only:

```bash
CONVO_ZOOM_SDK_KEY=<client id>
CONVO_ZOOM_SDK_SECRET=<client secret>
```

For other accounts' meetings, also add the OAuth scope `user:read:token`, add a redirect
URL (for the spike, `http://localhost:8765/zoom`), and submit the review request now if
Zoom asks for one. Note the dates in § 1.

**2. SDK download.** Download the Meeting SDK for Linux (x86_64) from the app's page to
`~/zoom-sdk`, outside every git checkout, and record § 3:

```bash
mkdir -p ~/zoom-sdk && tar -xf ~/Downloads/zoom-meeting-sdk-linux_x86_64-*.tar.xz -C ~/zoom-sdk --strip-components=1
ls ~/zoom-sdk; cat ~/zoom-sdk/version.txt 2>/dev/null; du -sh ~/zoom-sdk; ls -la ~/zoom-sdk/libmeetingsdk.so
test -f ~/zoom-sdk/h/zoom_sdk.h && echo "SDK layout ok"
```

Expected: `h/`, `libmeetingsdk.so`, `qt_libs/`, "SDK layout ok". (If the archive has no top
folder, drop `--strip-components=1`.)

**3. Spike checkout** (a separate clone, so no deployed worktree changes):

```bash
git clone -b feature/meeting-bot-0a https://github.com/SDarioosh/Athena ~/athena-spike
cd ~/athena-spike/meeting-bot/spike
python3 -m venv ~/athena-spike/.venv && ~/athena-spike/.venv/bin/pip install -q pytest jiwer
~/athena-spike/.venv/bin/python -m pytest -q tests     # 17 passed (needs ffmpeg)
```

**4. Build the image** (the SDK enters only through the build context):

```bash
cd ~/athena-spike
docker buildx build --load -t athena-meeting-spike --build-context zoomsdk="$HOME/zoom-sdk" meeting-bot/spike
docker buildx build --load -t athena-meeting-spike:py --build-arg WITH_PYTHON=1 \
  --build-context zoomsdk="$HOME/zoom-sdk" meeting-bot/spike
git status --short                # must show nothing: no SDK file in git
docker image ls athena-meeting-spike
```

Expected: both builds finish. A build that stops at "missing libraries" lists the
packages to add to the Dockerfile's runtime `apt-get` line; a compile error lists names to
fix (note them in § 10). If the Python build fails, note why in § 5 and continue with C++.

**5. Credentials for the run:**

```bash
export $(grep -E '^CONVO_(ZOOM_SDK_KEY|ZOOM_SDK_SECRET|WHISPER_SERVER_URL)=' /path/to/athena/server/.env | xargs)
python3 meeting-bot/spike/sign_jwt.py >/dev/null && echo "JWT ok"
mkdir -p ~/athena-spike/out && chmod 777 ~/athena-spike/out
```

**6. Test A: own meeting, 30 minutes, measured.** Host a meeting from your account with
2–3 fictional participants on other devices, each named (for example "Fictional Alice",
"Fictional Bob", "Fictional Carol"). Each reads their script below once, then they talk
freely, overlapping now and then. Note the meeting id and passcode, then:

```bash
cd ~/athena-spike/meeting-bot/spike
MID=<meeting id>; PW=<passcode>
docker run -d --name athena-spike --init -e CONVO_ZOOM_SDK_KEY -e CONVO_ZOOM_SDK_SECRET \
  -e ATHENA_SPIKE_PULSE=0 -v ~/athena-spike/out:/out athena-meeting-spike \
  -- --meeting-id "$MID" --passcode "$PW" --minutes 30 --out /out
mkdir -p results; docker logs -f athena-spike > results/cpp-a.log 2>&1 &
sudo ./measure.sh -l "cpp, test A" -i 5 -p "$(docker inspect -f '{{.State.Pid}}' athena-spike)" -L results/cpp-a.log
docker rm athena-spike; docker ps; ss -tlnp | grep -c athena || true
cat ~/athena-spike/out/participants.json; wc -l ~/athena-spike/out/chat.jsonl
```

Expected: `STATE recording` within seconds of the host admitting Athena, a
`track-<id>.pcm` per participant with their names in `participants.json`, the chat lines
someone typed in `chat.jsonl`, `STATE ended duration` after 30 minutes, a row in
`results/measurements.md` with "Left behind: none", and no container or socket left. If
it says `STATE recording no_audio` with `ATHENA_SPIKE_PULSE=0`, run step 7 first. Copy the
row into § 6, then repeat with the Python image (`athena-meeting-spike:py` and `--python`
right after the image name, output to `~/athena-spike/out-py`) for the second row.

Scripts (fictional, also the WER references; save each as `<name>.txt`):

- Alice: "Thanks for joining everyone. Today we should decide on the launch date for the
  fictional garden app. I think the second week of March works, as long as the design team
  finishes the onboarding screens by the end of February."
- Bob: "That sounds fine to me. I can have the payment flow tested by then, but I need the
  final price list from marketing first. Can someone send me the spreadsheet with the
  subscription tiers?"
- Carol: "I will send the spreadsheet after this call. One more thing, the beta testers
  asked for a dark mode, so let us add that to the backlog and look at it again next
  Tuesday."

**7. Display and audio device check** (§ 4): two short joins to the same meeting, no
display in the container either time:

```bash
for pulse in 0 1; do
  docker run --rm --init -e CONVO_ZOOM_SDK_KEY -e CONVO_ZOOM_SDK_SECRET -e ATHENA_SPIKE_PULSE=$pulse \
    -v ~/athena-spike/out:/out athena-meeting-spike \
    -- --meeting-id "$MID" --passcode "$PW" --minutes 2 --no-audio-seconds 20 --out /out/pulse-$pulse
done
```

Expected: either both record (then no PulseAudio is needed), or only `pulse-1` does.

**8. Test B: meeting hosted from a different Zoom account** (§ 2). Use a second Zoom
account you control (for example a free account on another e-mail). Host from it on
another device and join it yourself first, signed in to your main account. Get an OBF
token for your main account (the spike does the OAuth by hand; Phase 1B automates it):

```bash
# a) open in a browser signed in to your MAIN account, approve, copy `code=` from the redirect URL
echo "https://zoom.us/oauth/authorize?response_type=code&client_id=$CONVO_ZOOM_SDK_KEY&redirect_uri=http://localhost:8765/zoom"
CODE=<code from the redirect URL>
# b) access token (stays in this shell)
ACCESS=$(curl -s -u "$CONVO_ZOOM_SDK_KEY:$CONVO_ZOOM_SDK_SECRET" -X POST \
  "https://zoom.us/oauth/token?grant_type=authorization_code&code=$CODE&redirect_uri=http://localhost:8765/zoom" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
# c) OBF token for this meeting (you must be in the meeting now)
MID=<meeting id>; PW=<passcode>
export ATHENA_SPIKE_OBF_TOKEN=$(curl -s -H "Authorization: Bearer $ACCESS" \
  "https://api.zoom.us/v2/users/me/token?type=onbehalf&meeting_id=$MID" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
cd ~/athena-spike/meeting-bot/spike
docker run --rm --init -e CONVO_ZOOM_SDK_KEY -e CONVO_ZOOM_SDK_SECRET -e ATHENA_SPIKE_OBF_TOKEN \
  -e ATHENA_SPIKE_PULSE=<0 or 1 from step 7> -v ~/athena-spike/out:/out athena-meeting-spike \
  -- --meeting-id "$MID" --passcode "$PW" --minutes 10 --no-audio-seconds 30 --out /out/test-b
```

(If Zoom's OAuth or token endpoints answer differently, follow their current docs and note
it in § 1.) During the run: look at the host's screen (participant list, banners, any
prompt) and fill § 2; don't grant recording, the bot never asks. After a few minutes leave
the meeting with your main account and note whether and when the bot gets
`STATE ended ...`. Then run the bot once more **without** `-e ATHENA_SPIKE_OBF_TOKEN` and
note the `STATE failed ...` line.

**9. Opus vs WAV** (§ 7), with the server's whisper-server (the URL from your Athena
`.env`, `CONVO_WHISPER_SERVER_URL`; nothing else is touched):

```bash
cd ~/athena-spike/meeting-bot/spike
cat ~/athena-spike/out/participants.json          # which track id is which name
~/athena-spike/.venv/bin/python wer.py --whisper-url "$CONVO_WHISPER_SERVER_URL" --out results/wer.md \
  --track ~/athena-spike/out/track-<alice id>.pcm alice.txt \
  --track ~/athena-spike/out/track-<bob id>.pcm bob.txt \
  --track ~/athena-spike/out/track-<carol id>.pcm carol.txt
```

A track holds everything that person said in the meeting, so first cut each to just the
script reading, for example
`ffmpeg -f s16le -ar <sample_rate> -ac 1 -i track-<id>.pcm -ss <start s> -t <seconds> alice.wav`,
and pass `alice.wav` instead. Copy the table into § 7. (`pip install jiwer` for the jiwer
engine; without it the script uses its own edit distance.)

**10. Clean up.** The image contains the SDK and the output folder holds meeting audio:

```bash
docker image rm athena-meeting-spike athena-meeting-spike:py
rm -rf ~/athena-spike/out ~/athena-spike/out-py
```

Then paste §§ 1–9 into the PR (or commit this file filled in on the branch).
