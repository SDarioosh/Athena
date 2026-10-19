# barvis-sound progress

barvis-sound (formerly convo-ai / barvis-ai) is the first module of
[Barvis](https://github.com/SDarioosh/Barvis). Design: wearables (Amazfit T-Rex 3 Pro,
T-Watch Ultra recorder planned, XIAO ESP32-S3 pendant after it, Omi later) → local
Ubuntu server (Silero VAD, whisper.cpp, diarization, llama-server, SQLite) → MCP server → Barvis. See MISSION.md and AGENTS.md.
Nothing in the data path leaves the network.

_Last updated: 2026-10-19 (round 11: T-Watch Ultra milestone 1, bring-up firmware)_

## Status

| # | Item | Status | Where |
|---|------|--------|-------|
| 1 | Omi local-ingestion research | ✅ done | `docs/omi-ingestion.md` (PR: docs/redesign) |
| 2 | Frontend decision doc (Hermes vs Odysseus vs hybrid) | ✅ decided: **hybrid via MCP**, frontend **Odysseus fork (Barvis)** | `docs/frontend-decision.md` |
| 3 | Rewrite AGENTS.md / MISSION.md / PROGRESS.md | ✅ done | PR: docs/redesign |
| 4 | Ingestion API (auth, idempotent chunks, silence/max-duration stop) | ✅ done, tested | `server/` (PR: feature/server-pipeline) |
| 5 | Pipeline: reassembly, Silero VAD, whisper.cpp, diarization + enrollment, splitting, summaries, digest, audio deletion | ✅ done, tested | `server/` |
| 6 | SQLite storage, FTS5 + optional embeddings, read API with transcript links | ✅ done, tested | `server/` |
| 7 | Knowledge-layer MCP server (works with Hermes or Odysseus) | ✅ done, tested (PR #3) | `server/convo_server/mcp_server.py` |
| 7b | Wire into Hermes + Odysseus, side-by-side test | ✅ done (20 questions): Hermes 15/20, Odysseus 3/20 | `server/eval/`, results in `docs/frontend-decision.md` |
| 7c | Barvis: Odysseus fork fixed for our MCP tools, kept in sync with upstream | in progress: 9/20 (was 3/20), target ≥ 15/20; fork `SDarioosh/Barvis` (default branch `barvis`), PR SDarioosh/Barvis#1; HTTP annotations fix merged (SDarioosh/Barvis#5) | `docs/frontend-decision.md` (fork plan), Barvis `BARVIS.md` and `docs/barvis/roadmap.md` |
| 7d | Agent docs: AGENTS/CLAUDE/Antigravity files, rename to barvis-sound in prose, Barvis module contract | ✅ merged (PR #8, Barvis #2) | `AGENTS.md`, Barvis `docs/barvis/modules.md` |
| 7e | Cloud sandbox stack: whole system on CPU (Qwen3-4B, whisper base.en), fictional data | ✅ working: 50 tests pass, seed 8/8 summarized, eval raw 1/3 and Barvis 1/3 on questions 1, 4, 16; 2/3 and 3/3 in two runs with SDarioosh/Barvis#5 | Barvis `docs/barvis/sandbox.md` |
| 8 | Omi ingestion path (Android BLE relay) | optional, depends on how the XIAO goes | — |
| 9 | T-Rex 3 Pro Zepp test app (screen-off recording check) | merged (PR #4), **waiting for owner's watch test**; test server is running | `zepp-app/` |
| 9d | Watch voice profile (guided 60–90 s owner enrollment) | PR open, **needs the real watch** | `zepp-app/page/enroll.js`, `POST /v1/speakers/enroll.json` |
| 9b | Lock down host services: Tailscale-only access | **deferred** until the system is close to ready; LAN + Tailscale open for testing | — |
| 9c | Calendar/task integration for action items | later (wanted) | — |
| 11 | Own pendant: XIAO ESP32-S3 Sense (BLE → Android → server) | **after the T-Watch Ultra**, on the same firmware; plan written, some decisions still open | `docs/xiao-esp32s3-sense-plan.md` |
| 11b | LilyGO T-Watch Ultra recorder (same firmware as the XIAO, board layer) | **main focus** with the T-Rex; milestone 1 firmware **running on the owner's watch** (2026-10-19): rails, battery, RTC, SD, screen, buttons, haptics and recording work (mic timeout fix; stop early with BOOT+PWR again, `stop` or mute). Mic: raw level was nearly inaudible and muffled; now high-pass + T-Watch EQ (+9 dB 1.6 kHz, -6 dB 3.6 kHz) + 30 dB gain + limiter, speech ~-24 dBFS, owner says it sounds best; `gain`/`eq` on the console. Recordings copy off over USB (`tools/pull.py`). Still to check: SD_DET with the card out, `smoke_upload.py` run; next: milestone 2 (power baseline), milestone 3 tunes gain/EQ by transcription | `firmware/recorder/`, `docs/t-watch-ultra-plan.md` |
| 10 | End-to-end with real devices | not started | — |

## Done in this round

**Research**
- Omi: the official app and Omi cloud can't be made local (the audio-bytes webhook
  is sent from Omi's cloud; custom STT still sends transcripts to Omi). Recommendation:
  our own BLE relay over Omi's documented GATT protocol. Borrow from Chronicle (its
  RN BLE relay app and chunk-identity design) and `cgallic/omi-pendant-chronicle`;
  don't adopt Chronicle's backend (Mongo/Redis, Deepgram by default, duplicates ours).
- Frontend: recommendation is the **hybrid**: our SQLite is the source of truth,
  exposed through our own MCP server, with **Hermes** as the first frontend in a
  dedicated local-only profile. Odysseus stays a credible second frontend (better at
  tasks/CalDAV and phone PWA). Neither product's memory can answer "what did X say
  about Y last month" with transcript links on its own.

**Server (`server/`, Python 3.11+, FastAPI)**
- `POST /v1/ingest/chunk`: multipart audio + `device`, `session_id`, `sequence`,
  `timestamp`, `codec`; bearer token (constant-time compare, no default token);
  retries are idempotent; out-of-order chunks are fine. Returns `stop: true` with
  `reason` `silence` / `max_duration` / `client` / `idle`.
- Worker finalizes sessions (Stop, server stop, idle timeout), transcribes only VAD
  speech regions, diarizes, stores segments with absolute timestamps, deletes raw
  audio (kept on failure for retry), splits on > 3 min gaps (a new session within
  the gap extends the previous conversation), summarizes with llama-server
  (`json_schema`-constrained, thinking off), indexes FTS5 (+ embeddings if
  configured), and writes yesterday's digest.
- Read API: conversations, transcripts (`?start=&end=`), search, action items
  (+ status updates), digests, speakers / voice enrollment.
- 36 tests (fakes for whisper, LLM and speaker embeddings), ruff clean, also passes
  on Python 3.11.

**End-to-end check on this machine** (real components, public-domain speech
samples, no personal audio): `scripts/smoke_upload.py` sent 11 × 30 s Opus chunks
(JFK clip, a second speaker, 4 min silence, a third speaker) →
Silero VAD tracked speech/silence correctly → whisper.cpp large-v3-turbo (CUDA) →
ECAPA diarization labelled the enrolled owner and two other speakers correctly →
two conversations split at the 4-min gap → Qwen3.5-35B summaries with action items
and facts (~8 s each) → FTS search found them → raw audio deleted.
- Diarization tuning found during this run: 1–2 s whisper segments were too short
  for stable speaker embeddings (one speaker became 7); embedding ≥ 3 s windows with
  cluster threshold 0.45 fixed it.
- whisper.cpp was built at `~/tools/whisper.cpp` (CUDA, sm_86) with models
  `ggml-large-v3-turbo-q5_0.bin` and `ggml-base.en.bin`. It's not running as a
  service yet.

## Decisions so far

- Knowledge layer: **hybrid**. Our SQLite is the source of truth, exposed over MCP.
- Frontend: **Odysseus, forked as Barvis**, modified for our system and kept in sync with
  upstream. Chosen after the side-by-side test; fork plan in `docs/frontend-decision.md`.
- Focus: the **XIAO pendant** and the **T-Rex 3 Pro** (owned). Omi only if the XIAO falls short.
- Phone: **Android**, with Tailscale access to the server.
- Omi: the current consumer pendant (CV1 in Omi's firmware), not bought yet. The T-Rex 3 Pro
  comes first.
- Silence stop: **5 min** trailing silence (`CONVO_SILENCE_STOP_SECONDS=300`), 4 h max
  session. These are already the defaults.
- Timezone: **UTC** (`CONVO_TIMEZONE`, already the default).
- Retention: **keep full transcripts forever**. No pruning job.
- Calendar/tasks: action items stay in SQLite for now; a real calendar/task integration
  comes **later** and is wanted.
- Security: **as tight as possible; reachable only over Tailscale.** Services bind to
  loopback or the Tailscale interface, never `0.0.0.0`. That covers Odysseus' ChromaDB
  (8100), ntfy (8091), and llama-server (8082, no auth).
  **Deferred:** while testing, services stay open to the LAN and Tailscale. Lock down
  before the system goes into daily use.
- The old T-Watch `firmware/` is removed and `firmware-archive/` is deleted and ignored.
- Recorder (2026-10-19): **T-Watch Ultra first, XIAO later**, one shared ESP-IDF firmware
  (can be split later). Buttons as in `docs/t-watch-ultra-plan.md` §3, screen off until a
  button press, microSD overflow, T-Watch S3 left out for now. Late-chunk policy: finalise
  on `/end` + 24 h timeout, re-open on late chunks. Omi live-audio service: skip. A daily
  charge is OK, but battery life should be as long as possible without hurting recording
  quality.
- Pendant: the eventual plan is our own **XIAO ESP32-S3 Sense** pendant
  (`docs/xiao-esp32s3-sense-plan.md`). It sends audio over BLE to the Android app,
  has Wi-Fi off, and is tuned for battery life.

## Done in round 2 (PR #3, stacked on #2)

- MCP server (`convo-server mcp`, stdio for Hermes, token-protected HTTP for Odysseus).
  Its 10 tools include `search_transcripts` ("what did X say about Y on DATE") and
  `name_speaker`.
- Speaker naming: saying "Speaker 1 was Alice" renames her lines, redoes the summary and
  learns her voice for future recordings.
- Signed transcript links that open in a phone browser and jump to the moment.
- Verified: Qwen3.5-35B called the right tools with the right dates on 5/5 questions
  against real data. Odysseus' own MCP client code (mcp 1.x) connects to our server.

## Done in round 3

- **Frontend side-by-side** (`server/eval/bakeoff.py`, fictional data from
  `server/scripts/seed_demo.py`): Hermes 15/20 once tuned, Odysseus 3/20. Full table and
  failure analysis are in `docs/frontend-decision.md`.
- **MCP:** every non-empty tool result now asks for the source link, because neither
  frontend forwards MCP server instructions.
- **Live test server** for the watch test, from a `main` checkout at
  `~/projects/barvis-ai-wt/main`:
  - `convo-server serve` on `0.0.0.0:8000`, with `CONVO_PUBLIC_URL` set to the Tailscale IP.
  - `whisper-server` (large-v3-turbo) on `127.0.0.1:8178`.
  - Verified over Tailscale with a real chunk: upload, VAD, transcript, summary, link.
  - Not a service yet: it stops on reboot.

## Done in round 4

- Repo renamed to **barvis-sound** in prose. Code identifiers (`convo_server`,
  `convo-server`, `CONVO_*`, paths) are unchanged on purpose.
- `AGENTS.md`: "This is a Barvis module", the MCP tool contract, where work belongs,
  instruction files per tool. Added `CLAUDE.md` (imports `AGENTS.md`) and
  `.agents/rules/antigravity.md`. Root `README.md` written.
- Barvis got the module contract (`docs/barvis/modules.md`) and a barvis-sound module
  page.

## Done in round 5

- Agents now work in a cloud sandbox; the owner runs a "Server verification" section
  from each PR on the server (real model, real devices). `AGENTS.md` updated.
- The sandbox stack was built and run end to end. It found two Barvis MCP bugs (HTTP
  servers lose tool annotations, so read-only tools still need approval; stdio servers
  fail on every call). Tracked in Barvis `docs/barvis/roadmap.md`. Use MCP over HTTP.
- The sandbox also found a barvis-sound bug: a summary that fails (e.g. llama-server
  down) marks the conversation `failed`, and `convo-server summarize-pending` only retries
  `pending` ones (`Pipeline.summarize_pending`), so it never gets a summary. Fixed in
  round 6.
- Audio path checked in the sandbox: JFK clip → Silero VAD (8.1 s speech) → whisper.cpp
  `base.en` → transcript. Diarization not tested there (needs the SpeechBrain model).

## Done in round 6: MCP annotation contract test

- `test_tool_annotations_match_contract` (`server/tests/test_mcp.py`) reads the
  "MCP tool contract" table in `AGENTS.md` and checks every tool the MCP server lists:
  read-only tools have `readOnlyHint: true`, `update_action_item` and `name_speaker`
  have `readOnlyHint: false` and `destructiveHint: false`. A tool missing from the table
  (or listed but not served) fails the test. The code already matched the table; no
  annotations changed.

## Done in round 6: failed summaries are retried

- `convo-server summarize-pending` now also retries `failed` conversations, right away.
- The background worker retries a failed summary once its last attempt is
  `CONVO_SUMMARY_RETRY_SECONDS` old (default 300 s), oldest failure first, and stops
  that pass at the first retry that fails again (llama-server is most likely still
  down). So a down llama-server gets at most one retry request per 15 s worker pass,
  and each conversation at most one every 5 min. A failure moves the conversation to
  the back of the queue, so one broken conversation can't block the others.
- Tests: a summary that fails and then succeeds on retry; the worker not retrying
  inside the back-off window.

## Done in round 7: Barvis keeps MCP annotations over HTTP

- Barvis bug (sandbox finding 1): when Barvis connected to an MCP server over HTTP, it
  dropped each tool's annotations, so our `readOnlyHint: true` tools still needed
  approval on every call. Fixed in Barvis with one line (SDarioosh/Barvis#5, merged);
  nothing changed here. Our tool names, arguments, annotations and links are unchanged.
- Sandbox eval (questions 1, 4, 16; Qwen3-4B), two separate sandboxes: before the fix
  2/3 both times, with one approval per tool call; after, 2/3 and 3/3 with no approvals.
  Earlier sandbox run: 1/3. The scores differ by noise; what the fix changes is the
  approvals.
- Eval question for the owner: an answer that starts with "The task is incomplete: Model
  request failed." still scored PASS (question 4, in both sandboxes), because it also had
  the date and the link (Barvis sandbox finding 5). Should `eval/bakeoff.py` count that as a failure? Not changed yet.

## Done in round 8: watch app asks for the microphone

- First watch test: every test button failed with `create recorder: PERMISSION DENIED
  device:os.mic`. The app (0.1.1) now lists `device:os.mic` in `app.json` and asks for it
  at runtime before recording starts (`withMic` in `zepp-app/page/index.js`). Built with
  `zeus build` in the sandbox; still needs the real watch to confirm.
- With the mic fixed, every upload failed with `server: unknown request undefined`:
  zml treats an `ArrayBuffer` as a JSON object, so the watch sent `{"jsonrpc":"hmrpcv1"}`
  instead of the audio. Uploads now pass `contentType: 'bin'` (`shared/messaging.js`);
  `tests/messaging.test.mjs` loops zml's own messaging code to check the envelope arrives
  whole. The watch also shows the full reason when an upload or ping fails (0.1.2).
- Uploads now reach the server but fail with `422 could not decode audio` (ffmpeg:
  "Error opening input"). The Recorder's `.opus` file is not a format ffmpeg probes
  (likely raw Opus frames, no Ogg). The server now keeps undecodable uploads in
  `server/data/rejected/` (newest 20) so the owner can send a hex dump of the first bytes.
- The owner's hex dump answered it: `[u32 BE length][u32 BE final range][packet]` per
  packet (libopus `opus_demo` layout), TOC `0x58` = SILK wideband, mono, 60 ms. The server
  now wraps such uploads in Ogg Opus before ffmpeg (`convo_server/opus_packets.py`,
  `tests/test_opus_packets.py`). An incomplete last packet is dropped.

## Done in round 9: "Speaker 2 was Jenn" renames the speaker

- The owner said "/sound … Speaker 1 and Speaker 3 were both Shahyah and Speaker 2 was
  Jenn" and the model only repeated what they said; nothing was renamed. Reproduced in
  the sandbox: Barvis offered `name_speaker` on the `/sound` turn, but the model read the
  sentence as a claim to check and never called it.
- `INSTRUCTIONS` now has a "Naming speakers" paragraph (a statement of who a speaker was
  is a request to save it; call `name_speaker` once per label), and the tool description
  says the same. Sandbox, same message: before, only `search_conversations`; after,
  `search_conversations`, `get_conversation`, `name_speaker` (approved) and Speaker 1
  became Shahyah. The next model call hit the sandbox CPU's read timeout (HTTP 504), so
  Speakers 2 and 3 still need the server run.
- Needs the owner: paste the new `INSTRUCTIONS` into the Custom preset and rerun
  `barvis_setup.py` so `/sound` picks it up.

## Done in round 11: T-Watch Ultra milestone 1, bring-up test firmware

- `firmware/recorder/`: ESP-IDF 5.x project with the board layer from the plan (§6):
  `main/board.h` is the interface, `boards/twatch_ultra/board/` implements it. Shared code
  in `main/` (mic, test recorder, status screen, console, pure helpers); the XIAO becomes a
  second board directory later. Build: `idf.py -DBOARD=twatch_ultra
  -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.twatch_ultra" build` (CI does this
  on ESP-IDF v5.5.2, plus the host tests).
- Bring-up order as in plan §2.1: I2C → AXP2101 (DC1 on; GPS, LoRa, NFC, speaker, sensor
  and display rails off; SD rail only while the card is in use; 400 mA charge, 4.2 V;
  charge LED off; VBAT + TS ADC; LilyGO's fuel-gauge battery model written once; RTC
  backup cell charged) → XL9555 (haptic/display enables low, touch held in reset) →
  PCF85063A → DRV2605 → buttons. The rails are read back from the PMU and logged at boot
  and on `rails`. LoRa/NFC chip selects stay Hi-Z (plan §2.2).
- Expander pin question (plan §2.1) answered from the schematic: TP_RST = P10, SD_DET =
  P12, i.e. the hardware page's "GPIO10/12" are port names and the arduino board file's
  8/10 are the same pins as linear indices. `pins.h` keeps both readings; `exp` logs all
  16 inputs so the owner can confirm (SD_DET low with a card inserted).
- Buttons: PWR short press (AXP2101 IRQ on GPIO7) shows the status screen for 5 s on the
  CO5300 AMOLED (`espressif/esp_lcd_co5300` from the component registry, LilyGoLib init
  sequence, 410×502 portrait, text bands, no LVGL), then powers the display rail off.
  BOOT short press toggles mute with 1 buzz (recording) / 2 buzzes (muted); BOOT long
  press logs "sync now". BOOT held + PWR short, or `rec` on the USB console, records 60 s
  from the PDM mic (I2S PDM RX, 16 kHz mono 16-bit) to a WAV on the SD card.
- Time: RTC → system clock at boot; `time 2026-10-19T12:34:56Z` on the console sets
  both. BLE time sync comes later.
- No Wi-Fi and no BLE: neither is initialised, and the link map has no `libnet80211`,
  `libesp_wifi`, `libphy` or `libbt` (the project builds only main's dependencies).
- Host tests (`firmware/recorder/test/`, plain CMake + ctest, no ESP-IDF): WAV header,
  calendar/ISO-8601 parsing, PCF85063A BCD codec, font and text rendering.
- Needs the real watch (owner, Server verification section of the PR): boot log with the
  rail read-back, expander pin values, status screen, mute + buzz, battery %, RTC across a
  reboot, the 60 s WAV through the server pipeline. Likely first-flash tweaks: PDM slot
  (left/right), display init (LilyGoLib runs its sequence twice), SD_DET polarity,
  ERM vs LRA haptics.

## Done in round 10: voice profile on the watch

- The watch app (0.2.0) has a **Voice** button: a guided recording of 8 short prompts
  (about 75 s; Done after 45 s, auto-stop at 2 min) through the same microphone as daily
  recordings. The phone posts it to the new `POST /v1/speakers/enroll.json` (base64
  audio in JSON, because the Side Service's fetch only sends strings) with
  `is_owner=true`, named with the new "Your name" setting (default `Me`).
- Logic in `zepp-app/shared/enroll.js` with `tests/enroll.test.mjs`; the device helpers
  moved unchanged from `page/index.js` to `utils/device.js` so both pages share them.
  Server test: `test_enroll_speaker_json_for_the_watch`.
- `zeus build` doesn't run in this sandbox (it fetches Zepp's device list over plain
  HTTP, which the proxy blocks); all pages bundle with esbuild (`@zos/*` external).
- **Needs the real watch** (not verifiable here):
  1. The page opens from **Voice**, prompts are readable on both screen sizes, buttons
     fit (44 mm and 48 mm).
  2. One ~75 s Recorder file works (the test app only used 30 s files) and the screen
     stays on for the whole recording.
  3. Upload of a ~100 KB file over BLE completes (time it), and the watch shows
     "Saved as … (N s of speech)".
  4. `curl -H "Authorization: Bearer $CONVO_INGEST_TOKEN" http://127.0.0.1:8000/v1/speakers`
     lists the owner with `"is_owner": true`.
  5. A normal recording afterwards labels the owner's lines with the owner's name.

## Next

1. **Owner: T-Watch Ultra milestone 1 test** (`firmware/recorder/README.md`, PR Server
   verification). Then milestone 2: power baseline (plan §7.3, §8): current for board
   idle, mic on, SD write, screen on; whether I2S PDM RX allows light sleep; whether the
   AXP2101 reports discharge current (it has no battery-current ADC channel in its
   register map, so expect a USB meter or PPK2); full-discharge run.
2. **Owner: T-Rex watch test.** The steps are in `zepp-app/README.md`. Server URL
   `http://100.106.28.112:8000`; the token is in that checkout's `server/.env`.
3. **Barvis fork:** citations from 9/20 to ≥ 15/20 (Barvis `docs/barvis/roadmap.md`).
4. **Owner: server verification** of the failed-summary retry (steps in its PR), and of
   SDarioosh/Barvis#5 (full eval on the test instance; expect no approvals for read-only
   tools and a score ≥ 9/20; steps in that PR).
5. Build the real T-Rex recording app from the watch-test results.
6. systemd units for `whisper-server`, `convo-server serve`, and MCP over HTTP.
7. Owner voice enrollment from the watch (round 10), plus speaker-threshold check on
   real audio.
8. T-Watch Ultra recorder: milestones 3+ (`docs/t-watch-ultra-plan.md` §8) once the
   power baseline says go.
9. XIAO pendant, after the T-Watch Ultra: a second `boards/` directory on the same firmware.

## Open questions for you

The questions from round 2 were answered on 2026-10-18 and are under "Decisions so far".
Still open:
- T-Watch Ultra: everything under "Needs the real watch" above. Also: does the battery
  take a 4.2 V charge target (set conservatively; LilyGoLib asks the chip for 4.288 V)?
- Barvis fork: enable GitHub Actions on `SDarioosh/Barvis` (Actions tab) so CI runs on PRs.
- The remaining XIAO pendant decisions in `docs/xiao-esp32s3-sense-plan.md` §10
(battery/form factor, fallback board, extra parts, microSD). They wait until XIAO work
starts, after the T-Watch Ultra.

## Notes

- The previous untracked `server/` prototype (hardcoded token, broken VAD call) was
  replaced. A copy is in `~/barvis-ai-backups/`.
- The ECAPA speaker model is downloaded once from Hugging Face; model weights only,
  no data goes out.
