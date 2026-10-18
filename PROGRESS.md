# convo-ai progress

Design: two wearables (Omi pendant, Amazfit T-Rex 3 Pro) → local Ubuntu server
(Silero VAD, whisper.cpp, diarization, llama-server, SQLite) → personal knowledge
frontend. See MISSION.md and AGENTS.md. Nothing in the data path leaves the network.

_Last updated: 2026-10-18 (round 2)_

## Status

| # | Item | Status | Where |
|---|------|--------|-------|
| 1 | Omi local-ingestion research | ✅ done | `docs/omi-ingestion.md` (PR: docs/redesign) |
| 2 | Frontend decision doc (Hermes vs Odysseus vs hybrid) | ✅ decided: **hybrid via MCP**; Hermes vs Odysseus settled by a side-by-side test | `docs/frontend-decision.md` |
| 3 | Rewrite AGENTS.md / MISSION.md / PROGRESS.md | ✅ done | PR: docs/redesign |
| 4 | Ingestion API (auth, idempotent chunks, silence/max-duration stop) | ✅ done, tested | `server/` (PR: feature/server-pipeline) |
| 5 | Pipeline: reassembly, Silero VAD, whisper.cpp, diarization + enrollment, splitting, summaries, digest, audio deletion | ✅ done, tested | `server/` |
| 6 | SQLite storage, FTS5 + optional embeddings, read API with transcript links | ✅ done, tested | `server/` |
| 7 | Knowledge-layer MCP server (works with Hermes or Odysseus) | ✅ done, tested (PR #3) | `server/convo_server/mcp_server.py` |
| 7b | Wire into Hermes + Odysseus, ~30-question side-by-side | next | — |
| 8 | Omi ingestion path (Android BLE relay) | later: no pendant yet | — |
| 9 | T-Rex 3 Pro Zepp test app (screen-off recording check) | **next, top priority** | `zepp-app/` |
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

- Knowledge layer: **hybrid**. Our SQLite is the source of truth, exposed over MCP, so
  Hermes or Odysseus can sit on top. Pick the frontend after a side-by-side test.
- Phone: **Android**, with Tailscale access to the server.
- Omi: the current consumer pendant (CV1 in Omi's firmware), not bought yet. The T-Rex 3 Pro
  comes first.

## Done in round 2 (PR #3, stacked on #2)

- MCP server (`convo-server mcp`, stdio for Hermes, token-protected HTTP for Odysseus).
  Its 10 tools include `search_transcripts` ("what did X say about Y on DATE") and
  `name_speaker`.
- Speaker naming: saying "Speaker 1 was Alice" renames her lines, redoes the summary and
  learns her voice for future recordings.
- Signed transcript links that open in a phone browser and jump to the moment.
- Verified: Qwen3.5-35B called the right tools with the right dates on 5/5 questions
  against real data. Odysseus' own MCP client code (mcp 1.x) connects to our server.

## Next

1. **Zepp test app** for the T-Rex 3 Pro: Start/Stop, 30 s Opus chunks, chunk counter.
   You run the 10-min screen-on/off tests. Also check that the Side Service can reach
   the server's Tailscale address.
2. Wire the MCP server into a local-only Hermes profile and into Odysseus, then run the
   ~30-question comparison.
3. systemd units for `whisper-server`, `convo-server serve`, and MCP over HTTP.
4. Omi Android relay once you have the pendant.

## Open questions for you

1. **Silence stop for the watch:** default is 5 min trailing silence and 4 h max session. OK?
2. **Timezone** for digests and date questions (`CONVO_TIMEZONE`, default UTC)?
3. **Retention:** keep full transcripts forever, or only summaries/facts after N months?
4. **Calendar/tasks:** should action items go to a real calendar/task system? That would
   favour Odysseus.
5. **Exposed services on this host:** Odysseus' ChromaDB is readable without auth on
   `0.0.0.0:8100`, ntfy is on `0.0.0.0:8091`, llama-server on `0.0.0.0:8082` has no
   auth. OK to rebind them to loopback / the docker network? (Nothing changed yet.)
6. Commit the `firmware/` removal and ignore `firmware-archive/` (untracked, contains
   `include/secrets.h`)?

## Notes

- The previous untracked `server/` prototype (hardcoded token, broken VAD call) was
  replaced. A copy is in `~/barvis-ai-backups/`.
- `zepp-app/` is a placeholder that won't build (no `app.json`, browser-style
  `window` usage, nonexistent npm packages). It will be rebuilt as the Phase 3 test
  app.
- The ECAPA speaker model is downloaded once from Hugging Face; model weights only,
  no data goes out.
