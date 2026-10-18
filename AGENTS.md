# convo-ai: agent guide

convo-ai is a private conversation-memory system. Two wearables capture
audio; this Ubuntu server transcribes, diarizes and summarizes it with local
models; a personal knowledge frontend answers questions about it. **Nothing in
the data path touches the cloud.**

## Layout

- `server/`: Python 3.11+ package `convo_server`: ingestion API, VAD,
  transcription, diarization, conversation splitting, summaries, SQLite
  storage, search, digest. See `server/README.md`.
- `zepp-app/`: Zepp OS Mini Program for the T-Rex 3 Pro (to be rebuilt in
  Phase 3; the current files are a non-working placeholder).
- `docs/`: research and decisions (`omi-ingestion.md`,
  `frontend-decision.md`, `xiao-esp32s3-sense-plan.md`: the plan for our own
  XIAO ESP32-S3 Sense pendant; plan only, no code until its open decisions are answered).
- `hermes/`: Hermes profile snippet used by the orchestrator.
- `orchestrator/`: optional build loop. Leave it alone.

## Privacy rules (non-negotiable)

- Audio, transcripts, summaries, facts and embeddings stay on the owner's
  network. No cloud STT (Deepgram, Omi cloud, OpenAI…), no cloud LLMs or
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
   through the API (or a future MCP server), never the other way round.

## Checks

- Server lint: `cd server && .venv/bin/ruff check .`
- Server tests: `cd server && .venv/bin/python -m pytest -q`
  (no models needed; fakes for whisper, LLM, embeddings)
- Zepp app: `cd zepp-app && zeus build` once the real project exists
- Optional end-to-end: `server/scripts/smoke_upload.py` against a running
  server with whisper-server and llama-server up.

Skip a check only if its project doesn't exist yet. CI (`.github/workflows/ci.yml`)
runs the server checks on Python 3.11.

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

- Work on a feature branch. Agents run by the orchestrator never push,
  merge or open PRs (the orchestrator does). In interactive sessions, push and
  open PRs only when the owner asks; never merge without review.
- Never commit secrets, `.env`, databases, or audio files of any kind.
- Don't build the knowledge-layer/frontend integration until the owner has
  decided (see `docs/frontend-decision.md`).
- If you're stuck, end your reply with `ESCALATE: <reason>` instead of guessing.
- Device code you can't verify on hardware is fine, but list what still needs
  real-device testing in the PR description and PROGRESS.md.
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
