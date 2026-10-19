# odysseus-sound server

The `convo_server` package (CLI `convo-server`, env prefix `CONVO_`) keeps the project's
old name, convo-ai. Don't rename it.

Device-agnostic ingestion + processing pipeline. Everything runs on this machine:
Silero VAD, whisper.cpp, ECAPA speaker embeddings (SpeechBrain), llama-server, SQLite.

```
device/phone ──POST /v1/ingest/chunk──▶ decode (ffmpeg) ─▶ Silero VAD ─▶ chunk + speech spans
                    ◀── {"ok", "stop"} ──  (stop on N min trailing silence / max duration)

session ended (Stop, server stop, idle) ─▶ reassemble by sequence ─▶ whisper.cpp on speech
  ─▶ diarize (enrolled voices) ─▶ segments ─▶ delete raw audio ─▶ split on >3 min gaps
  ─▶ llama-server, small focused calls: triage (discard TV/noise, pick the kind) ─▶
     notes with a per-kind template ─▶ action items (+ due dates) ─▶ facts ─▶ link
     facts to known ones ─▶ FTS5 (+ optional embeddings) ─▶ daily digest
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

## Deploy: one command, runs in the background

```bash
server/scripts/deploy.sh deploy     # update to origin/main, install, restart, check
server/scripts/deploy.sh status     # what runs and whether it answers
server/scripts/deploy.sh logs mcp   # serve | mcp | whisper; add -f to follow
server/scripts/deploy.sh stop       # start | restart | stop
```

It works from any directory and needs no sudo. `deploy`:

1. Refuses tracked local changes, fetches the remote and fast-forwards `main` (or checks
   out the ref you pass).
2. Creates `server/.env` with new tokens if there is none, then creates or updates
   `.venv` (`uv` if installed) with the `vad,diarization` extras (`+pyannote` when
   `CONVO_DIARIZER=pyannote`).
3. Runs these as systemd **user** services that start at boot (it turns on
   `loginctl enable-linger`) and restart on a crash:

   | service | runs | when |
   |---|---|---|
   | `barvis-sound` | `convo-server serve` | always |
   | `barvis-sound-mcp` | `convo-server mcp --http` | `CONVO_MCP_TOKEN` is set |
   | `barvis-sound-whisper` | `BARVIS_SOUND_WHISPER_CMD` | that is set |

   `serve` and MCP restart on every deploy; whisper only when its command changed. A
   session interrupted by the restart is processed again.
4. Waits until each answers (`/health`, `/mcp`, the whisper URL). If the new commit
   doesn't come up, it checks out the previous one and restarts that.

Everything is read from `server/.env` (settings: bottom of `.env.example`). Without
systemd it starts background processes instead (pid files and logs in
`data/run/`, `data/logs/`). It stops with an error if a port is already taken, e.g. by a
`serve` you started by hand: stop that first. On the server, the hub's
`scripts/fork_deploy.sh deploy` runs this script before it rebuilds Odysseus
([hub docs/fork/deploy.md](https://github.com/SDarioosh/Barvis/blob/barvis/docs/fork/deploy.md)).

## Run by hand

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
| `kind`        | `conversation` (default) or `note` (voice note); fixed per session  |

A **voice note** (`kind=note`) is the owner talking to themselves: no speaker matching
(every line is the owner), never split on gaps or joined with nearby conversations, never
discarded as too short, and summarized with a note prompt (the idea, plus the to-dos it
implies). It shows up as a conversation with `kind` `note`. A chunk whose `kind` differs
from its session's is rejected with 400. `POST /v1/ingest/chunk.json` takes the same
`kind` field.

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

## Summaries

Each conversation gets several small `json_schema` calls instead of one big one (a
small local model does better on narrow questions):

1. **Triage**: discard a TV, radio or podcast playing, noise, gibberish or no real
   conversation (kept when in doubt; under 3 words is discarded without a call), and pick
   the kind: meeting, call, appointment, casual, other. A discarded conversation keeps
   its transcript (still searchable; `POST /v1/conversations/{id}/summarize` retries) but
   gets no summary, action items or facts, and stays out of digests.
2. **Notes** (title, summary, key points, people, topics) with a template per kind, e.g.
   decisions and owners for meetings, instructions and the next date for appointments.
3. **Action items** (promises, requests, appointments), each written as a task, with
   `due_date` (YYYY-MM-DD). When the deadline names a day ("by Friday", "October
   thirty-first", "tomorrow") the date is worked out in code (`convo_server/dates.py`)
   from the conversation date in `CONVO_TIMEZONE` ("by Friday" said on Wednesday
   2026-10-14 → 2026-10-16); the model's date is used only for vaguer deadlines.
4. **Facts**, then a **linking** call against related known facts (same subject or
   matching words): a repeat becomes another source of the known fact; a changed value
   replaces the older one (by conversation time, so re-summarizing an old conversation
   can't overwrite newer knowledge). A link is ignored unless the two facts share at
   least two words besides their subject. `list_facts` shows only current facts, each
   with "said again" and "replaces:" lines that link every source transcript.

A re-summary (e.g. after a speaker is renamed) keeps what was known about the old items:
the status of action items, the other sources of a fact and which facts it replaced.

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
