# convo-ai progress

Design: two wearables (Omi pendant, Amazfit T-Rex 3 Pro) → local Ubuntu server
(Silero VAD, whisper.cpp, diarization, llama-server, SQLite) → personal knowledge
frontend. See MISSION.md and AGENTS.md. Nothing in the data path leaves the network.

_Last updated: 2026-10-18_

## Status

| # | Item | Status | Where |
|---|------|--------|-------|
| 1 | Omi local-ingestion research | ✅ done | `docs/omi-ingestion.md` (PR: docs/redesign) |
| 2 | Frontend decision doc (Hermes vs Odysseus vs hybrid) | ✅ written, **waiting for your decision** | `docs/frontend-decision.md` |
| 3 | Rewrite AGENTS.md / MISSION.md / PROGRESS.md | ✅ done | PR: docs/redesign |
| 4 | Ingestion API (auth, idempotent chunks, silence/max-duration stop) | ✅ done, tested | `server/` (PR: feature/server-pipeline) |
| 5 | Pipeline: reassembly, Silero VAD, whisper.cpp, diarization + enrollment, splitting, summaries, digest, audio deletion | ✅ done, tested | `server/` |
| 6 | SQLite storage, FTS5 + optional embeddings, read API with transcript links | ✅ done, tested | `server/` |
| 7 | Knowledge-layer integration (MCP server + frontend) | ⏸ on hold until you decide | — |
| 8 | Omi ingestion path (BLE relay) | ⏸ after decision | — |
| 9 | T-Rex 3 Pro Zepp test app (screen-off recording check) | ⏸ after decision | `zepp-app/` |
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

## Next (after your decision)

1. MCP server over our store (`search_conversations`, `get_conversation`,
   `get_transcript_excerpt`, `list_action_items`, `update_action_item`,
   `list_facts`, `daily_digest`) + the chosen frontend's config, plus an HTML
   transcript view for phone links.
2. Omi: Linux `bleak` relay prototype → ring-buffer offline drain → Android relay.
3. Zepp test app: Start/Stop, 30 s Opus chunks, chunk counter. You run the
   screen-on / screen-off 10-minute tests on the watch.
4. systemd units for `whisper-server` and `convo-server`.

## Open questions for you

1. **Frontend:** hybrid + Hermes (recommended), hybrid + Odysseus, or something else?
2. **Phone OS:** Android or iPhone? This decides the Omi relay (iOS needs Xcode
   sideloading and background-BLE testing) and how the Zepp Side Service reaches the
   server.
3. **Omi hardware/firmware:** CV1 or DevKit, and firmware version (≥ 3.0.20 uses the
   ring storage protocol)?
4. **Away from home:** is the server reachable over Tailscale, or should relays queue
   until you're back on the LAN? For how long?
5. **Omi capture mode:** live streaming or store-on-pendant and sync later (battery vs
   latency)?
6. **Silence stop for the watch:** default is 5 min trailing silence
   (`CONVO_SILENCE_STOP_SECONDS=300`) and 4 h max session. OK?
7. **Timezone** for daily digests (`CONVO_TIMEZONE`, default UTC)?
8. **Retention:** keep full transcripts forever, or only summaries/facts after N months?
9. **Calendar/tasks:** should action items go to a real calendar/task system
   (CalDAV, Apple Reminders)? That would favour Odysseus.
10. **Exposed services on this host** (found during research): Odysseus' ChromaDB
    answers unauthenticated on `0.0.0.0:8100` (`CHROMADB_BIND=0.0.0.0`), ntfy is on
    `0.0.0.0:8091`, llama-server on `0.0.0.0:8082` has no auth. OK to rebind them to
    loopback / the docker network? (I haven't changed anything.)
11. The old `firmware-archive/` (untracked, contains `include/secrets.h`) and the
    deleted `firmware/` are uncommitted changes in your working tree. Should I commit
    the `firmware/` removal and ignore `firmware-archive/`?

## Notes

- The previous untracked `server/` prototype (hardcoded token, broken VAD call) was
  replaced. A copy is in `~/barvis-ai-backups/`.
- `zepp-app/` is a placeholder that won't build (no `app.json`, browser-style
  `window` usage, nonexistent npm packages). It will be rebuilt as the Phase 3 test
  app.
- The ECAPA speaker model is downloaded once from Hugging Face; model weights only,
  no data goes out.
