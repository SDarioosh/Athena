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
- A speaker with a single line joins the closest speaker with several lines when the
  voice is at least `CONVO_SPEAKER_ABSORB_THRESHOLD` (0.30) similar, instead of becoming
  one more "Speaker N" (one noisy piece often ends up alone). 0 turns it off.
- whisper gets a prompt: the names of known speakers (`CONVO_WHISPER_PROMPT_NAMES`,
  default on), so they are spelled right, plus `CONVO_WHISPER_PROMPT` (empty by default).
  Measured on AMI (`eval/recognition.py`): the names made WER and DER slightly better;
  a 12-word vocabulary list in `CONVO_WHISPER_PROMPT` broke whisper's timestamps (lines
  stretched over 30 s: false alarm 8% → 58%, wrong names 2% → 6%). Leave it empty
  unless the eval shows it helps with your vocabulary.
  In the transcription-quality eval a 26-name list was neutral, but a descriptive
  vocabulary sentence raised WER by 5.7 points and a punctuation-style sentence by 1.6
  (whisper skipped speech after them): keep it to a short list of words.
- Text whisper invents is dropped: on a short clip of noise (a cough, a rustle the VAD
  took for speech) large-v3-turbo often answers "Thank you.". A segment that is only such
  a stock phrase is dropped when whisper was unsure of it (mean token log-probability
  below `CONVO_WHISPER_DROP_PHRASE_LOGPROB`, -0.7), so a real "thank you" stays. Subtitle
  credits and lines with no words go too, and repetition loops are cut to
  `CONVO_WHISPER_MAX_REPEATS` (4). `CONVO_WHISPER_DROP_HALLUCINATIONS=false` turns it off.
- Speech goes to whisper in clips of up to `CONVO_WHISPER_WINDOW_SECONDS` (28): the VAD
  regions of a run, padded by `CONVO_WHISPER_PAD_SECONDS` (0.25), are packed in order
  and the silence between them is shortened to `CONVO_WHISPER_WINDOW_GAP_SECONDS` (1.0).
  Timestamps are mapped back to the recording; a whisper segment that spans a cut is
  split by its token times. `CONVO_WHISPER_WINDOW_SECONDS=0` sends every region alone
  (the behaviour before). `CONVO_WHISPER_CONTEXT_WORDS` (0 = off) adds the end of the
  previous clip's text to the prompt; it made the eval worse.
- Decoding settings go with every whisper request (whisper-server form fields, the same
  flags for whisper-cli): `CONVO_WHISPER_BEAM_SIZE`, `_BEST_OF`, `_SUPPRESS_NON_SPEECH`,
  `_NO_SPEECH_THOLD`, `_LOGPROB_THOLD`, `_ENTROPY_THOLD`, `_CARRY_INITIAL_PROMPT`,
  `_TEMPERATURE_INC`. They override whatever whisper-server was started with.

## Deploy: one command, runs in the background

```bash
server/scripts/deploy.sh deploy     # update to origin/main, install, restart, check; then Odysseus
server/scripts/deploy.sh odysseus   # only update Odysseus
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
5. Updates Odysseus: runs `scripts/fork_deploy.sh deploy` in the Odysseus checkout, which
   rebuilds and restarts Odysseus (without running this script again). The checkout is
   found from the running Odysseus container (`sudo docker ps`, so it may ask for your
   password), or set `BARVIS_SOUND_ODYSSEUS_DIR=/path/to/odysseus` in `server/.env`;
   `off` skips this step. If no Odysseus is found, it says so and stops there.

Everything is read from `server/.env` (settings: bottom of `.env.example`). Without
systemd it starts background processes instead (pid files and logs in
`data/run/`, `data/logs/`). It stops with an error if a port is already taken, e.g. by a
`serve` you started by hand: stop that first. The hub's `scripts/fork_deploy.sh deploy`
also runs this script before it rebuilds Odysseus, so either command updates both
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

VAD: Silero runs on each chunk. `CONVO_VAD_THRESHOLD`, `CONVO_VAD_MIN_SILENCE_MS` and
`CONVO_VAD_SPEECH_PAD_MS` tune it. A far-field or wrist recording can be 20-30 dB quieter
than a close mic, and Silero then misses some of its speech, so whisper never gets it.
`CONVO_VAD_AGC_MAX_GAIN` (1 = off, the default) lets Silero score a copy of each chunk
gained toward a `CONVO_VAD_AGC_TARGET` (0.8) peak, at most that many times; the stored
audio is unchanged. `CONVO_ASR_AGC_MAX_GAIN` (1 = off) does the same for each clip sent to
the transcriber, only when the clip is quiet; the diarizer always gets the ungained audio.
Both are off until the server eval decides (`docs/omi-audio-quality.md` §7: it helped on
very quiet input, but hurt speaker naming at the watch's level).

`CONVO_TRANSCRIBER=parakeet` swaps whisper for NVIDIA Parakeet TDT 0.6B v3 (int8, via
sherpa-onnx on CPU, extra `parakeet`, `CONVO_PARAKEET_MODEL_DIR`); see `.env.example`.
whisper stays the default.

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
   conversation (kept when in doubt), and pick the kind: meeting, call, appointment,
   casual, other. A discarded conversation keeps
   its transcript (still searchable; `POST /v1/conversations/{id}/summarize` retries) but
   gets no summary, action items or facts, and stays out of digests.
   Plain junk is discarded first, without a model call, by fixed rules
   (`convo_server/relevance_rules.py`): under 3 words (`too_short`), only filler words
   such as "okay", "mm", "yeah" (`filler`), or a microphone check such as "testing, one,
   two" (`mic_check`). These rules only discard: anything with one content word, a name,
   a number, "yes"/"no", or words in another language goes to the model. No rule keeps a
   conversation without the model, because the model is what spots a TV playing.
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

`--audio array` uses the far-field table mic instead of the headset mix (`--mic
Array1-01` still works), `--audio sim --distance N` simulates a room with the others N m
away, `--opus-kbps KBPS` (or `--opus`) round-trips the stream through Opus like the
recorders, and `--level DBFS` sets the input level before encoding (the T-Watch's 30 dB
mic gain puts speech near -24 dBFS). `--set field=value` overrides any setting. More in
the script's docstring and `docs/omi-audio-quality.md` §7.

Measured 2026-10-20 in the cloud sandbox (large-v3-turbo on CPU, `--diarizer none`, 3
meetings x 300 s, ~2,100 words). "Before" turns the transcription changes of that round
off. These numbers include a VAD gain that was on by default then; it is now off by
default (`--set vad_agc_max_gain=4` turns the current one on), so the "after" column needs
a rerun on the server:
`--set whisper_window_seconds=0 whisper_drop_hallucinations=false
whisper_suppress_non_speech=false`.

| condition | before | after |
|---|---|---|
| far-field, T-Watch level (`--mic Array1-01 --level -24 --opus 16`) | 39.8 % | 38.9 % |
| the same at 24 kbps (`--opus 24`, the new firmware default) | | 34.4 % |
| far-field, raw level (`--mic Array1-01 --opus 16`) | 50.2 % | 40.5 % |
| headset mix, WAV | 23.7 % | 23.5 % |

Most remaining errors are deletions where people talk over each other: whisper writes one
voice. large-v3-turbo-q5_0 scored the same as large-v3-turbo (f16) with less than half
the memory; large-v3-q5_0 was no better.

## Checks

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
```

Tests use fakes for whisper, the LLM and speaker embeddings, and the energy VAD, so they
run without models. `scripts/smoke_upload.py` uploads real audio files as 30 s Opus
chunks to a running server for an end-to-end check.
