# convo-ai server

Device-agnostic ingestion + processing pipeline. Everything runs on this machine:
Silero VAD, whisper.cpp, ECAPA speaker embeddings (SpeechBrain), llama-server, SQLite.

```
device/phone ──POST /v1/ingest/chunk──▶ decode (ffmpeg) ─▶ Silero VAD ─▶ chunk + speech spans
                    ◀── {"ok", "stop"} ──  (stop on N min trailing silence / max duration)

session ended (Stop, server stop, idle) ─▶ reassemble by sequence ─▶ whisper.cpp on speech
  ─▶ diarize (enrolled voices) ─▶ segments ─▶ delete raw audio ─▶ split on >3 min gaps
  ─▶ llama-server JSON summary (title, summary, key points, action items, people,
     topics, facts) ─▶ FTS5 (+ optional embeddings) ─▶ daily digest
```

## Setup

```bash
cd server
uv venv -p 3.12 .venv            # or python3.11+ -m venv .venv
uv pip install -p .venv/bin/python -e ".[vad,diarization,dev]"
cp .env.example .env              # then set CONVO_INGEST_TOKEN and endpoints
```

Requirements outside Python:

- `ffmpeg` (decodes Opus/Ogg/anything)
- whisper.cpp `whisper-server`, e.g.
  `whisper-server -m models/ggml-large-v3-turbo-q5_0.bin --port 8178 --host 127.0.0.1`
- llama-server with an OpenAI-compatible API (`CONVO_LLM_BASE_URL`, `CONVO_LLM_MODEL`)
- optional: a second llama-server with `--embeddings` for semantic search

The ECAPA speaker model is downloaded once from Hugging Face into `data/models/`
(model weights only; no audio or text leaves the machine).

## Run

```bash
.venv/bin/convo-server serve               # API + background worker
.venv/bin/convo-server process             # one-off: finalize idle sessions, process
.venv/bin/convo-server digest --date 2026-10-17
```

Enroll your voice once (20–60 s of you talking, any format ffmpeg reads):

```bash
curl -H "Authorization: Bearer $CONVO_INGEST_TOKEN" -F name=Me -F is_owner=true \
     -F codec=wav -F audio=@me.wav http://127.0.0.1:8000/v1/speakers/enroll
```

## Ingestion API

`POST /v1/ingest/chunk` (multipart), header `Authorization: Bearer <token>`:

| field         | notes                                                              |
|---------------|--------------------------------------------------------------------|
| `audio`       | file; Opus/Ogg/WAV/… or raw `pcm16`                                |
| `device`      | e.g. `trex3pro`, `omi` (`[A-Za-z0-9._:-]{1,128}`)                   |
| `session_id`  | recording session, same charset                                   |
| `sequence`    | int ≥ 0, order within the session; retries are idempotent          |
| `timestamp`   | wall-clock start of the chunk: ISO-8601 or epoch s/ms              |
| `codec`       | `opus` (default), `ogg`, `wav`, `pcm16`, `mp3`, `m4a`, `aac`, `webm` |
| `sample_rate` | only for `pcm16` (default 16000)                                   |

Response: `{"ok": true, "stop": bool, "reason": "silence"|"max_duration"|"client"|"idle"|null,
"duplicate": bool, "speech_seconds", "trailing_silence_seconds", "session_seconds"}`.

`POST /v1/sessions/{id}/end` marks a session finished (device Stop button).

Read API: `/v1/conversations`, `/v1/conversations/{id}`, `/v1/conversations/{id}/transcript`
(`?start=&end=` offsets), `/v1/search?q=`, `/v1/action-items`, `/v1/digests/{date}`,
`/v1/sessions`, `/v1/speakers`. Action items and facts carry `source_url`
(`…/transcript#t=<seconds>`) so answers can link back to the transcript.

## Checks

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
```

Tests use fakes for whisper, the LLM and speaker embeddings, and the energy VAD, so they
run without models. `scripts/smoke_upload.py` uploads real audio files as 30 s Opus
chunks to a running server for an end-to-end check.
