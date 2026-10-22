# odysseus-sound: agent guide

odysseus-sound (formerly "convo-ai", then "barvis-ai", then "barvis-sound") is a
private conversation-memory system and the first **Odysseus module**. Wearables capture
audio; this Ubuntu server transcribes, diarizes and summarizes it with local
models; [Odysseus](https://github.com/SDarioosh/Barvis), our Odysseus fork (repo
`SDarioosh/Barvis`), answers questions about it through our MCP server. **Nothing in
the data path touches the cloud.**

## Names: prose vs code

- In prose (docs, PR text, comments you add) call the project **odysseus-sound**
  (formerly convo-ai, barvis-ai, barvis-sound) and the hub **Odysseus** ("our Odysseus
  fork" where it must be told apart from upstream Odysseus).
- **Don't rename code identifiers.** They keep the old name on purpose: the
  `convo_server` package, the `convo-server` CLI, `CONVO_*` env vars, paths such as
  `server/data/convo.db`, the MCP server name `convo-ai`, the hub's server name
  `convo`, the Hermes profiles `convo-ai` (orchestrator) and `convo` (eval), the Zepp app ("Convo Rec Test", vendor
  `convo-ai`), the repo URL `SDarioosh/barvis-sound`, the `barvis-sound*` service names
  and `BARVIS_SOUND_*` vars of `server/scripts/deploy.sh`, the Android app ID
  `org.barvis.sound.relay`, the BLE name `barvis-tw` and its `BARVIS_UUID` /
  `BARVIS_*` Opus tag macros, and the PC relay's `barvis_relay` package. Renaming them
  breaks deployed servers and configs, paired watches, the installed phone app and
  Odysseus. Don't "fix" them.

## This is an Odysseus module

- Odysseus (our Odysseus fork, AGPL-3.0) is the hub: the one chat and agent UI. This repo
  is a separate program that owns its data. Odysseus reaches it only through our MCP
  server (or the HTTP API here) and never writes `convo.db` directly.
- The module contract is in the hub repo:
  [docs/fork/modules.md](https://github.com/SDarioosh/Barvis/blob/barvis/docs/fork/modules.md).
  How Odysseus connects to us:
  [docs/fork/modules/odysseus-sound.md](https://github.com/SDarioosh/Barvis/blob/barvis/docs/fork/modules/odysseus-sound.md).
- Never copy Odysseus code (upstream or our fork) into this repo. Talking over MCP keeps
  the AGPL out of this module.

## Layout

- `server/`: Python 3.11+ package `convo_server`: ingestion API, VAD,
  transcription, diarization, conversation splitting, summaries, SQLite
  storage, search, digest, MCP server (`convo_server/mcp_server.py`), frontend eval
  (`server/eval/bakeoff.py`). `server/scripts/deploy.sh` updates the server and runs it
  as background services; the hub's `scripts/fork_deploy.sh` calls it. See `server/README.md`.
- `zepp-app/`: Zepp OS Mini Program for the T-Rex 3 Pro. Currently the
  recording feasibility test app; see `zepp-app/README.md`.
- `firmware/recorder/`: ESP-IDF 5.x firmware for our own recorders, one shared `main/`
  and a board layer (`main/board.h`, `boards/twatch_ultra/board/`, `boards/twatch_s3/board/`
  for the LilyGO T-Watch S3: one button, recordings queued in flash; `boards/t4_s3/board/`
  for the LilyGO T4-S3 conference recorder: one button, four INMP441 I2S mics; shared chip
  drivers are in `boards/common/`). Milestone 1
  (bring-up test firmware for the T-Watch Ultra) is built, plus Opus recording sessions,
  the SD chunk queue and the BLE service; `tools/ble_relay.py` is the PC relay. See
  `firmware/recorder/README.md`.
- `android-relay/`: Android relay app (Kotlin): drains the T-Watch over BLE and uploads to
  the server. Protocol: `docs/twatch-ble-protocol.md`. See `android-relay/README.md`.
- `docs/`: research and decisions (`omi-ingestion.md`,
  `frontend-decision.md`, `xiao-esp32s3-sense-plan.md`: the plan for our own
  XIAO ESP32-S3 Sense pendant, which comes later; `t-watch-ultra-plan.md`: the same
  recorder on the LilyGO T-Watch Ultra, built first, with one shared ESP-IDF firmware.
  Milestone 1 of the T-Watch Ultra is implemented in `firmware/recorder/`, and so is the BLE
  upload path; `twatch-ble-protocol.md`: the watch ↔ relay protocol;
  `t5-pda-plan.md`: a T5 E-Paper Pro PDA synced over BLE through the phone, planned).
- `hermes/`: Hermes profile snippet used by the orchestrator.
- `orchestrator/`: optional build loop. Leave it alone.

## Privacy rules (non-negotiable)

- Audio, transcripts, summaries, facts, embeddings and chat content stay on the
  owner's network. No cloud STT (Deepgram, Omi cloud, OpenAI…), no cloud LLMs or
  embedding APIs in the data path, no telemetry.
- Cloud is fine only for development tooling (coding agents, package and
  model-weight downloads).
- Raw audio is deleted after successful transcription unless
  `CONVO_KEEP_AUDIO=true`. Failed sessions keep audio so they can be retried.
- Every API route except `/health` requires `Authorization: Bearer <token>`.
  Bind to `127.0.0.1` unless devices need LAN/Tailscale access.

## Devices

### Omi pendant (BasedHardware/omi)
- Streams Opus audio over BLE; stores audio offline when disconnected.
  Does no transcription itself.
- Default Omi setups use cloud STT; ours must not. The official Omi app and
  backend stay out of the data path entirely (its audio-bytes webhook is sent
  from Omi's cloud; custom-STT mode still sends transcripts to Omi).
- Recommended path (`docs/omi-ingestion.md`): our own BLE relay (Android
  foreground service first, optional Linux `bleak` collector at home) reads
  service `19b10000-e8f2-537e-4f6c-d104768a1214`, reassembles the 3-byte-header
  Opus packets (16 kHz, 20 ms), and posts 30 s Ogg/Opus chunks to
  `POST /v1/ingest/chunk` with `device=omi`. Offline audio is drained over the
  storage service and acknowledged only after the server returns `ok`.
  Borrow from Chronicle's relay app and chunk design; don't adopt its backend.

### Amazfit T-Rex 3 Pro
- Zepp OS 5, built-in mic. App code is a Zepp OS Mini Program (JavaScript,
  built with `@zeppos/zeus-cli`).
- Records 30-second Opus chunks with the `@zos/media` Recorder; the
  phone-side Side Service uploads them to the server.
- Recording stops on Stop, a max duration, or when the server answers
  `"stop": true` (the Recorder exposes no audio levels, so the server runs VAD).
- Whether recording survives with the screen off must be verified on the
  real device first (test app). Developer mode is required to install.

## Architecture

1. **Ingestion** — `POST /v1/ingest/chunk` (multipart): `audio`, `device`,
   `session_id`, `sequence`, `timestamp` (chunk start, ISO-8601 or epoch),
   `codec`. Decodes with ffmpeg, runs Silero VAD, stores the chunk and its
   speech spans, and returns `{"ok": true, "stop": bool, "reason": ...}`.
   Stop when trailing silence ≥ `CONVO_SILENCE_STOP_SECONDS` or the session
   exceeds `CONVO_MAX_SESSION_SECONDS`. Retries of the same
   (session, sequence) are idempotent. `POST /v1/sessions/{id}/end` = Stop.
2. **Finalize** — a session ends on Stop, server stop, or
   `CONVO_SESSION_IDLE_SECONDS` without uploads; the worker then processes it.
3. **Transcribe** — contiguous chunks are reassembled by sequence; only VAD
   speech regions go to whisper.cpp (`whisper-server`, or `whisper-cli`).
4. **Diarize** — ECAPA speaker embeddings (≥ 3 s windows), agglomerative
   clustering, matched against enrolled voices (`POST /v1/speakers/enroll`;
   the owner is `is_owner=true`).
5. **Split** — conversations break on speech gaps > 3 min
   (`CONVO_CONVERSATION_GAP_SECONDS`); a new session within the gap on the
   same device extends the previous conversation.
6. **Summarize** — llama-server (OpenAI-compatible, `json_schema` constrained,
   thinking disabled) returns title, summary, key points, action items,
   people, topics, durable facts, each with a source offset. Daily digest per
   `CONVO_TIMEZONE` day.
7. **Store** — SQLite (`server/data/convo.db`): sessions, chunks, segments,
   conversations, action items, facts, people, digests, speakers, FTS5 index,
   optional embeddings. This is the source of truth; any frontend reads it
   through the MCP server or the API, never the other way round.

## MCP tool contract

`server/convo_server/mcp_server.py` is the interface Odysseus depends on.

| Tool | Annotation |
|---|---|
| `search_transcripts`, `search_conversations`, `get_conversation`, `get_transcript`, `list_action_items`, `list_facts`, `get_daily_digest`, `list_people`, `list_unnamed_speakers` | read-only (`ro`: `readOnlyHint: true`) |
| `update_action_item`, `name_speaker`, `reassign_line` | write (`rw`: `readOnlyHint: false`, `destructiveHint: false`) |
| `rename_speaker_everywhere`, `retire_fact` | write, destructive (`rw_merge`: `readOnlyHint: false`, `destructiveHint: true`; merging two names or forgetting a fact can't be undone) |

- Odysseus runs read-only tools without approval because of `readOnlyHint: true`. A tool
  that changes anything must use `rw`. Never mark a write tool read-only.
- Every fact in a result carries a transcript link
  (`<CONVO_PUBLIC_URL>/view/<id>?sig=…#t=<seconds>`), and every non-empty result asks
  for the link next to each fact used: frontends don't forward the server
  `instructions`.
- HTTP auth accepts `Authorization: Bearer` or `?access_token=` (`BearerAuth`); Odysseus
  can only use the query form.
- `list_action_items` and `list_facts` take `format="json"` for programs (the hub's
  jobs). `list_facts(confirmed=true)` keeps only confirmed facts: current, said in
  ≥ `CONVO_FACT_CONFIRM_SOURCES` (3) distinct conversations on ≥
  `CONVO_FACT_CONFIRM_DAYS` (2) distinct dates in `CONVO_TIMEZONE`. The hub copies them
  into its memory hourly (`confirmed=true, format="json"`) and retires any fact id no
  longer returned. Each JSON row also has `confirmed` (true/false by those thresholds),
  so a program can read all current facts and promote the confirmed ones. JSON fields are
  listed in `server/README.md`.
- Each `list_action_items` JSON row has `title` (≤ 60 characters, for the hub's to-do or
  note) and `explicit` (true when the item has a date or time, a named list, kind
  reminder/event/note, or the voice note used a command word such as "remind me" or "add a
  task"), so the hub files explicit items itself and offers the rest as suggestions;
  `update_action_item` can set `title` (MCP 0.7.0, additive). Intentions, priorities,
  principles and decisions are facts about the owner, not action items.
- People have a stable id (`people` table) that survives renames, so the hub can link a
  person here to one of its contacts. `list_people(format="json")` returns
  `{"people": [{id, name, aliases, is_owner, has_voice, conversations, first_seen,
  last_seen}]}` (people merged into another are left out), and each `list_facts` JSON
  row has `person_id` (the subject's person for `about` person or owner, else null).
  `rename_speaker_everywhere` keeps the id and keeps the old name as an alias; renaming
  onto an existing person merges into that person, whose id survives.
- `retire_fact(fact_id, reason)` forgets a fact (the hub's review page): status
  `retired`, out of `list_facts` (both formats), search and conversation details. It
  stays retired when its conversation is re-summarized, and a later fact linked "same"
  to it becomes another source of it, so a forgotten fact doesn't come back. Idempotent.
- **Changing a tool name, an argument, an annotation, the link format, or the auth is a
  breaking change.** It needs a coordinated hub PR (module doc, preset text) per the
  [versioning rules](https://github.com/SDarioosh/Barvis/blob/barvis/docs/fork/modules.md#versioning),
  and a rerun of `server/eval/bakeoff.py --frontend odysseus`: a few questions in the
  sandbox stack, and the full run listed under Server verification for the owner. Put
  the scores in both PRs. Current server score: 9/20, target ≥ 15/20.

## Where work belongs

| Work | Where |
|---|---|
| Capture, ingestion, VAD, transcription, diarization, splitting, summaries, storage | `server/` here |
| MCP tools, their results and links | `server/convo_server/mcp_server.py` here (contract rules above) |
| Watch app | `zepp-app/` here |
| Watch/pendant firmware, BLE protocol, phone and PC relays | this repo (`firmware/`, `android-relay/`, `docs/twatch-ble-protocol.md`) |
| Eval questions and scoring | `server/eval/bakeoff.py` here |
| How Odysseus gates, routes or shows our tools; presets; Odysseus UI views | the hub repo (`SDarioosh/Barvis`) |
| Upstream Odysseus bugs | the hub repo (offered upstream) |

## Where you work: the cloud sandbox

- You work in a cloud sandbox, **not on the owner's server**. Never assume access to the
  server, its services, its data, the devices or Tailscale.
- The sandbox runs the whole system on CPU with fictional data: llama-server with a
  small model, whisper-server, `convo-server serve` and `mcp --http`, Odysseus on
  `127.0.0.1:7010`, and the eval. Steps, timings and known issues:
  [hub docs/fork/sandbox.md](https://github.com/SDarioosh/Barvis/blob/barvis/docs/fork/sandbox.md).
- Small-model scores only compare with other sandbox runs. The real score
  (`qwen3.5-35b` on the server) is measured by the owner.
- Use only fictional data (`server/scripts/seed_demo.py`). Never ask for or use real
  recordings, transcripts or tokens.

### Server verification (owner)

- Every PR that can change answers, the pipeline on real audio, devices or deployment
  has a **Server verification** section: exact commands, the expected result, and
  real-device steps ([format](https://github.com/SDarioosh/Barvis/blob/barvis/docs/fork/sandbox.md#server-verification)).
- The owner runs it and pastes the result into the PR; continue from that result.
- Never write steps that touch the owner's production Odysseus install (Docker,
  `~/models/odysseus`, port 7000). Server-side evals target the Odysseus test
  instance on `127.0.0.1:7010`.

## Checks

- Server lint: `cd server && .venv/bin/ruff check .`
- Server tests: `cd server && .venv/bin/python -m pytest -q`
  (no models needed; fakes for whisper, LLM, embeddings)
- Zepp app: `cd zepp-app && zeus build` once the real project exists
- Recorder firmware host tests: `cmake -S firmware/recorder/test -B build-host &&
  cmake --build build-host && ctest --test-dir build-host`
- Recorder firmware build (ESP-IDF ≥ 5.4): `cd firmware/recorder && idf.py
  -DBOARD=twatch_ultra -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.twatch_ultra"
  build`; T-Watch S3: `idf.py -B build-s3 -DBOARD=twatch_s3 -DSDKCONFIG=build-s3/sdkconfig
  -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.twatch_s3" build`; T4-S3: `idf.py -B
  build-t4 -DBOARD=t4_s3 -DSDKCONFIG=build-t4/sdkconfig
  -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.t4_s3" build`
- PC BLE relay (no Bluetooth needed): `python -m pytest -q firmware/recorder/tools`
- Android relay: `cd android-relay && ./gradlew assembleDebug test lint` (JDK 17+,
  Android SDK platform 35)
- Optional end-to-end: `server/scripts/smoke_upload.py` against a running
  server with whisper-server and llama-server up.

Skip a check only if its project doesn't exist yet. CI (`.github/workflows/ci.yml`)
runs the server checks on Python 3.11, the firmware host tests and `idf.py build`, the PC
relay tests and the Android build and tests.

## Conventions

- Python 3.11+, type hints, ruff clean (`pyproject.toml` config), pytest.
- External models sit behind small protocols (`VAD`, `Transcriber`,
  `SpeakerEmbedder`, `Embedder`, `LLMClient`) so tests use fakes and backends
  can be swapped.
- Timestamps are UTC epoch seconds internally, ISO-8601 in the API.
- Config comes from environment / `server/.env` (gitignored), prefix
  `CONVO_`. Commit only `.env.example`. The server refuses to start without
  `CONVO_INGEST_TOKEN`.
- Zepp app: JavaScript, `@zeppos/zeus-cli`, test on the real device.
- Small, focused commits with descriptive messages.

## Rules for agents

- Work on a feature branch (`feature/<name>`, docs: `docs/<name>`) and open a PR into
  `main`. Small commits. Agents run by the orchestrator never push, merge or open PRs
  (the orchestrator does). In interactive sessions, push and open PRs only when the
  owner asks.
- Agents **never merge** and never push to `main` directly.
- Never commit secrets, `.env`, tokens, databases, or audio files of any kind.
- Frontend: the owner chose an Odysseus fork (`docs/frontend-decision.md`), now simply
  called Odysseus. Hub work happens in the hub repo; see "This is an Odysseus module".
- After **three failed attempts at the same problem**, stop and end your reply with
  `ESCALATE: <reason>` instead of guessing.
- Device code you can't verify on hardware is fine, but list what still needs
  real-device testing in the PR's Server verification section and PROGRESS.md.
- Keep PROGRESS.md current: done, next, open questions for the owner.

## Key APIs and tools

- **Server**: FastAPI, ffmpeg, `silero-vad`, whisper.cpp `whisper-server`
  (`/inference`, `verbose_json`), SpeechBrain ECAPA
  (`speechbrain/spkrec-ecapa-voxceleb`), llama-server `/v1/chat/completions`
  with `response_format: json_schema`, optional `/v1/embeddings`, SQLite FTS5.
- **Zepp OS**: `@zos/media` Recorder, `@zos/ble` / TransferFile (watch →
  phone), Side Service `fetch` for upload, display keep-on, vibration; docs at
  https://docs.zepp.com.
- **Omi**: see `docs/omi-ingestion.md`.

## Instruction files per tool

- `AGENTS.md` (this file) is the single source of instructions.
- Claude Code reads `CLAUDE.md`, which imports this file (`@AGENTS.md`).
- Google Antigravity reads `AGENTS.md` at the workspace root directly;
  `.agents/rules/antigravity.md` adds Antigravity notes only.
- Put shared rules here, never in the tool-specific files.
