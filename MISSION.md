You are building convo-ai in this repository. Read AGENTS.md first; it describes
the devices, architecture, privacy rules, and checks.

## Goal

A private, fully local system that captures conversations from two wearables,
turns them into transcripts and structured knowledge with local models, and
makes that knowledge available in a personal memory/knowledge frontend.

- **Capture:** an Omi pendant (BLE Opus audio, offline storage) and an Amazfit
  T-Rex 3 Pro (Zepp OS 5 Mini Program recording 30-second Opus chunks; the
  phone-side Side Service uploads them).
- **Process (this Ubuntu server):** one device-agnostic ingestion API →
  Silero VAD → whisper.cpp → speaker diarization with the owner's voice
  enrolled → conversations split on gaps > 3 minutes → llama-server
  structured output (title, summary, key points, action items, people,
  topics, durable facts) and a daily digest → SQLite (+ FTS5, embeddings).
- **Remember:** the SQLite store is the source of truth; a frontend (Hermes
  Agent or Odysseus, decision pending in `docs/frontend-decision.md`) lets the
  owner ask questions and get grounded answers that link to transcripts.

**Privacy is the core constraint.** No audio, transcript, summary or derived
fact leaves the owner's network. Cloud APIs are allowed only for development
tooling (coding agents, downloading model weights), never in the data path.
Raw audio is deleted after successful transcription by default.

The earlier LilyGO T-Watch / custom-firmware design is dropped.

## Plan

Work through these phases in order. Phase 2 runs while the Phase 1 decision
doc is under review; Phase 3 waits for the owner's frontend decision.

### Phase 1: Research and docs
1. **Omi local ingestion** (`docs/omi-ingestion.md`): the Omi app's developer
   features, a self-hosted Omi backend, direct BLE via the Omi SDK, and
   Chronicle; recommend a fully local path and what to adopt or borrow.
2. **Frontend decision** (`docs/frontend-decision.md`): Hermes vs Odysseus vs a
   hybrid (our store exposed over MCP). **Stop and wait for the owner's
   decision.**
3. **Rewrite AGENTS.md / MISSION.md / PROGRESS.md** for this design.

### Phase 2: Device-agnostic server (`server/`)
4. **Ingestion API**: `POST /v1/ingest/chunk` with device, session ID,
   sequence, timestamp, codec; shared bearer token; idempotent retries;
   `{"ok": true, "stop": <bool>}` after N minutes of trailing silence or the
   max session length.
5. **Pipeline**: reassembly by sequence, Silero VAD, whisper.cpp, diarization
   with voice enrollment, conversation splitting, structured summaries, daily
   digest, raw-audio deletion.
6. **Storage and search**: SQLite with FTS5 and optional local embeddings;
   read API with transcript links (`…/transcript#t=<seconds>`).

### Phase 3: After the frontend decision
7. **Knowledge-layer integration**: the chosen frontend (likely via an MCP
   server over our store) with grounded, linked answers; action items →
   tasks/notifications.
8. **Omi ingestion path**: the recommended local relay feeding the ingestion
   API, including offline-storage sync.
9. **T-Rex 3 Pro Zepp app**: first a test app (Start/Stop, 30 s Opus chunks,
   chunk counter) to check on the real device whether recording survives with
   the screen off; then the full app, Side Service upload, and server-driven
   stop with vibration.
10. **End-to-end test** with real devices, phone and server.

## Process for each feature
1. Branch from `main`: `feature/<short-name>` (docs: `docs/<short-name>`).
2. Implement with tests; run the checks in AGENTS.md.
3. Small commits with descriptive messages. Never commit secrets or audio.
4. Push and open a PR. List anything that still needs real-device testing.
5. Update PROGRESS.md: done, next, open questions for the owner.

## Rules
- Never merge without review and passing checks.
- If stuck after 3 attempts on the same problem, delegate to a subagent; if
  that fails, write the blocker in PROGRESS.md and move on to the next
  feature that doesn't depend on it.
- Don't build the knowledge-layer integration before the owner picks a
  frontend.
- Leave `orchestrator/` alone unless asked.
