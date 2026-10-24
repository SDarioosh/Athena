# Athena server

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
- llama-server with an OpenAI-compatible API (`CONVO_LLM_BASE_URL`, `CONVO_LLM_MODEL`).
  `CONVO_LLM_MODEL` is the default; the hub's settings page (Settings → AI Defaults →
  Background & summaries) can pick another model the LLM server lists
  (`PUT /v1/settings/llm {"model": name | null}`, stored in `convo.db`, used from the
  next summary on; `null` goes back to `.env`). The LLM server's URL only comes from
  `.env`.
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

#### Fewer wrong names with pyannote (default with pyannote since 2026-10-24)

Measured with `eval/recognition.py` (see [Recognition eval](#recognition-eval)) in the
sandbox and on the server GPU (`docs/omi-audio-quality.md` §8.3). With
`CONVO_DIARIZER=pyannote`, line splitting (parts ≥ 0.3 s), the line check and
`CONVO_VAD_THRESHOLD=0.35` are **on by default**; set any of them in `.env` to override
(e.g. `CONVO_SPEAKER_LINE_CHECK=false`, `CONVO_VAD_THRESHOLD=0.5` for the old behaviour).
With ECAPA nothing changes. The rest of the table stays opt-in:

| setting | what it does | sandbox effect |
|---|---|---|
| `CONVO_SPEAKER_SPLIT_LINES=true` + `CONVO_SPEAKER_SPLIT_MIN_SECONDS=0.3` | a whisper line that runs over a pyannote turn change becomes one line per speaker (cut at word times); a part shorter than the minimum joins its neighbour (1.5 s by default, which threw away short interjections) | with the line check, see below |
| `CONVO_SPEAKER_LINE_CHECK=true` | each named line's own voice (3 s window) is compared with every enrolled voice; a line that fits another enrolled person at least as well becomes `Unknown` (never taken for a person; fix with `reassign_line`). Needs two or more voices on file | wrong names on the table mic 5.3 → 1.8 % |
| both together | | table mic: wrong names 5.3 → 1.6 %, other people's speech under an enrolled name 8.7 → 4.6 %; late joiners: 4.3 → 2.6 % and 15.8 → 11.0 %; names right unchanged |
| `CONVO_PYANNOTE_VBX_FB=0.2` | pyannote keeps more speakers apart (community-1 default 0.8; `CONVO_PYANNOTE_VBX_FA`, default 0.07, too) | late joiners: newcomers under an enrolled name 15.8 → 7.8 %, but 8–11 speakers for 4 people, names right 95 → 80 %; 0.3 is no useful middle (14.9 %, names right 92 %) |
| `CONVO_VAD_THRESHOLD=0.35` (an existing setting, default 0.5) | lets more quiet, far speech through to whisper | room WER 28.1 → 27.1 %, the others' 47.5 → 44.6 %; server GPU: table 36.1 → 35.2 %, room 28.4 → 27.6 % |

Measured and **not** worth it: `CONVO_PYANNOTE_CLUSTER_THRESHOLD` (in community-1 it
only seeds VBx; 0.2–0.9 gave the same speakers), `CONVO_SPEAKER_MATCH_MARGIN` and
`CONVO_SPEAKER_NAME_MIN_SECONDS` (Omi's naming rules; whole speakers were already named
right), `CONVO_SPEAKER_LINE_FLOOR` (unnames a line that fits its name poorly; barely
moved anything) and `CONVO_SPEAKER_EMBEDDER=pyannote` (names with pyannote's own
WeSpeaker model instead of ECAPA; same names; switching needs a new enrollment, voices
of the other model never match). `CONVO_PYANNOTE_MIN_SPEAKERS`/`_MAX_SPEAKERS` bound the
speaker count per recording; not measured.

The owner loudness hint (below) logs `owner hint: speaker N of M named …` whenever it
names a speaker. It never fired in the eval, but a wrist mic that turns away can make
someone else the loudest: if the log shows it naming the wrong person, set
`CONVO_OWNER_HINT_MARGIN=0`.

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
  `CONVO_WHISPER_MAX_REPEATS` (4). A whole sentence (4 or more different words) said
  again right after itself, in one line or as the next lines, is kept once: that is
  whisper repeating itself over the next pause or someone else's turn, not a person.
  `CONVO_WHISPER_DROP_HALLUCINATIONS=false` turns it off.
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

VAD: Silero runs on each chunk. `CONVO_VAD_THRESHOLD` (0.5; 0.35 by default with
`CONVO_DIARIZER=pyannote`), `CONVO_VAD_MIN_SILENCE_MS` and `CONVO_VAD_SPEECH_PAD_MS` tune
it. A far-field or wrist recording can be 20-30 dB quieter
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
(`?start=&end=` offsets), `/v1/search?q=`, `/v1/action-items` (`?include_notes=true`
adds note items), `/v1/digests/{date}`,
`/v1/sessions`, `/v1/speakers`; `PATCH /v1/action-items/{id}` takes any of `status`,
`kind`, `due_date`, `due_time`, `list_name`, `title` (`null` clears a date, time or list,
and gives the title back from the text). Conversations carry a
signed `link`, and action items and facts a `source_url`, of the form `<CONVO_PUBLIC_URL>/view/<id>?sig=…#t=<seconds>`: an HTML
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
| `list_action_items(status, person, since, until, format)` | to-dos, events and reminders; `format="json"` returns a JSON array for programs (below), notes (kind `note`, `note_title`) included (the hub files voice-note items into its calendar, to-dos and notes from it) |
| `update_action_item(item_id, status, kind, due_date, due_time, list_name, title)` | mark done/dismissed or reopen; change kind (`task`/`event`/`reminder`), date (`YYYY-MM-DD`, `none` clears), time (`HH:MM`, `none` clears), to-do list (`shopping`, `none` clears) or title (at most 60 characters, `none` = from the text); every argument but `item_id` is optional |
| `list_facts(subject, query, limit, confirmed, format)` | facts about people, the user and things; `confirmed=true` keeps only confirmed facts (below); `format="json"` returns a JSON object for programs (the hub copies confirmed facts into its memory from it) |
| `retire_fact(fact_id, reason)` | "forget that": the fact is retired for good (below); answers `Fact #12 retired.`, `Fact #12 was already retired.` or `No fact #12.` |
| `erase_fact(fact_id, reason)` | forget completely: retired, and its words erased where they were said (below); answers `Fact #12 erased: 2 transcript line(s) redacted, 2 conversation summary(ies) being rewritten.`, `Fact #12 was already erased.` or `No fact #12.` |
| `get_daily_digest(date)` | memory |
| `list_people(format)` | speakers and people mentioned; `format="json"` returns each person's stable id for programs (the hub links people to its contacts by it, below) |
| `name_speaker(conversation_id, speaker_label, name, remember_voice)` | "Speaker 1 was Alice": renames, re-summarizes, learns the voice so future recordings say Alice, and renames confident matches in other conversations |
| `rename_speaker_everywhere(old_name, new_name)` | "it's Kath, not Kat": renames in every conversation (merges if the name exists); the person keeps their id |
| `reassign_line(line_id, name)` | one line given to the wrong person (`#id` from `get_transcript`) |
| `list_unnamed_speakers(limit)` | voices nobody has named yet, with quotes and a guess, for "who is this?" |

Dates are `YYYY-MM-DD` in `CONVO_TIMEZONE`. Set `CONVO_PUBLIC_URL` to the address your
phone uses (e.g. the server's Tailscale IP) so links work there.

**Action items in JSON.** `list_action_items(format="json")` returns an array, newest
conversation first, with no citation line:

```json
[{"id": 7, "status": "open", "kind": "reminder", "title": "Send the slides to Greta",
  "explicit": true, "text": "Send the slides to Greta", "owner": "Me", "due": "Friday",
  "due_date": "2025-10-10", "due_time": null, "duration_minutes": null, "location": null,
  "list_name": null, "note_title": null, "time_unclear": false,
  "conversation_id": "…", "conversation_title": "Errands", "conversation_kind": "note",
  "conversation_at": "2025-10-09T08:53:20+00:00",
  "created_at": "2025-10-09T08:55:02+00:00", "timezone": "Europe/Berlin",
  "link": "<CONVO_PUBLIC_URL>/view/<id>?sig=…#t=0",
  "with": [{"id": 4, "name": "Greta"}, {"id": null, "name": "Tom"}]}]
```

`title` is a short imperative summary of at most 60 characters ("Call the plumber about
the tap"), written by the model; when it gives none (or a longer one) code takes the
start of the text (or the title) cut at a word boundary, and a note's title is its
`note_title`. `explicit` is true when the user asked for the item: it has a due date or
time, a `list_name`, kind `reminder`, `event` or `note`, or the voice note used a command
word ("remind me", "todo", "to-do", "task:", "add a task", "add … to my … list", "note
that"). It is worked out when the conversation is summarized and stored, so the hub
files explicit items itself and offers the others as suggestions. An item the user later
gives a date, time, list or reminder/event kind with `update_action_item` becomes
explicit; a re-summary keeps an item explicit. Both fields are also in `GET
/v1/action-items` and `GET /v1/conversations/{id}`. The text listing is unchanged.

`with` (MCP 0.9.0) lists the other people in the item's conversation, for the hub's
open loops ("you told Greta you'd send the slides"): the named speakers of its lines
(by first line) and the people its summary lists, each with their stable person `id`
(following merges and aliases; `null` for a speaker with no person row yet) and current
`name`. People whose name (or a name they appear under there) is a word of the item's
text come first, then the rest by first appearance; no duplicates. Unnamed labels
("Speaker 2", "Unknown") and the owner (owner lines, the owner's person and its aliases,
`CONVO_OWNER_NAME`, words like "me") are never listed; it is `[]` when nobody else is
there and for notes. It is only in the MCP JSON rows, not in the HTTP API.

**Confirmed facts.** A fact is confirmed when no newer fact has replaced it and it was
said in at least `CONVO_FACT_CONFIRM_SOURCES` (default 3) distinct conversations (its own
plus every "said again") that started on at least `CONVO_FACT_CONFIRM_DAYS` (default 2)
distinct dates in `CONVO_TIMEZONE`. `list_facts(confirmed=true)` lists only those, in
both formats. `list_facts(format="json")` returns current facts in the text mode's order
(newest conversation first), with no citation line:

```json
{"facts": [{"id": 12, "subject": "Alice", "about": "person", "person_id": 3,
            "fact": "Alice is vegetarian", "times_said": 3, "days": 2,
            "first_said": "2025-10-09", "last_said": "2025-10-10",
            "confirmed": false, "link": "<CONVO_PUBLIC_URL>/view/<id>?sig=…#t=2"}]}
```

`about` is `owner` when the subject (trimmed, any case) is me, I, myself, owner, the
owner, user, the user, `CONVO_OWNER_NAME` or the name of the enrolled owner voice;
`person` when it is an enrolled speaker, a person named in a conversation or one of their
former names (aliases, below); else `other`. `person_id` is the subject's person id (below) when `about` is `person`, and
the owner's person id when it is `owner` (the owner person, below); null for `other`
or when no such person exists.
`times_said` counts distinct conversations, `days` distinct local dates, and
`first_said`/`last_said` are the first and last of those dates. `confirmed` is true when
the fact meets the thresholds above, so a program can take every current fact with its
counts without knowing them (`confirmed=true` as an argument still filters). `link` is
the same transcript link as in the text. `limit` goes up to 1000 in JSON mode (200 in text), so a
program can hold every fact and reconcile by id: the hub asks for 1000 and retires any
fact id that stops being returned (replaced, retired, or its conversation deleted).

**People.** Every enrolled voice and every person named in a conversation has a row in
`people` with a stable id, so the hub can link them to its contacts. Names match in any
case. `rename_speaker_everywhere` keeps the id and keeps the old name as an alias; if
the new name is already a person, the two are merged and the id of the name that
already existed survives (the other row gets `merged_into` it, its aliases and voice
move, and its old id and names resolve to the survivor). Unnamed `Speaker N` labels are
not people. `list_people(format="json")` lists the people not merged into another, in
the text mode's order (speakers by last heard, then people mentioned, then the rest by
name):

```json
{"people": [{"id": 3, "name": "Alice", "aliases": ["Ally"], "is_owner": false,
             "has_voice": true, "conversations": 4, "first_seen": "2025-10-09",
             "last_seen": "2025-10-12"}]}
```

The owner person is the one with the enrolled owner voice; without one, the person called
`CONVO_OWNER_NAME`, else one called me, I, myself, owner, the owner, user or the user. Only
that person has `"is_owner": true`, and owner facts carry its id. Next to an enrolled
owner, a person called by one of those owner names ("Me" from summaries) is listed as
the owner, with that name among the owner's aliases, not as a person of its own.
`has_voice` means an enrolled or learned voice; `conversations` counts distinct
conversations they spoke in or were named in (under their name or an alias), and
`first_seen`/`last_seen` are the local dates (`CONVO_TIMEZONE`) of the first and last of
those, null if none. The text mode is unchanged.

**Retired facts.** `retire_fact(fact_id, reason)` (write, destructive) is how the owner
forgets a fact from the hub's review page. The fact gets status `retired` (next to
`current` and `superseded`) with `retired_at` and `retired_reason`, and leaves
`list_facts` (both formats), search (keyword index and embeddings), `get_conversation`
and `GET /v1/conversations/{id}`. Facts it had replaced stay replaced. It stays
forgotten: a re-summary of its conversation (e.g. after `name_speaker`) marks the matching
fact retired again (it keeps its id, see below; the hub still reconciles by the facts
returned), and the fact linker is shown retired facts too, so the
same fact said in a later conversation becomes another source of the retired fact
instead of a new one. A later fact that *updates* a retired one is a new belief and is
shown. Calling it again answers "already retired".

**Erased facts** (MCP 0.10.0). `erase_fact(fact_id, reason)` (write, destructive) is what
the hub's Forget calls; it does everything `retire_fact` does and takes the fact's words
out of every place this module serves or stores them, while the rest of the conversation
stays:

| Where | What happens |
|---|---|
| Transcript | The line at the fact's `source_at` (whole seconds from the conversation start, so the line that starts within that second) reads `[forgotten]` (`segments.erased = 1`); speaker and times stay, so transcript links still open at that spot. Done in the fact's conversation and in every one that said it again (`fact_sources`). |
| Title, summary, key points, topics | Taken off every read path at once (`get_conversation` says "being rewritten") and moved to `forget_rewrites`; the worker's local model rewrites them without the fact (`ForgetRewrite`, keep everything else word for word), then code drops any sentence, key point or topic that still has the fact's words in order. If the model is down they stay hidden and are retried on the next pass. |
| Daily digest | Every digest that lists the conversation is taken down and written again once its conversations are rewritten. |
| Search | The line index follows the line; the conversation's summary and transcript entries are redone; the fact's and the conversation's embeddings are deleted (the conversation is embedded again after the rewrite). |
| Kept audio | With `CONVO_KEEP_AUDIO=true`, the audio chunks under the line are deleted. |
| Database file | `PRAGMA secure_delete` is on, both full-text indexes are merged and the write-ahead log is checkpointed after an erase, so the words don't stay in free pages. |

A later repeat linked "same" to an erased fact is erased the same way; a re-summary of a
conversation it was said in hides the new notes until they are rewritten too. Not
touched: action items (their own text), people, and facts the erased one had replaced.
The fact's own sentence stays in the `facts` table, never served, so that a repeat can be
recognized. It can't be undone. Calling it again erases only what is new and answers
"already erased"; a fact retired earlier can be erased later.

**Retention** (`CONVO_TRANSCRIPT_RETENTION_DAYS`, default 0 = keep forever). When set,
the worker (at most hourly) deletes the transcript lines of conversations that ended more
than that many days ago, and kept audio of finished sessions older than that. Pending and
failed conversations keep their lines until they are summarized. A pruned conversation
still answers everything its notes hold: title, summary, key points, topics, people,
facts (with links) and action items, `search_conversations`, `list_facts`, digests.
It no longer answers exact wording: `search_transcripts` doesn't find its lines,
`get_transcript` says when the transcript was removed, and its links (`/view/<id>#t=…`)
open the page with the summary, facts and that note instead of the lines. It can't be
re-summarized.

**Hermes** (stdio; add to the profile's `config.yaml`):

```yaml
mcp_servers:
  convo:
    command: /home/<user>/projects/barvis-ai/server/.venv/bin/convo-server
    args: [mcp]
    env:
      CONVO_INGEST_TOKEN: ${CONVO_INGEST_TOKEN}     # put the value in the profile's .env
      CONVO_DATA_DIR: /home/<user>/projects/barvis-ai/server/data
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

**Uploads through the MCP server.** `convo-server mcp --http` also serves two plain HTTP
routes next to `/mcp`, behind the same token (`Authorization: Bearer` or
`?access_token=`). They are not MCP tools, so no model ever sees them:

| route | same as |
|---|---|
| `POST /ingest/chunk` | `POST /v1/ingest/chunk` (same multipart fields, same JSON answer) |
| `POST /sessions/{session_id}/end` | `POST /v1/sessions/{id}/end` |
| `GET /health` | `GET /health` (here behind the token) |
| `GET /conversations`, `GET /conversations/{id}`, `GET /conversations/{id}/transcript` | the `/v1` routes, same query |
| `GET /action-items`, `PATCH /action-items/{id}` | the `/v1` routes |
| `GET /digests/{date}`, `POST /digests/{date}` | the `/v1` routes |
| `GET /settings/llm`, `PUT /settings/llm` | the `/v1` routes (the hub's settings page: the summary model) |

The read routes are for the phone relay's T5 sync when it is paired through Odysseus
(Odysseus forwards them for a token with `sound:read` / `sound:write`); the MCP token can
read the same data through the tools anyway.

Each request is forwarded unchanged to this module's own ingest API
(`http://<CONVO_HOST>:<CONVO_PORT>`, `127.0.0.1` when `CONVO_HOST` is `0.0.0.0`) with
`CONVO_INGEST_TOKEN`, so there is one ingest code path and the ingest token, which also
signs transcript links, never leaves the module. The API's status and `detail` come back
as they are; the body is capped like the API's (25 MB of audio); if `convo-server serve`
isn't running the answer is 502 with a readable `detail`. Odysseus uses these routes to
add recordings (its call recorder, phones paired through it) with the URL and token of
the `convo` MCP server it already has. **The MCP token can therefore add recordings**:
treat it like the ingest token, and keep the MCP port on loopback, the Docker bridge or
Tailscale. uvicorn's access log shows `access_token=***` instead of the token.

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
   An action item is a concrete thing to do with an object and ideally a time ("send the
   slides to Greta", "call the plumber on Friday"). A statement of intent, priority,
   principle or decision ("I should focus on X before Y", "stop prioritizing Z", "we
   decided to …", "from now on …") is not one: the facts prompt makes it a fact about
   the speaker, phrased as a decision (subject the owner: "The owner decided to stabilise
   the base system before side projects."). A vague wish with nothing to act on ("figure
   out how it could use less battery") is a plan fact too, unless it has a time, a named
   list or a command word. In a voice note, code moves any undated task without a list
   or command word that the model still lists into the facts (subject the owner's name,
   `CONVO_OWNER_NAME`): one that says a decision or principle ("from now on", "going
   forward", "decided", "focus on", "prioritize", "stop/quit/avoid …ing", or an intent
   word such as "should", "need to", "want to", "going to" with a habit such as "be
   more …", "try to", "start/keep …ing", "sleep more") becomes "The owner decided …",
   and a vague wish ("figure out how/whether …", "think about how …") "The owner plans
   …", unless the facts already say it. An intent word alone keeps a concrete to-do ("I
   should email Bob about the invoice" stays a task), and so does an item another person
   should do. Every item gets a `title` and `explicit` (above).
   Each item has a `kind`: `event` (meeting, appointment, call: "meeting with Jason on
   Wednesday at 12 pm"), `reminder` ("remind me at 5 to call Mom") or `task` ("get the
   proposal done on Wednesday"), plus `duration_minutes` and `location` for events if
   said. In a voice note, code also finds appointments the model left out: a sentence
   with an appointment word (meeting, call, lunch, appointment, dentist, …), a day and a
   time, not in the past tense ("Meeting with the Meshtastic team Tuesday 11 a.m."),
   becomes an event (text without the day and time) unless an item already says most of
   its words or has its day from its line; an undated task or reminder that says it gets
   its day and time and becomes the event. An event is named as said: when the model writes
   "Meet X" for "meeting with X", code makes it "Meeting with X" ("Meet the Meshtastic
   team" reads like getting to know them). A fact that only repeats a dated event of
   the same conversation with its weekday or time ("… has a meeting with the Meshtastic
   team on Tuesday at 11 a.m.") is left out: the event is on the calendar. Something repeating ("every Monday") becomes a task "Set up a repeating event:
   …". The model returns the time as said; `resolve_time` in `dates.py` turns it into
   `due_time` (HH:MM, `CONVO_TIMEZONE`): "12 pm"/"noon" → 12:00, "3:30 pm" → 15:30, "half
   past two" → 14:30, "midnight" → 00:00; a bare hour 12 → 12:00, 1–6 → afternoon, 7–11 →
   morning with `time_unclear` set (it could be the evening); "in the evening",
   "tonight" decide. A time that can't be read leaves `due_time` empty with
   `time_unclear`. A time without a date is today (tomorrow if it had already passed).
   `list_name`: the to-do list the user named ("add milk to my shopping list" → text
   "Milk", list `shopping`), without "my"/"the"/"list". If the model leaves the phrase
   in an "Add …"/"Put …" text, code takes the list from it and drops it from the text.
   Odysseus adds the item to its to-do list with that name (or creates it).
   **Notes (voice notes only, explicit commands only):** "create a note battery
   dimensions are 13mm x 14mm x 3mm" becomes an item of kind `note` with text "Battery
   dimensions are 13 mm x 14 mm x 3 mm" and `note_title` "Battery dimensions"; "add to
   my hardware notes: the screen is 1.4 inch" also gets `list_name` `hardware` (the
   notes to add it to, without "my"/"the"/"notes"). Code keeps a note only when the
   voice note says "note"/"notes", strips command words the model leaves in, and gives
   a note no date, time or owner. It also finds the command sentences itself: one the
   model missed becomes a note (untitled; the hub titles it from the text), and an
   undated task the model wrote for it becomes that note (never a to-do the user asked
   for out loud). Named notes need not say "notes" right after a note command: "make a
   note in voice recorder that the case needs to be 3 mm bigger", "… for the voice
   recorder: …", "… under car, …" and "add to my car note: …" go to `voice recorder` /
   `car`; a time, a person or "in case …" there names no notes ("make a note for later:
   …", "make a note for Sam that …"). When the model's note leaves the notes out, they
   come from the command. In a conversation a `note` becomes a `task`.
   **Spoken commands in voice notes** are caught in code even when the model lists no
   item: to-dos ("make a todo to …", "to-do: …", "new task, …", "create a to-do called
   …", "make a to-do for tomorrow to …", "remind me [at 5 / tomorrow / on Friday] to …",
   "remind me about …", "add … to my to-do list"), list lines ("add eggs to my shopping
   list [for Friday]", "add to the shopping list: oat milk", "put … on my work to-do
   list") and notes ("note: …", "note to self, …", "new note: …", "add to my notes: …").
   Words said before a command don't hide it ("um", "okay so", "hey", "can you", "I want
   to", "let's"), nor do an ellipsis or "uh" inside it, and "…, thanks" is not part of
   it. One sentence with several commands ("make a todo to get eggs and make a note that
   the car is broken") gives one item per command, and an appointment said in the same
   sentence as a command is still an event. "Remind me" with a time is a `reminder`. A
   model item for a list command that lacks the list gets it. A comma list of short
   things for a named list ("add eggs, milk and bread to my shopping list") is one item
   per thing, so each is its own line; "salt and pepper" stays one. A list said line by
   line ("Make a to-do list. Get eggs. Fix the car.", "Make a shopping list. Eggs.
   Milk.", "Shopping list: eggs, milk, bread", "… for tomorrow: …") gives one item per
   line on that list (the plain to-do list for a to-do list) until a closer ("okay,
   that's it"), another command or a long sentence. The day and time said in a command
   are the item's when the model drops them ("make a to-do to get eggs tomorrow" →
   "Get eggs" due tomorrow; "remind me at 5 to call mom" → a reminder at 17:00), and a
   midnight the model gives a command that names no time is dropped.
   are left out of the text `list_action_items`, the digest and `GET /v1/action-items`
   (add `?include_notes=true`); the JSON listing has them. Odysseus files each into its
   notes (a note of its own, or a line on the named notes) and marks it done here.
   Items on a shopping-type list (`list_name` in `CONVO_DIGEST_SKIP_LISTS`, default
   `Shopping,Groceries,Grocery`, any case) are left out of the daily digest too; they
   stay in `list_action_items`.
4. **Facts**, then a **linking** call against related known facts (same subject or
   matching words): a repeat becomes another source of the known fact; a changed value
   replaces the older one (by conversation time, so re-summarizing an old conversation
   can't overwrite newer knowledge). A link is ignored unless the two facts share at
   least two words besides their subject. `list_facts` shows only current facts, each
   with "said again" and "replaces:" lines that link every source transcript.

A re-summary (e.g. after a speaker is renamed) keeps what was known about the old items:
the id, status, creation time and `explicit` of action items (and the kind, date, time,
list and title if the user changed them with `update_action_item`; an item with the same text is matched
before a similar one said nearby), and of facts the id (the same text, or the same
transcript line read alike), the other sources, which facts it replaced, and whether the
owner retired it. New facts get ids no old fact of the conversation had. Stable ids
matter: Odysseus files voice-note items and facts by id, and a fact under a new id was a
second belief there (the same fact twice in its memory when the owner had confirmed the
first).

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

With `--audio sim`, `--late-joiners` silences everyone but the two most active speakers
for the first half of the window (people joining a conversation) and `--owner-dips DB`
turns the owner's voice down by 6..DB dB for 2–8 s every 5–20 s (a wrist mic turning away
as the arm moves). Besides WER and DER, each run reports named right/wrong, owner
precision and recall (lines labelled as the owner that are the owner's, and the owner's
lines labelled as the owner) and `unenrolled_named` (speech of people with no voice on
file that got an enrolled name). `--cache` reuses whisper results (with word times) and
pyannote's turns, so runs that only change naming take seconds.
Results of the capture-quality round (2026-10-23/24): `docs/omi-audio-quality.md` §8.

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
