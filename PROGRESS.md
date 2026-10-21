# odysseus-sound progress

odysseus-sound (formerly convo-ai / barvis-ai / barvis-sound) is the first module of
[Odysseus](https://github.com/SDarioosh/Barvis), our Odysseus fork. Design: wearables
(Amazfit T-Rex 3 Pro, T-Watch Ultra recorder planned, XIAO ESP32-S3 pendant after it, Omi later) → local
Ubuntu server (Silero VAD, whisper.cpp, diarization, llama-server, SQLite) → MCP server → Odysseus. See MISSION.md and AGENTS.md.
Nothing in the data path leaves the network.

_Last updated: 2026-10-21 (notes from voice notes: "create a note …", "add to my hardware notes …"; finished T5 lists archive after 10 minutes; round 17: to-do lists for voice-note items; T-Watch Ultra on the owner's watch, bug hunt; plain questions reach the module in Odysseus; T5 E-Paper Pro PDA firmware, milestones 0–3 on the owner's board; T5 PDA PC relay side built)_

## Status

| # | Item | Status | Where |
|---|------|--------|-------|
| 1 | Omi local-ingestion research | ✅ done | `docs/omi-ingestion.md` (PR: docs/redesign) |
| 2 | Frontend decision doc (Hermes vs Odysseus vs hybrid) | ✅ decided: **hybrid via MCP**, frontend **Odysseus fork** (called Barvis until 2026-10-19, now Odysseus) | `docs/frontend-decision.md` |
| 3 | Rewrite AGENTS.md / MISSION.md / PROGRESS.md | ✅ done | PR: docs/redesign |
| 4 | Ingestion API (auth, idempotent chunks, silence/max-duration stop) | ✅ done, tested | `server/` (PR: feature/server-pipeline) |
| 5 | Pipeline: reassembly, Silero VAD, whisper.cpp, diarization + enrollment, splitting, summaries, digest, audio deletion | ✅ done, tested | `server/` |
| 6 | SQLite storage, FTS5 + optional embeddings, read API with transcript links | ✅ done, tested | `server/` |
| 7 | Knowledge-layer MCP server (works with Hermes or Odysseus) | ✅ done, tested (PR #3) | `server/convo_server/mcp_server.py` |
| 7b | Wire into Hermes + Odysseus, side-by-side test | ✅ done (20 questions): Hermes 15/20, Odysseus 3/20 | `server/eval/`, results in `docs/frontend-decision.md` |
| 7c | Odysseus fork fixed for our MCP tools, kept in sync with upstream | in progress: 9/20 (was 3/20), target ≥ 15/20; fork `SDarioosh/Barvis` (default branch `barvis`), PR SDarioosh/Barvis#1; HTTP annotations fix merged (SDarioosh/Barvis#5) | `docs/frontend-decision.md` (fork plan), hub `FORK.md` and `docs/fork/roadmap.md` |
| 7d | Agent docs: AGENTS/CLAUDE/Antigravity files, rename to barvis-sound in prose, module contract | ✅ merged (PR #8, SDarioosh/Barvis#2) | `AGENTS.md`, hub `docs/fork/modules.md` |
| 7e | Cloud sandbox stack: whole system on CPU (Qwen3-4B, whisper base.en), fictional data | ✅ working: 50 tests pass, seed 8/8 summarized, eval raw 1/3 and Odysseus fork 1/3 on questions 1, 4, 16; 2/3 and 3/3 in two runs with SDarioosh/Barvis#5 | hub `docs/fork/sandbox.md` |
| 8 | Omi ingestion path (Android BLE relay) | optional, depends on how the XIAO goes | — |
| 9 | T-Rex 3 Pro Zepp test app (screen-off recording check) | merged (PR #4), **waiting for owner's watch test**; test server is running | `zepp-app/` |
| 9d | Watch voice profile (guided 60–90 s owner enrollment) | PR open, **needs the real watch** | `zepp-app/page/enroll.js`, `POST /v1/speakers/enroll.json` |
| 9b | Lock down host services: Tailscale-only access | **deferred** until the system is close to ready; LAN + Tailscale open for testing | — |
| 9c | Calendar/task integration for action items | module side built (kinds, times, JSON listing, edits, named to-do list: "add milk to my shopping list" → `list_name` `shopping`; notes: "create a note …" → kind `note` with `note_title`, "add to my hardware notes …" → `list_name` `hardware`); hub files voice-note items into the Odysseus calendar, to-dos and notes, items with a list onto that to-do list, notes with a name onto those notes (coordinated hub PRs); **needs owner verification on real voice notes** | `server/convo_server/{llm,dates,store,mcp_server}.py` |
| 11 | Own pendant: XIAO ESP32-S3 Sense (BLE → Android → server) | **after the T-Watch Ultra**, on the same firmware; plan written, some decisions still open | `docs/xiao-esp32s3-sense-plan.md` |
| 11b | LilyGO T-Watch Ultra recorder (same firmware as the XIAO, board layer) | **main focus** with the T-Rex; milestone 1 firmware **running on the owner's watch** (2026-10-19): rails, battery, RTC, SD, screen, buttons, haptics and recording work (mic timeout fix; stop early with BOOT+PWR again, `stop` or mute). Mic: raw level was nearly inaudible and muffled; now high-pass + T-Watch EQ (+9 dB 1.6 kHz, -6 dB 3.6 kHz) + 30 dB gain + limiter, speech ~-24 dBFS, owner says it sounds best; `gain`/`eq` on the console. Recordings copy off over USB (`tools/pull.py`). SD_DET confirmed (P12 LOW with card, HIGH without; expander question §2.1 answered); RTC set, files get dated names. Still to check: one recording of fictional speech through `smoke_upload.py` on the server (owner). **Round 12: BLE upload path built, not yet on hardware**: BOOT+PWR records an Opus session (libopus, 16 kbps) as 30 s Ogg/Opus chunks into a queue on the SD card that survives reboots; NimBLE GATT service drains it (protocol `docs/twatch-ble-protocol.md`); `tools/ble_relay.py` is the PC relay; Wi-Fi still not linked. Needs the owner: flash, record, drain with the PC relay and the phone (PR Server verification). Then milestone 2 (power baseline), milestone 3 (gain/EQ/bitrate by transcription) | `firmware/recorder/`, `docs/t-watch-ultra-plan.md` §11, `docs/twatch-ble-protocol.md` |
| 11c | Android relay app (T-Watch → BLE → phone → server) | **built, unit-tested (67 tests), not yet on a phone**: foreground service, scan/bond/drain per the protocol, uploads with the bearer token over LAN/Tailscale, acks only after the server's ok, backoff; settings (URL, token in Keystore) and status screen; no Google services, no analytics. CI builds the APK | `android-relay/` |
| 11d | T5 E-Paper Pro PDA (briefing, conversations, action items, calendar over BLE via the phone) | **phone side built, unit-tested, not on hardware**: Odysseus calendar REST verified (plan §6); `android-relay` syncs the T5 next to the watch (glance protocol, feed, outbox, `cal_add` to Odysseus). Firmware and PC relay `pda` in sibling branches | `docs/t5-pda-plan.md`, `android-relay/` |
| 12 | One-command deploy: update, install, run `serve`, MCP over HTTP and whisper-server as systemd user services (start at boot, restart on crash), roll back if unhealthy; the hub's `fork_deploy.sh deploy` runs it first, and it then updates Odysseus itself (`BARVIS_SOUND_ODYSSEUS_DIR`, found from the running container by default), so either command updates both | built, tested in the sandbox (background mode, and systemd with a stand-in `systemctl`), **needs the owner's server run** (PR Server verification) | `server/scripts/deploy.sh`, hub `docs/fork/deploy.md` |
| 13 | Speaker recognition (PR #24) and focused summaries (PR #25): measured in the sandbox, bugs fixed, two improvements | PRs open, **owner: Server verification** | SDarioosh/barvis-sound#31 (recognition), SDarioosh/barvis-sound#38 (insights) |
| 14 | T5 E-Paper Pro PDA firmware (feed and outbox over BLE through the phone) | **milestones 0–3 built, on the owner's T5**: bring-up and refresh times, all screens from a fictional feed, touch and buttons by hand, BLE pairing, TIME, outbox drain/ack and feed PUT from a Windows PC; host tests in CI. Front light, BOOT-hold sleep, sync mode, automatic light sleep and timed sync wakes from deep sleep built and flashed; currents not measured yet. Still open: the PC relay and the Android `PdaSync` against the real T5, the server `PATCH` end to end, light sleep and power (milestone 5) | `firmware/pda/`, `docs/t5-pda-plan.md` |
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
- Frontend: **Odysseus, forked** (the fork was called Barvis until 2026-10-19), modified
  for our system and kept in sync with upstream. Chosen after the side-by-side test; fork plan in `docs/frontend-decision.md`.
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
- `AGENTS.md`: "This is a Barvis module" (now "This is an Odysseus module"), the MCP
  tool contract, where work belongs, instruction files per tool. Added `CLAUDE.md` (imports `AGENTS.md`) and
  `.agents/rules/antigravity.md`. Root `README.md` written.
- The hub got the module contract (now `docs/fork/modules.md`) and a module page for
  this repo (now `docs/fork/modules/odysseus-sound.md`).

## Done in round 5

- Agents now work in a cloud sandbox; the owner runs a "Server verification" section
  from each PR on the server (real model, real devices). `AGENTS.md` updated.
- The sandbox stack was built and run end to end. It found two MCP bugs in our Odysseus fork (HTTP
  servers lose tool annotations, so read-only tools still need approval; stdio servers
  fail on every call). Tracked in the hub's `docs/fork/roadmap.md`. Use MCP over HTTP.
- The sandbox also found a bug here: a summary that fails (e.g. llama-server
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

## Done in round 7: Odysseus keeps MCP annotations over HTTP

- Odysseus fork bug (sandbox finding 1): when Odysseus connected to an MCP server over HTTP, it
  dropped each tool's annotations, so our `readOnlyHint: true` tools still needed
  approval on every call. Fixed in our fork with one line (SDarioosh/Barvis#5, merged);
  nothing changed here. Our tool names, arguments, annotations and links are unchanged.
- Sandbox eval (questions 1, 4, 16; Qwen3-4B), two separate sandboxes: before the fix
  2/3 both times, with one approval per tool call; after, 2/3 and 3/3 with no approvals.
  Earlier sandbox run: 1/3. The scores differ by noise; what the fix changes is the
  approvals.
- Eval question for the owner: an answer that starts with "The task is incomplete: Model
  request failed." still scored PASS (question 4, in both sandboxes), because it also had
  the date and the link (hub sandbox finding 5). Should `eval/bakeoff.py` count that as a failure? Not changed yet.

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
  the sandbox: Odysseus offered `name_speaker` on the `/sound` turn, but the model read the
  sentence as a claim to check and never called it.
- `INSTRUCTIONS` now has a "Naming speakers" paragraph (a statement of who a speaker was
  is a request to save it; call `name_speaker` once per label), and the tool description
  says the same. Sandbox, same message: before, only `search_conversations`; after,
  `search_conversations`, `get_conversation`, `name_speaker` (approved) and Speaker 1
  became Shahyah. The next model call hit the sandbox CPU's read timeout (HTTP 504), so
  Speakers 2 and 3 still need the server run.
- Needs the owner: paste the new `INSTRUCTIONS` into the Custom preset and rerun
  `modules_setup.py` (was `barvis_setup.py`) so `/sound` picks it up.

## Done on 2026-10-19: renamed to odysseus-sound in prose

- The owner dropped the "Barvis" name: the hub is now simply **Odysseus** (our fork, repo
  still `SDarioosh/Barvis`, branch `barvis`) and this module is **odysseus-sound** in
  prose and visible names (docs, comments, log and help text, the Android app label
  "Odysseus Sound Relay", `deploy.sh` messages and unit descriptions, the firmware boot
  log). Links into the hub point at its renamed `docs/fork/` pages.
- Unchanged on purpose (see AGENTS.md "Names: prose vs code"): the repo URL, the
  `barvis-sound*` service names and `BARVIS_SOUND_*` vars, the Android app ID
  `org.barvis.sound.relay`, the BLE name `barvis-tw` and its UUID/tag macros, the Opus
  vendor string, the `barvis_relay` package, and all `convo` identifiers.

## Done on 2026-10-21: finished T5 lists go away (hub: same branch in SDarioosh/Barvis)

- Owner report: a checklist ticked off on the T5 stayed on its Tasks tab for good, fully
  done, taking room from new lists.
- Hub: an unpinned checklist with every item done and no change for 10 minutes is
  archived (`src/checklist_auto_archive.py`, setting `FORK_CHECKLIST_ARCHIVE_MINUTES`,
  `0`/`off` = off). It leaves the T5 at the next sync; no firmware or relay change. Pinned
  lists stay, even finished or empty. An unarchived finished list is archived again after
  10 minutes unless it has an open item (owner's choice). Plan: `docs/t5-pda-plan.md` §4.4.
- Open for the owner: the hub PR's Server verification (tick off a test list on the T5,
  see it leave after 10 minutes and the next sync).

## Done on 2026-10-20: faster transcript requests (hub: same branch in SDarioosh/Barvis)

- Owner report: `/sound` took up to 20 s to show a transcript (qwen3.5-9b, 43 tokens/s).
  The time went to the model retyping the transcript, not to the lookup.
- `get_transcript`: one time per line (`#id [45s] Speaker: text`, start time with seconds
  in the header), 100 lines a page by default (was 200); the result, its description and
  `INSTRUCTIONS` say to answer with the link and a short summary instead of copying lines.
  The hub's Conversations page parser reads both line shapes.
- Sandbox, 60-line fictional meeting: 1,310 s → 125 s; final answer 2,395 → 152 tokens;
  tool result 2,386 → 1,872 tokens. Eval Q1, Q4, Q16, Q22 with `/sound`: 3/4 (Q4 fails
  on `main` too).
- Open for the owner: the PR's Server verification (update the Custom preset with the new
  `INSTRUCTIONS`, rerun `modules_setup.py`, time a transcript request).

## Done on 2026-10-20: T5 PDA, phone side

- **Odysseus calendar REST checked** in the hub (read only, `SDarioosh/Barvis@5cc32c5`),
  written into `docs/t5-pda-plan.md` §6: `GET/POST /api/codex/calendar/events` and
  `DELETE …/{uid}`, `ody_` tokens scoped `calendar:read,calendar:write`, Bearer header only.
  CalDAV write-back happens only into a CalDAV calendar, so the relay sends its id. Create
  isn't idempotent. Proposed hub changes (not made): a codex list-calendars route, an
  idempotency key on create, occurrence delete.
- **android-relay** now runs a `PdaSync` job next to the watch drain: glance protocol
  codec, scan for both UUIDs (the PDA's only while due), outbox first (ack only after the
  server, never on a temporary failure, `cal_add` deduped), then the feed (≤ 32 KB, sent
  only when changed), then `fetch` details, and stay connected while the T5 is in use.
  Settings: Odysseus URL and its own encrypted token, calendar id, templates, interval;
  a PDA line on the main screen. The watch path is unchanged (`GattClient` takes the
  watch profile by default). 112 JVM tests pass, lint is clean.
- **Needs a real phone and T5** (after the firmware): the list in `android-relay/README.md`
  "Not verified on hardware", mainly the two-device scan, the feed `PUT` time, Android's
  `PATCH`, and `cal_add` reaching CalDAV.

## Done on 2026-10-20: plain questions and links (PR #40, hub SDarioosh/Barvis#24)

- Hub: a plain recording question in Odysseus "Auto" (no `/sound`) is offered our read tools
  (`src/module_turn_scope.py` in the hub, through the existing slash-skill hooks; no
  upstream lines).
- Here: list results end with the cite rule; `INSTRUCTIONS` say to search before saying
  "I don't know"; `bakeoff.py` takes `ODYSSEUS_MODE=chat`.
- Sandbox (every tool on + Auto, Q1 4 13 16 20): 0/4 before → 5/5 agent, 4/5 chat; every
  pass cited its link.
- Open for the owner: full 20-question run on the server (both modes) per the hub PR's
  Server verification; the production Custom preset needs the new `INSTRUCTIONS`.

## Done in round 17: T-Watch Ultra buttons, notes and recovery tested on the watch

Branch `feature/twatch-hw-verify`. On the owner's watch over USB from a Mac (ESP-IDF 5.5.1,
`/dev/cu.usbmodem101`), driven by the new `tools/wcon.py` (one console command per call,
`--boot`, `--listen`). Every test chunk (real room audio) was deleted with `queue drop`
right after its sizes, flags and timings were read; none was uploaded.

**Verified on the watch**
- Buttons: BOOT single (status), fast double (mute 2 buzzes / unmute 1 long buzz, owner can
  tell them apart), slow double = two singles, BOOT hold 1 s = sync window; PWR short
  starts/stops a session; PWR hold 1 s starts a note (reg 0x27 reads back 1000 ms); a hold of
  ~6 s powers the watch off, also on USB (it then needs a PWR press to come back).
- Voice notes: NOTE on every chunk, `LAST NOTE` on the last; the 5-min cap gives 10 x 30 s
  chunks and ends "5 min limit"; a note during a conversation ends it with LAST, records the
  note, and a new conversation starts by itself; mute mid-note ends it and nothing resumes.
- Recovery: a restart in the middle of a chunk (console `reboot`, the card is left as by a
  power cut) recovered it at boot (`recovered 1`), cut back to 14.993 s with LAST; the
  previous chunk intact. A real battery pull wasn't done (a PWR hold first starts a note).
- Encoder: 10.4-11.9 ms avg per 20 ms frame (about 53 % of a core at 80 MHz), max 15 ms
  while recording alone, up to 46-68 ms while the screen comes on and a relay syncs (the DMA
  buffer absorbs it; no gap seen).
- Heap (new `heap:` line in `status`): 291 KB internal free before BLE, 197 KB after (BLE
  ~94 KB), largest block 92 KB; lowest during sessions 80 KB; 153 KB after many sessions,
  steady (no leak). PSRAM 8186 KB free, unused so far.

**Fixed**
- **A card write error ended the recording.** An sdspi `0x108` at a chunk boundary ended a
  note after 30 s, and happened again in a conversation. Now the chunk is cut back to its
  last page without LAST, and the session goes on with a new chunk after 100/300/1000 ms of
  mic frames that are read and counted (later chunks keep correct start times); it ends
  after 4 failures in a row. `cq_abandon()` takes a `last` flag (host test added). Tested
  with `sd failwrite <n>` (new test hook) and by a real error that it recovered.
- **`queue drop`** read the zero-padded ids that `queue` lists as octal. Fixed here too, but
  the bug hunt's fix on `main` (ids listed in decimal, `cq_parse_id`) won the merge.
- **PWR hold during a note** did nothing; either PWR gesture now ends a note (owner).
- **Extra PWR short presses**: after a real press, the PMU reported more short presses
  (real key edges) 0.4-2 s later, which once restarted a stopped session. Probably the key
  under a resting finger (the buzz?); a resting finger alone held it long enough to power
  off. A short press within 1 s of the previous one is now ignored (1.5 s at first; 1 s at the owner's request), which catches most of
  them. Owner: not an issue in use, left at that.
- Host tests build on macOS (`reboot()` clashed with `<unistd.h>`); `/build-host/` ignored.

**Reliability (step 2), all tested on the watch**
- **Low battery**: `board_battery_limits()` in `board.h` (T-Watch: low at <= 5 % or < 3.5 V,
  recording allowed again at >= 10 % or on USB); `main/battery_guard.c` (pure, host-tested)
  needs 2 low readings in a row, checked every 15 s. When low: whatever records ends
  cleanly (LAST), 3 buzzes, "BATTERY LOW", sessions/notes/WAV refused (3 buzzes, "CHARGE
  ME"); `status` and the status screen say so. `batt sim <percent> [mV]` / `batt sim off`
  fakes it (USB unplugged). Not yet seen with a really empty battery.
- **Reset reason**: boot log, `status` (`reset: ..., up N s`) and BLE `info` `reset=<name>`
  (protocol §4.1, additive in `proto=1`; the PC relay shows it in `--list`, the Android app
  on its status screen; Android build not run here, no SDK on the Mac: CI).
- **Core dump**: 128 KB `coredump` partition after the app (NVS and bonds kept; bonds
  survived the reflash). Console `coredump` (task, PC, backtrace), `coredump get`, `coredump
  erase`, `crash yes`; `tools/pull.py --coredump` saves it, `idf.py coredump-info -c` decodes
  it (decoded to `cmd_crash` on the test). Its own logs off. Costs ~6 KB internal RAM (191 KB
  free after BLE).
- **Task watchdog**: it only printed warnings before (`CONFIG_ESP_TASK_WDT_PANIC` off). Now
  the session and BLE tasks are subscribed and a 10 s hang restarts the watch with a core
  dump and `reset=task_wdt`; a conversation that was recording starts again after the boot
  (not a note, not after a power cut or brownout). `crash wdt` tested it: restart after
  ~10 s, the cut chunk recovered with LAST, a new session started. 70 s of recording and
  chunk closes: no false trigger. Not yet tested during a BLE drain.
- `pull.py` reset the watch when opening the port on macOS (same fix as `wcon.py`).

**Power logging (step 3, milestone 2)**
- `power` prints the battery, the state (rec/note/ble_adv/ble_conn/screen/sd), the CPU
  clock and the drain rate (%/h over >= 30 min on battery). A background log samples every
  5 min (`power log <min>|off`, NVS) into a 7-day PSRAM ring and writes `/sdcard/power.csv`
  once an hour, only while nothing records or syncs. `power locks` lists the PM locks.
  The AXP2101 has no battery-current reading: drain comes from % over time.
- **Card access from a second task during a recording fails.** Every power-log append while
  a session recorded got sdspi `0x107` (read timeout); the same append right after the
  session worked, and the session's own writes were fine. Hence the "only while idle" rule.
  Together with round 14 (reads failing right after ACK deletes) and the `0x108` write
  errors, the card or the SPI link seems to need more time after writes than sdspi gives
  it. Not investigated further yet (ideas: a slower SPI clock, a longer sdspi timeout, another
  card).
- **Light sleep with the mic running: not possible.** On the watch, `power locks` shows the
  I2S driver's `APB_FREQ_MAX` lock held for the whole recording (the PDM DMA needs the APB
  clock; light sleep would stop it and leave gaps), and BLE's `btLS NO_LIGHT_SLEEP` lock
  held all the time (the controller runs on the main crystal; there is no 32 kHz crystal,
  and the 136 kHz RC clock is too inaccurate for BLE connections per ESP-IDF). So while
  recording the CPU stays at 80 MHz and awake; recording current is set by the mic, the
  encoder (~53 % of a core) and the card. Light sleep could only save power between
  recordings, and only if BLE is shut down while idle (and brought back to advertise) or
  runs on the RC clock: a design choice for the owner. The experiment build was not needed.
- **Owner, milestone 2:** unplug the watch and use it normally (or record all day) until it
  is nearly empty, then `power` and `get power.csv` give the drain per state.

**Seen, not fixed**
- A bonded relay (`8c:a3:ec:...`) connected twice during the tests: once it sent BYE
  without reading, once it read ~111 KB, then the link dropped with 983 notification send
  retries ("no buffers for 5000 ms"). Nothing was acked.
- SD `0x108` errors keep showing up at 4 MHz SPI (round 14: reads after an ACK; now
  writes). Cause unknown; recovery handles it.

## Done on 2026-10-20: voice notes → calendar and to-dos (module side)

- Action items get a `kind` (`task`, `event`, `reminder`), `due_time` (HH:MM in
  `CONVO_TIMEZONE`), `duration_minutes`, `location`, `time_unclear` and `created_at`
  (DB migration to `user_version` 6; old rows become tasks, `created_at` = conversation
  time). Both action prompts explain the three kinds with one example each; times are
  worked out in code (`resolve_time` in `dates.py`): "12 pm" → 12:00, a bare 5 → 17:00,
  a bare 9 → 09:00 marked "time unclear". Repeating events become a task "Set up a
  repeating event: …".
- MCP (non-breaking, version 0.3.0): `list_action_items(..., format="json")` returns a
  JSON array for the hub; text lines show "event:"/"reminder:" and the time.
  `update_action_item(item_id, status, kind, due_date, due_time)`: every field
  optional. `PATCH /v1/action-items/{id}` takes the same fields.
- A re-summary keeps item ids (the hub files items by id), creation times and the
  user's kind/date/time changes.
- Hub side (filing into the Odysseus calendar and to-dos, push, Undo/Move on
  `/modules/sound`): coordinated hub PR.
- **Owner: verify on real voice notes**: record "meeting with Jason on Wednesday at
  12 pm", "get the proposal done on Wednesday", "remind me at 5 to call Mom" on the
  watch, then check `list_action_items format=json` shows event / task / reminder with
  the right date and time (qwen3.5-35b; only fake-LLM tests ran in the sandbox).

## Done on 2026-10-20: "my latest voice notes" finds voice notes

- Problem (owner): "give me my latest voice notes" returned recent conversations. Voice
  notes were stored as kind `note` and labelled "(voice note)", but no tool could ask
  for only them, and the instructions never mentioned them.
- `search_conversations` takes `kind`: `note` (only voice notes), `conversation` (only
  the others), empty (both); "voice_note", "memo", "notes" also mean `note`, and a bad
  value returns a readable message. `INSTRUCTIONS` tell the model to use `kind="note"`
  for voice notes, notes or memos. Hub: the Conversations page gets a type filter and
  labels voice notes (coordinated hub PR).
- Eval: two fictional voice notes in `seed_demo.py` (`note-bike`, `note-offsite`) and
  question 21 "What was my latest voice note about?". Sandbox (Qwen3-4B, Odysseus
  `/sound`): 0/1 before, 1/1 after. **Owner:** rerun `seed_demo.py` on the server's demo
  store (adds only the notes), then the Server verification in the PR.

## Done on 2026-10-20: "the one about AI glasses" finds the conversation faster

- Problem (owner): `/sound` asked for "the one talking about AI glasses" called the same
  transcript many times before answering, although the title ("Privacy and AI Glasses
  Discussion") and summary already matched.
- `INSTRUCTIONS` (the `/sound` skill text) get a "Finding a conversation" paragraph:
  `search_conversations` once with only the topic words, pick the match from the titles
  and summaries it returns, `get_conversation` only for details, `get_transcript` only for
  exact wording, never the same call twice. The `search_conversations` and
  `get_transcript` descriptions say the same.
- Search drops stopwords and "pointing" words (the, one, about, talking, conversation...)
  unless nothing else is left, so "the one talking about AI glasses" searches "AI" OR
  "glasses" instead of matching nearly every conversation. Also used by
  `search_transcripts` and fact linking.
- **Owner:** paste the new `INSTRUCTIONS` into the Odysseus Custom preset and rerun
  `modules_setup.py` (PR Server verification), then retry the question.

## Done on 2026-10-20: junk discarded without a model call

Fixed rules now run before the triage call (`server/convo_server/relevance_rules.py`).
A conversation of only filler words ("okay, mm, yeah") is discarded as `filler`, and a
microphone check ("testing, one, two, is this on?") as `mic_check`, without asking the
model. The rules only discard; everything else still goes to triage. Idea taken from
Omi's backend (`utils/conversations/relevance_rules.py`), without its "keep anything over
100 words" rule, which would let long TV through.

Next (owner's call, not started): mark a voice as TV/media ("Speaker 2 is the TV") so
recurring TV voices are left out of summaries. Needs a new MCP write tool and a hub PR.

## Done on 2026-10-20: bug hunt

Eight bugs were reproduced and fixed here, and three in the hub. Each one has a
regression test. Details are in `docs/bug-hunt-2026-10.md`.
- **Server:** `pcm16` at other sample rates; a merge lost closed action items; facts
  stayed hidden after a deletion; the digest wasn't redone for late uploads; a `nan`
  timestamp gave a 500; speaker clustering was O(n⁴).
- **Firmware:** `queue drop` read zero-padded ids as octal and could drop the wrong
  chunk.
- **PC relay:** crashed on a broken HTTP answer.

Still needs the owner:
- ~~`idf.py build` and `queue` / `queue drop` on the watch~~: done 2026-10-20 in round 17
  (merged build; `queue` lists decimal ids, `queue drop 98` dropped chunk 98).
- The found-not-fixed items in that doc (late-chunk diarization, the BYE/backoff
  conflict) need a decision.

## Done in round 16: PRs #24 and #25 measured, fixed and improved

Both were merged before they were measured. Sandbox numbers (CPU, whisper base.en,
Qwen3-4B) only compare with other sandbox runs.

**Recognition (#24), SDarioosh/barvis-sound#31.** `eval/recognition.py` on 3 AMI meetings
(5 min each, 2 speakers enrolled):

| | WER | DER | named right | named wrong |
|---|---|---|---|---|
| before #24 | 29.8% | 37.2% | 66.7% | 1.3% |
| main | 28.9% | 33.6% | 85.6% | 2.1% |
| #31 (one-line speakers absorbed) | 28.9% | 30.5% | 89.1% | 2.5% |

- Kept: several voice samples per person (+14 pt named right on its own), names of known
  speakers in the whisper prompt (slightly better WER and DER), relabelling of past
  recordings (62–80% of past unnamed speech named, 1–2% wrong).
- Off by default: the vocabulary prompt `CONVO_WHISPER_PROMPT` (broke whisper's
  timestamps: false alarm 8% → 58%) and pyannote (best DER, 95.9% named right, but 4.4%
  named wrong and ~8× slower on CPU).
- The owner loudness hint never fired, even with the owner 6 dB louder and a 12 s
  profile; it needs a real wearable.
- Fixed: the pyannote fallback reused "Speaker 1" for a second voice;
  `rename_speaker_everywhere` could rename a person (even the owner) to "Speaker N".
- Checked: a pre-#24 database migrates to user_version 4 with nothing lost; relabelling
  never changes a name a person set; pyannote made no connection except to Hugging Face
  for the model files; MCP annotations match the contract (no hub change).

**Insights (#25), SDarioosh/barvis-sound#38.** Seeded demo data, by hand and with `eval/bakeoff.py`:

| | before #25 | main | this round |
|---|---|---|---|
| TV snippet discarded | no | yes | yes |
| commitments found (of 9) | 9 | 3 | 8 |
| "by Friday" due date right | — | no | yes |
| outdated facts replaced (budget, Berlin date) | no | yes | yes |
| facts lost through wrong links | 0 | 3 | 0 |
| bakeoff raw (20 questions) | 12/20 | 11/20 | 11/20 |
| bakeoff via Odysseus (questions 2, 14, 19) | 3/3 | 0/3 | 2/3 |

The bakeoff hardly moves: raw passes the same questions on main and this round, and the
Odysseus sample is 3 questions with a 4B model. The data quality shows in the hand checks
and in the answers (Q2 now leads with the 10% cut, not the old 15%).

- Fixed: action-item prompt (#25's examples were demo-data quotes; the model copied
  sentences), due dates resolved in code (`convo_server/dates.py`), implausible fact links
  ignored, and three ways a re-summary lost user state (closed action items reopened, fact
  sources lost, replaced facts back as current). Re-summaries are frequent now, because
  relabelling triggers them.
- Checked: a failing call in any of the four new LLM calls keeps the conversation
  (`failed`, then retried).

## Done on 2026-10-20: transcription quality (PR #39, combined with #42)

Owner report: transcripts are often off. Branch `claude/wizardly-feynman-i86zg9` (PR #39).
It was built in parallel with the room-pickup work of PR #42 and now sits on top of it.
Measured in the cloud sandbox with `server/eval/recognition.py` on public AMI meeting audio
(large-v3-turbo on CPU, 3 meetings x 300 s); only changes that measured better shipped.

- **Finding**: most errors are missing words, not wrong ones. On far-field audio over half
  were speech the VAD never sent to whisper (quiet voices); on clean audio they are mostly
  people talking over each other (whisper writes one voice).
- **Shipped** (server): speech goes to whisper in clips of up
  to 28 s instead of one region at a time (`CONVO_WHISPER_WINDOW_SECONDS`, 0 = old way);
  `suppress_non_speech` on and all decoding settings configurable (`CONVO_WHISPER_BEAM_SIZE`
  etc.); invented "Thank you."/"Bye." lines whisper was unsure of, subtitle credits and
  repetition loops are dropped (`CONVO_WHISPER_DROP_HALLUCINATIONS`); one retry when
  whisper-server drops a kept-alive connection. The VAD gain this round measured
  (boost toward -20 dBFS) was dropped in favour of #42's `CONVO_VAD_AGC_MAX_GAIN`, which
  stays **off** until the server eval decides (owner, 2026-10-20): #42 saw it hurt speaker
  naming at the watch's level.
- **Shipped** (firmware): T-Watch Opus default 24 kbps instead of 16 (complexity 1 stays).
  At the watch's level, 16 kbps cost ~7-9 WER points against uncompressed audio; 24 kbps
  ~1.3. Chunks grow from ~60 to ~90 KB. Not on hardware yet.
- **Turbo WER, before -> after** (measured with this round's VAD gain on): far-field at T-Watch level, 16 kbps 39.8 -> 38.9 %
  (34.4 % at 24 kbps); far-field raw level 50.2 -> 40.5 %; headset mix 23.7 -> 23.5 %.
  About 35 % less whisper time (fewer requests).
- **Tried, didn't help**: beam search (slower, no better), Silero threshold changes, wider
  padding, carrying the previous clip's text into the prompt (worse), a descriptive
  vocabulary prompt (+5.7 points; keep `CONVO_WHISPER_PROMPT` a short word list), denoising
  (much worse), high-pass/dynamic normalization, boosting the clip sent to whisper (helps
  small.en only), large-v3-q5_0 (no better than turbo, slower).
- **Model**: large-v3-turbo-q5_0 scored the same as turbo f16 with ~0.8 GB instead of
  ~1.9 GB; the recommended model next to the 9B LLM.
- **Known**: whisper sometimes repeats a sentence a few seconds later inside a 28 s clip;
  a filter for that removed more real speech than invented text, so none was added.

## Done in round 15: new T-Watch Ultra buttons and voice notes

Branch `feature/voice-notes`, not on hardware yet. Owner decision 2026-10-19 after the first
real recording (a solo mic test) was discarded by triage as `not_a_conversation`.

- **Buttons** (`docs/t-watch-ultra-plan.md` §3): BOOT press = status screen (after a 0.3 s
  double-click window), BOOT double-click = mute (2 buzzes) / unmute (1 long buzz), BOOT
  hold 1 s = sync window, PWR short = start/stop a conversation session (1 buzz / 2),
  PWR hold 1 s = voice note (AXP2101 long-press time set to 1 s, reg 0x27 bits[5:4];
  power-off hold left as shipped). BOOT+PWR is gone. Every action shows a 3 s message.
  BOOT gestures are a pure state machine (`main/button_gesture.c`) with host tests.
- **Voice notes**: a note is its own session; every chunk has the new `NOTE` flag (0x04),
  kept in the `.met` record and sent in `LIST` (protocol §4.3, new test vector). PWR press
  stops it, it stops by itself at 5 min (10 chunks). A note during a conversation ends that
  session cleanly; a new conversation session starts after the note. Console `note
  start|stop`.
- **Relays** (PC and Android) send `kind=note` for NOTE chunks; ordinary uploads unchanged.
- **Server**: ingest `kind` (`conversation` default, `note`), stored per session
  (migration v5). A note session skips speaker matching (every line is the owner), becomes
  one conversation of kind `note` that the gap rule never joins, is never discarded, and
  gets a note prompt (title, the idea in 1-2 sentences, implied to-dos as action items,
  facts). MCP: tool names, arguments, annotations, links and auth unchanged; results say
  "(voice note)" and `type note`.
- Checks: `idf.py build` passes; server ruff clean, pytest 99 passed (the one failure,
  `test_deploy_script.py::test_units_follow_config`, also fails on a clean checkout on
  Windows; CI is Linux); PC relay 62 passed; Android `assembleDebug test lint` passed (70
  tests). Firmware host tests not run (no host C compiler on this PC; CI runs them).

**Needs the real watch**: PWR 1 s long press starts a note and a ~6 s hold still powers
off; short PWR is not also reported long; BOOT single/double/long by feel (fast doubles
not missed, slow singles not read as doubles); the long unmute buzz is distinguishable;
a note alone (`queue` shows NOTE on every chunk, LAST on the last; 5-min cap ends it with
"5 MIN MAX"); a note during a conversation (conversation gets LAST, note separate, new
conversation resumes; mute mid-note doesn't resume); message screen text size; a drain
shows flag 0x05 on the note's last chunk and the server makes a `note` conversation.

## Done on 2026-10-20: Android relay drains the T-Watch (frame cap, GATT client)

The Android relay skipped every chunk ("DATA offset N, expected 0"); after that was fixed it
couldn't get past the MTU exchange. Two bugs, both in the firmware:

- **514-byte notifications.** At ATT MTU 517 a DATA frame was 514 bytes; the Core spec caps
  an attribute value at 512 and Android drops longer notifications, so only each chunk's
  short last frame arrived (the skipped offsets were all multiples of 505). Frames are now
  min(MTU - 3, 512) bytes (503-byte payload); the PC relay, the Android tests' fake watch and
  protocol §7.2 match.
- **No GATT client in NimBLE.** With `CONFIG_BT_NIMBLE_GATT_CLIENT=n` NimBLE has no handler
  for an incoming indication or notification and answers it with an ATT Error Response
  (0x06). Android's own GATT server indicates Service Changed to the bonded watch at
  connect, and the phone took the error as the answer to its pending request: MTU status 6,
  read status 133, "incorrect discovery opcode 001d". Windows never indicates, so the PC
  relay worked. The GATT client (and the central role it depends on in Kconfig) is now on.
- Android relay: clears Android's GATT cache before discovery (`BluetoothGatt.refresh()` by
  reflection), so a firmware update that moves handles can't leave it on stale ones.
- On the owner's Galaxy Z Fold2 (Android 17) with the T-Watch on this firmware: one
  connection, MTU 517, 6 chunks (324 KB, 670 frames) uploaded and acked, 0 links lost.
- Diagnosis tools: `adb logcat -b all` shows the stack's GATT state; the `dumpsys
  bluetooth_manager` BTSNOOP summary has HCI events only, not ATT.

## Done on 2026-10-20: T-Watch Ultra idle power (light sleep and standby)

The owner's watch lost 30 % (about 330 mAh) overnight while idle: about 35-40 mA, an
ESP32-S3 that never sleeps. Light sleep was off, the BT controller would have blocked it
anyway (main-crystal sleep clock without `BT_CTRL_MAIN_XTAL_PU_DURING_LIGHT_SLEEP`), and
the button task polled every 10 ms.

- Automatic light sleep: `light_sleep_enable`, the BT crystal option, buttons on level-low
  interrupts that also wake the chip (no polling while idle; the BOOT gesture machine
  says when nothing is pending, `button_gesture_idle()`), main loop wakes only for events
  and the 15 s battery guard, battery/power logs and standby check. The screen holds a no-light-sleep lock
  while on; a USB host holds one too (`USJ_NO_AUTO_LS_ON_CONNECTION=y`). Without that the
  USB console dies.
- Standby: deep sleep after 2 min idle (no session, `ble_sync_wants_relay()` false, no
  relay, screen off, card unmounted, no USB power), or `standby` on the console. Wake: PWR
  (AXP2101 IRQ line, ext1) or USB power plugged in. Waking shows the status screen, no
  buzz; the waking PWR press does not start a recording. Mute survives (RTC memory). The
  power log is flushed to the card first, and the BT crystal request is dropped before
  deep sleep.
- First flash was built from a stale local branch (without rounds 15-17: buttons, voice
  notes, battery guard, power log); the owner's watch couldn't record with it. Merged main
  and re-flashed.
- On the owner's watch: builds, flashes, console works over USB, `standby` refuses while a
  finished session is queued ("advertising for a relay").
- Needs the owner: drain the queue, unplug, wait 2 min (standby), press PWR (wakes, screen,
  no buzz); a BLE drain on battery (light sleep with BLE); overnight battery % idle with an
  empty queue, and with a queue and no phone in range (light sleep only).
## Done on 2026-10-21: T5 PDA idle power and sync wakes

- **Idle power:** automatic light sleep (DFS 240/40 MHz, `CONFIG_BT_CTRL_MAIN_XTAL_PU_DURING_LIGHT_SLEEP`:
  the main crystal stays the BLE sleep clock, the T5 has no 32 kHz crystal), held off
  while the front light is lit, during a refresh and while USB power is in (BQ25896
  power-good; keeps the console up). Touch waits on the GT911 INT line (low level,
  0x804D = 0x06 on the owner's board) with a safety-net poll (250 ms, 1 s once INT is
  trusted); buttons polled every 50 ms instead of 20. Advertising stays at 1–1.25 s
  (android-relay's low-power scan would miss a slower T5 during "Sync T5 now"). Console
  `power`. Estimated idle on battery: ~1.5–3 mA for the ESP32-S3 instead of ~15–25 mA
  (not measured).
- **Sync wakes from deep sleep:** besides BOOT, a timer aimed at the phone's next due
  sync (last BYE + 15 min, android-relay's default `pdaSyncMinutes`): awake from 10 s
  before to 45 s after (at most 240 s), fast advertising, bonded relays only, no UI, no
  light, panel untouched except a "synced HH:MM" line after a new feed. The RC sleep timer
  runs ~2 % fast, so a wake more than 25 s early sleeps again (PCF8563 time). A crash
  during a sync wake goes back to sleep. Decisions in `main/sleep_plan.c` (host-tested,
  with a 24 h simulation against the phone's schedule). Console `sleep [<min>|off|info]`.
- **Faster wake:** no epdiy full clear at boot; the first screen is one clean refresh
  (0.9 s on the owner's board, one flash instead of three).
- Built by two subagents in parallel (branches `feature/t5-idle-power`,
  `feature/t5-sleep-sync`), merged in `feature/t5-power-sleep`; flashed to the owner's T5,
  boot log clean. **Still to check by hand:** the two lists under "Not verified yet" in
  `firmware/pda/README.md`, above all the sleep and idle currents.
- Seen in the boot log: epdiy halves its pixel clock (20 → 10 MHz) because the data cache
  line is 32 B; a 64 B line (`CONFIG_ESP32S3_DATA_CACHE_LINE_64B`) may make refreshes faster.
- Open for the owner: sending `pdaSyncMinutes` to the T5 (e.g. in TIME) so sync wakes
  follow a non-default interval (an android-relay change).

## Done on 2026-10-20: T5 PDA front light, sleep and sync buttons

- **Front light** (GPIO11, LEDC PWM at 1 kHz on the XTAL clock so DFS doesn't change it):
  "Light" on the Status screen steps off → 25 → 50 → 100 % (kept in NVS); it goes off
  after 60 s without a touch or button and comes back with the next one. Console `light`.
- **BOOT held 1 s = sleep:** an "Asleep" screen (clean refresh, it stays on the panel), the
  GT911 put to sleep, the light pin held low, then deep sleep once BOOT is released;
  a BOOT press wakes it (EXT0 on GPIO0), which restarts the firmware. The feed, outbox,
  bonds and clock (RTC) survive. A button still held at start reports nothing, so the
  waking press isn't a "back". Console `sleep`.
- **The button next to BOOT** (labelled IO48; it is LilyGO's S3 on PCA9535 IO1_2, GPIO48
  is the e-paper's CKV): short = light on/off (the last level used), held 1 s = sync mode,
  the 2-minute pairing window (what BOOT long did), with fast advertising. Clean refresh
  is only on the Status screen now. The power button is hardware only (no log line when
  pressed, checked on the board), so sync mode can't go on it. A bonded phone syncs on its own schedule either way: the phone
  only scans while a sync is due, so the T5 can't call it over.
- Firmware builds and runs on the owner's T5 (boot log clean; the light toggle checked from
  the console). **Still to check by hand**: the list under "Not verified yet" in
  `firmware/pda/README.md`.
- Open question for the owner: should the T5 be able to ask the phone for a sync (a
  service-data flag the phone keeps a low-duty scan for)? That needs an android-relay change.

## Done on 2026-10-20: T5 E-Paper Pro PDA firmware, milestones 0–3

New ESP-IDF project `firmware/pda/` (plan §11 Q1 default: separate project, builds
`crc32`/`isotime` from the recorder unchanged). On the owner's T5 (ESP32-S3R8, MAC
30:ed:a0:b1:ab:cc; it moved from COM35 to COM36 during the session, so flash by MAC):

- **Bring-up (§1 of the plan, now confirmed):** epdiy 2.1.3 board v7 + ED047TC1 work
  unchanged on our I2C bus; PCA9535, TPS65185, GT911 (portrait raw frame: swap X/Y, flip
  Y), PCF8563, BQ27220, BQ25896 all answer. Refresh: partial GL16 ~0.95 s whatever the
  area, full GC16 ~1.0 s, clean (one flash cycle + GC16) 1.57 s, nothing changed 0.13 s.
- **Screens (§5):** Today, Briefing, Calendar, Actions, Talks, Status, conversation and
  transcript details, event detail, template picker and a time grid for new events;
  pending / undo / couldn't-sync marks; partial, full and clean refresh policy. Checked
  through framebuffer dumps and by hand (tabs, item done + undo, swipes, both buttons).
- **Storage:** LittleFS 2 MB: atomic feed swap, 20-entry detail cache, one file per outbox
  action with a never-reused id counter. The feed, outbox, clock and display offset survive
  reboot and reflash.
- **BLE (§3):** NimBLE service with passkey pairing in a 2-minute window, authenticated
  links only, PUT, OB_LIST/OB_ACK, TIME, BYE. Tested from Windows with
  `firmware/pda/tools/pda_ble_test.py` (pairs through WinRT with the code read from the USB
  console): MTU 517, a 4 KB feed in 0.8 s, a 200 KB detail, bad CRC refused, a dropped PUT
  discarded, a lost ack listed again and acked once.
- **Bug found on the board and fixed:** a flash write (outbox ack) during an e-paper
  refresh crashed in epdiy's LCD interrupt ("Cache disabled but cached memory region
  accessed"); `CONFIG_SPIRAM_XIP_FROM_PSRAM` keeps the cache on during flash writes.
- **Protocol details settled** in plan §3.6, matching the PC relay's §3.5 (its branch).
  The relay's scan-response service data and the full name don't fit in 31 bytes
  together: the service data goes out only while the outbox has entries, with the
  shortened name `odysseus-p`.
- Host tests (`firmware/pda/test/`, 6 suites, also under ASan/UBSan) and the `idf.py
  build` run in CI (`firmware-t5-pda` job).

Not done or not verified: see `firmware/pda/README.md` ("Not verified yet"): the sibling
relays against the real T5, light/deep sleep and battery life, flash encryption, pairing
refusal outside the window, and DST (times after a daylight-saving change within the week
are an hour off until the next TIME; v2 fix proposed: a `tz_posix` feed field).
## Done on 2026-10-20: T5 PDA, PC relay side

- `ble_relay.py pda` (`tools/pda_relay.py`, `barvis_relay/pda_*.py`) implements
  `docs/t5-pda-plan.md` §3–§4: the glance codec (status, MTU ≥ 420, the errors), the relay
  algorithm (TIME with the UTC offset, health, outbox first, feed `PUT` only when its
  content hash changed, `fetch` detail blobs with 10-minute transcript pages, staying
  connected while the user is active), strict checks on every action, and the 0/1/no-ack
  results.
- The calendar is behind a `Calendar` protocol. `OdysseusCalendar` uses the hub's token
  routes (`/api/codex/calendar/events`, confirmed in plan §6 by the Android session), with
  `--odysseus-url`, `--odysseus-token-file` and `--calendar-href`. Without them it is a
  "not configured" stub, and calendar actions wait on the T5. It is tested against a fake
  Odysseus only. The optional feed key `templates` comes from `--templates`.
- Tasks tab (plan §4.4): `OdysseusTodos` (`barvis_relay/pda_todos.py`) reads the owner's
  checklists (`GET /api/codex/todos?format=json`) into the feed's `todos`. The new action
  `todo_toggle` posts `toggle_item`, finding the item by index or by its text. Tests run
  against a fake Odysseus. The hub's `format=json` option is a hub change still to land.
- Where the plan left a choice, the answer is now in plan §3.5, §4.1 and §4.3.
- Tests: `tools/tests/test_pda.py` (fake T5 GATT peer, fake server: lost ack, bad CRC,
  paging, MTU, stay mode) and `server/tests/test_pda_feed.py` (the real API, seeded
  fictional data). The watch drain is unchanged.
- **Not run on hardware:** not yet run against the T5 firmware. Still to check
  with real devices: bleak MTU on Windows and BlueZ (≥ 420), pairing with the T5, the
  scan-response flag, PUT throughput.

## Done in round 14: T-Watch Ultra BLE link works from a Windows PC

On the owner's watch (ESP-IDF 5.5.5, COM35) with the bonded Windows desktop:
`ble_relay.py --list` exits 0 and shows `info`, `status` and the queue. A recording session
(`session start`/`session stop`) shows up in `--list`, both while it records and after its
LAST chunk.

- **Controller timeout at connect** (owner's log: `HCI wait for ack returned 19`, ocf 0x0022 =
  LE Set Data Length, `host reset: 19`): the connect callback sent conn-params, PHY and
  data-length requests while Windows was starting encryption. Now the connect callback
  sends no HCI command: 2M PHY and 251-byte packets are controller defaults set once at
  sync, and the fast interval is asked for after encryption. 2M/2M still comes up.
  Not reproduced on main this round (it worked in 6 tries), so the cause isn't proven,
  but none of the 19 runs on the new firmware showed it.
- **Windows missed the watch**: Windows' connection scan caught the 1–1.25 s advertising
  only after 4–10 s, and sometimes not within its 20 s timeout (the watch logged no
  connection). In the sync window the watch now advertises at 152.5–211.25 ms (connect
  in 1–3 s); outside it, still ~1 s.
- **Windows phantom connections**: about 1 in 3 tries, Windows reports a connection the
  watch never saw and closes it within a second. `--once`/`--list` now connect again (up
  to 3 tries) when the link drops before `info` was read, and `--list` prints the failure
  reason instead of only `0 chunk(s) queued`.
- **SD reads fail right after an ACK**: in a real drain the card sometimes answers a read
  within ~2 s of an ACK (two file deletes) late or garbled (sdspi `0x107`/`0x108`), and
  the READ stopped with `E_IO`. Dry runs (no ACKs) and USB reads never hit it. The watch
  now retries a failed read after 20/100/300 ms; all 3 errors in the last drain came
  through on the first retry. Counter: `SD read retries` in `ble`.
- **Drain into the server works**: all 14 test chunks (4 sessions) uploaded, acked and
  deleted on the watch; the server finished all 4 sessions (`done`, no errors) and
  deleted their raw audio. A second `--once` finds `queue=0`.
- `ble` counters show `host resets`. Protocol doc §7.1/§7.2 updated.
- Host tests (cmake) not run on the owner's PC (no host C compiler there); CI runs them.
  `ble_sync.c` isn't part of them.

## Done in round 13: one-command deploy

- `server/scripts/deploy.sh deploy` fetches and fast-forwards `main`, installs into
  `.venv`, (re)starts `convo-server serve`, `convo-server mcp --http` (when
  `CONVO_MCP_TOKEN` is set) and `whisper-server` (when `BARVIS_SOUND_WHISPER_CMD` is set)
  as systemd user services, and waits until each answers. If the new commit doesn't come
  up, it goes back to the previous one. whisper-server is only restarted when its command
  changes, so the model stays loaded. Also `status`, `logs`, `start`, `stop`, `restart`.
- Without systemd (the sandbox) it runs them as background processes with pid files.
- The hub's `scripts/fork_deploy.sh deploy` runs every module's deploy command first
  (`FORK_MODULES`, formerly `BARVIS_MODULES`, in `~/.config/barvis/deploy.env`), then
  rebuilds Odysseus: one command
  updates everything.
- Fix: a session that was being processed when the server stopped stayed `processing`
  forever. The worker now sends such sessions back to the queue when it starts.
- Needs the owner: the PR's Server verification (move the hand-started `serve` and MCP
  to the services).

## Done in round 12: T-Watch Ultra BLE upload path, Android relay

Nothing here has run on hardware yet; everything is built and tested off-device.

- **Protocol** (`docs/twatch-ble-protocol.md`, written first, the contract for all parts):
  our own GATT service (`f8060000-61c0-4fda-8b7b-26b08e916438`), `info`/`status`/`ctrl`/
  `data` characteristics, LIST/READ/ACK/TIME/BYE with byte layouts and test vectors, 30 s
  Ogg/Opus chunks with session id, sequence and RTC start time, delivery rule (watch
  deletes only after ACK; relay acks only after the server's `ok`; LAST chunk → `/end`
  first), passkey pairing (6-digit code on the watch screen) only in a 2-minute sync
  window, advertising only when something is
  waiting, drain-then-disconnect with 15–30 ms bursts, MTU 517.
- **Firmware** (`firmware/recorder/`): Opus session recorder (libopus via `78/esp-opus`,
  complexity 1), pure-C Ogg/Opus writer and page scanner, chunk queue on `/sdcard/q` with
  power-cut recovery and TIME fix-up, NimBLE GATT server (`ble_proto` pure + `ble_sync`
  glue). Console: `session`, `queue`, `ble`, `ls q`, `get q/…`; `tools/pull.py --queue`
  copies chunks over USB. Status screen shows session and queue. The WAV test recorder
  stays as `rec`. Image 0.96 MB, no Wi-Fi library linked. Host tests: 10 suites (CRC,
  Ogg writer, queue, BLE protocol incl. every test vector, plus the earlier ones).
- **PC relay** (`firmware/recorder/tools/ble_relay.py` + `barvis_relay/`): bleak client
  implementing the relay algorithm; stdlib uploader; 58 tests against a fake watch and a
  fake HTTP server; also run end to end against the real server with generated
  (non-speech) Ogg/Opus chunks.
- **Android relay** (`android-relay/`): see row 11c.
- **Server**: no protocol change needed (`codec=ogg`, any device name). Fixed a race: a
  chunk arriving while its session was being processed lost its audio untranscribed;
  now the session goes back for another pass (test added).
- **CI**: also runs the PC relay tests and builds/tests/lints the Android app.

**Not verified on hardware** (also in the PR):
- Opus encode time at 80 MHz (estimate 40–60 % of one core; logged per session), and
  whether DFS still drops to 40 MHz.
- BLE throughput (MTU 517, 2M PHY, DLE), connection-parameter acceptance by Android and
  BlueZ, pairing in and outside the sync window, bonds across reboots.
- Android: background/Doze behaviour, OEM battery killers, scan with the screen off,
  start-on-boot under Android 14/15 rules.
- Power: advertising, bursts, the SD card mounted for a whole session and fsynced every
  second (milestone 2).
- Power-cut recovery and `truncate`/`fsync` on a real FAT card; runtime heap after BLE.

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

- **Owner: transcription quality** (PRs #39 and #42, Server verification): the AMI eval on
  the GPU before/after, switch whisper to large-v3-turbo-q5_0, then the 24 kbps firmware on
  the watch (encode time per frame, drain time, a fictional-speech recording).
0. **Owner: Server verification** of SDarioosh/barvis-sound#31 (recognition eval with
   large-v3-turbo, optional pyannote on the GPU) and SDarioosh/barvis-sound#38 (re-summarize a copy of
   the real DB, bakeoff on the test instance with qwen3.5-35b), then a real-device check:
   name a speaker once, see the name in the next recording.
- **Owner: voice notes end to end**: buttons and notes are verified on the watch (round 17);
  still to do: drain a note with `ble_relay.py --once` and check it arrives as kind `note`
  with a title and summary.
0. **Owner: T-Watch Ultra upload path test** (round 12 PR, Server verification): `--list`
   from the Windows PC works, and so does `--once` into the server (round 14). Still to do:
   the Android app; report the BLE transfer time (PC: ~2 s per 53 KB chunk incl. upload). Encoder:
   ~10.3 ms avg per 20 ms frame at 80 MHz (about half a core).
1. **Owner: T-Watch Ultra milestone 1 test** (`firmware/recorder/README.md`, PR Server
   verification). Then milestone 2: power baseline (plan §7.3, §8): current for board
   idle, mic on, SD write, screen on; whether I2S PDM RX allows light sleep; whether the
   AXP2101 reports discharge current (it has no battery-current ADC channel in its
   register map, so expect a USB meter or PPK2); full-discharge run.
2. **Owner: T-Rex watch test.** The steps are in `zepp-app/README.md`. Server URL
   `http://<server-tailscale-ip>:8000`; the token is in that checkout's `server/.env`.
3. **Odysseus fork:** citations from 9/20 to ≥ 15/20 (hub `docs/fork/roadmap.md`).
4. **Owner: server verification** of the failed-summary retry (steps in its PR), and of
   SDarioosh/Barvis#5 (full eval on the test instance; expect no approvals for read-only
   tools and a score ≥ 9/20; steps in that PR).
5. Build the real T-Rex recording app from the watch-test results.
6. ~~systemd units for `whisper-server`, `convo-server serve`, and MCP over HTTP.~~ Built
   (round 13, `server/scripts/deploy.sh`); owner: Server verification.
7. Owner voice enrollment from the watch (round 10), plus speaker-threshold check on
   real audio.
8. T-Watch Ultra recorder: milestones 3+ (`docs/t-watch-ultra-plan.md` §8) once the
   power baseline says go.
9. XIAO pendant, after the T-Watch Ultra: a second `boards/` directory on the same firmware.
10. Room pickup (`docs/omi-audio-quality.md` §7, sandbox results 2026-10-20): the firmware
    Opus default is now 24 kbps (16 kbps cost 4–5 WER points at 2–3 m). Parakeet + hole
    re-decoding beat whisper base.en, but **whisper large-v3-turbo beats Parakeet on the
    people around the wearer** (3 m, others: 52 vs 64 % WER), so turbo stays the default;
    Parakeet is better on the owner and on speaker labels. Next: turbo's speaker confusion on
    the table recording (32.6 %). **Owner: Server verification**, then watch battery and
    drain time at 24 kbps.
    VAD gain stays off (it helps only on very quiet input).
11. T5 E-Paper Pro PDA (`docs/t5-pda-plan.md`): firmware milestones 0–3 done on the owner's
    T5 (2026-10-20, portrait); the PC relay (`ble_relay.py pda`) and the phone's `PdaSync` are
    built and tested against fakes; the hub calendar REST is confirmed (plan §6). Next: run both
    relays against the real T5, `item_status` through to the sandbox server's `PATCH`, then
    milestone 4 (phone + calendar on the Odysseus test instance, 127.0.0.1:7010) and milestone 5
    (sleep, power, flash encryption). Owner: create an Odysseus token with only the calendar
    scopes, and find the CalDAV calendar's id for the relay settings.

## Open questions for you

The questions from round 2 were answered on 2026-10-18 and are under "Decisions so far".
Still open:
- Recognition: if pyannote on the server GPU names people right without more wrong names
  than ECAPA (step 3 of SDarioosh/barvis-sound#31's Server verification), should it become the default there?
- Insights: should doctor's instructions like "avoid hard food until the follow-up" be
  action items? The model lists them; they're not things to do by a date.
- Owner loudness hint: it never fired in the eval. Keep it until a real-device recording
  shows whether it helps, or remove it?
- Voice notes: is ending the conversation session and resuming it after a note right, or
  should a note be refused while a conversation records? Should notes get their own place
  in the daily digest?
- T-Watch upload path: 16 kbps was not good enough in the eval (round 16); 24 kbps is
  the new default. Confirm on your recordings that it beats 16 kbps and that encode time
  and drain time are fine. (Pairing: answered, a 6-digit passkey on the watch screen.)
- T-Watch Ultra: everything under "Needs the real watch" above. Also: does the battery
  take a 4.2 V charge target (set conservatively; LilyGoLib asks the chip for 4.288 V)?
- T5 PDA: the questions in `docs/t5-pda-plan.md` §11 (firmware layout, feed window,
  Wi-Fi at home, transcript QR, auto-blank, languages). New: should the phone send a POSIX
  time zone (`tz_posix`) in the feed, so times after a DST change are right? And how does
  the real panel look to you (contrast, VCOM 1560 mV, ghosting after a day)?
- Odysseus fork: enable GitHub Actions on `SDarioosh/Barvis` (Actions tab) so CI runs on PRs.
- The remaining XIAO pendant decisions in `docs/xiao-esp32s3-sense-plan.md` §10
(battery/form factor, fallback board, extra parts, microSD). They wait until XIAO work
starts, after the T-Watch Ultra.

## Notes

- The previous untracked `server/` prototype (hardcoded token, broken VAD call) was
  replaced. A copy is in `~/barvis-ai-backups/`.
- The ECAPA speaker model is downloaded once from Hugging Face; model weights only,
  no data goes out.
