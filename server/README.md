# barvis-sound server

The `convo_server` package (CLI `convo-server`, env prefix `CONVO_`) keeps the project's
old name, convo-ai. Don't rename it.

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

### Better speaker separation: pyannote (optional)

`CONVO_DIARIZER=pyannote` uses pyannote.audio with the
`speaker-diarization-community-1` pipeline (code MIT, weights CC-BY-4.0) instead of
clustering one voiceprint per whisper segment. Enrolled voices still use ECAPA, so
existing profiles keep working.

```bash
uv pip install -p .venv/bin/python -e ".[vad,diarization,pyannote]"
# .env: CONVO_DIARIZER=pyannote  (CONVO_PYANNOTE_DEVICE=auto uses the GPU if there is one)
```

The weights come once from the ungated `pyannote-community` copy on Hugging Face into
`data/models/pyannote/`; after that it runs offline (`HF_HUB_OFFLINE=1` works, or set
`CONVO_PYANNOTE_MODEL` to a local directory). pyannote.audio 4 sends usage telemetry by
default; the server turns it off before loading pyannote. pyannoteAI cloud pipelines
(`precision-2`, `*-cloud`) are refused.

### Speakers

- Each person has several voice samples (enrollment is cut into ~9 s samples; every
  `name_speaker` adds one, up to 40 learned samples). A voice is matched against the
  closest few samples, not one blended voiceprint.
- After someone is named or enrolled, unnamed speakers ("Speaker N") in other
  conversations whose stored voice clearly matches are renamed too
  (`CONVO_SPEAKER_RELABEL_THRESHOLD`, default 0.60, and at least 0.05 above any other
  known voice); those conversations are re-summarized.
- Owner hint: the wearer is usually the loudest voice. A speaker at least
  `CONVO_OWNER_HINT_DB` (3 dB) louder than everyone else whose voice is a borderline
  match for the owner (within `CONVO_OWNER_HINT_MARGIN`, 0.10, below the match
  threshold) is labelled as the owner. `CONVO_OWNER_HINT_MARGIN=0` turns it off.
- whisper gets a prompt: `CONVO_WHISPER_PROMPT` (names, places, jargon) plus the names
  of known speakers, so they are spelled right.

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
`/v1/sessions`, `/v1/speakers`. Conversations carry a signed `link`, and action items and
facts a `source_url`, of the form `<CONVO_PUBLIC_URL>/view/<id>?sig=…#t=<seconds>`: an HTML
transcript page that opens in a phone browser without the bearer token and jumps to that
moment. Rotating `CONVO_INGEST_TOKEN` revokes all links.

## Knowledge layer (MCP)

`convo-server mcp` exposes the store to any MCP-capable frontend. The store stays the
source of truth, so the frontend can be swapped. Tools:

| tool | answers |
|---|---|
| `search_transcripts(speaker, query, since, until)` | "what did X say (about Y) on/since DATE": actual lines, with links |
| `search_conversations(query, person, since, until)` | conversations by topic / person / date |
| `get_conversation(id)`, `get_transcript(id, start_seconds, end_seconds)` | details |
| `list_action_items(status, person, since, until)`, `update_action_item(id, status)` | to-dos |
| `list_facts(subject, query)`, `get_daily_digest(date)`, `list_people()` | memory |
| `name_speaker(conversation_id, speaker_label, name, remember_voice)` | "Speaker 1 was Alice": renames, re-summarizes, learns the voice so future recordings say Alice, and renames confident matches in other conversations |
| `rename_speaker_everywhere(old_name, new_name)` | "it's Jenn, not Jen": renames in every conversation (merges if the name exists) |
| `reassign_line(line_id, name)` | one line given to the wrong person (`#id` from `get_transcript`) |
| `list_unnamed_speakers(limit)` | voices nobody has named yet, with quotes and a guess, for "who is this?" |

Dates are `YYYY-MM-DD` in `CONVO_TIMEZONE`. Set `CONVO_PUBLIC_URL` to the address your
phone uses (e.g. the server's Tailscale IP) so links work there.

**Hermes** (stdio; add to the profile's `config.yaml`):

```yaml
mcp_servers:
  convo:
    command: /home/steam/projects/barvis-ai/server/.venv/bin/convo-server
    args: [mcp]
    env:
      CONVO_INGEST_TOKEN: ${CONVO_INGEST_TOKEN}     # put the value in the profile's .env
      CONVO_DATA_DIR: /home/steam/projects/barvis-ai/server/data
      CONVO_TIMEZONE: Europe/Berlin
      CONVO_PUBLIC_URL: http://<tailscale-ip>:8000
    timeout: 120
    connect_timeout: 30
```

**Odysseus** (Docker; Streamable HTTP). Its MCP client only does OAuth, so the token goes in
the URL:

```bash
CONVO_MCP_TOKEN=... CONVO_MCP_HOST=172.17.0.1 convo-server mcp --http   # docker bridge IP
# Odysseus → Settings → MCP → add HTTP server:
#   http://host.docker.internal:8001/mcp?access_token=<CONVO_MCP_TOKEN>
```

Use only a local model in whichever frontend reads this. Results contain your transcripts.

## Recognition eval

`eval/recognition.py` measures word error rate and speaker error of the real pipeline on
public AMI meeting audio (CC-BY-4.0, downloaded into `eval/data/`, never committed):

```bash
uv pip install -p .venv/bin/python -e ".[vad,diarization,eval]"
.venv/bin/python eval/recognition.py fetch
CONVO_WHISPER_SERVER_URL=http://127.0.0.1:8178 .venv/bin/python eval/recognition.py run \
    --label server-ecapa --enroll 2
.venv/bin/python eval/recognition.py compare server-ecapa server-pyannote
```

## Checks

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
```

Tests use fakes for whisper, the LLM and speaker embeddings, and the energy VAD, so they
run without models. `scripts/smoke_upload.py` uploads real audio files as 30 s Opus
chunks to a running server for an end-to-end check.
