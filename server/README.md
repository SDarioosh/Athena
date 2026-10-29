# Athena server

The Python package is `athena`, with the CLI `athena`. The old names keep working:
`convo_server` is an alias of the package (`import convo_server.db.store` and
`python -m convo_server.cli` give the same modules), and the `convo-server` CLI runs the
same code, so deployed services and cron jobs need no change. The env prefix `CONVO_`
keeps the project's old name, convo-ai. Don't rename it.

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
| `CONVO_SPEAKER_LINE_CHECK=true` | each named line's own voice (3 s window) is compared with every enrolled voice; a line that fits another enrolled person at least as well becomes `Unknown` (never taken for a person; fix with `reassign_line`). The window stops at neighbouring lines labelled someone else, and a line with under 1 s of its own audio keeps its name (before 2026-10-26 short lines in a quick exchange were judged on the other people's voices and often became `Unknown`). Needs two or more voices on file | wrong names on the table mic 5.3 → 1.8 % |
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

#### Sentence fragments become one line

Whisper ends lines mid-sentence and the pyannote line split cuts at every turn change, so
"I just" / "found this" could be two lines. After speakers are named, a line that doesn't
end a sentence (`.`, `?`, `!`, `…`) joins the next one when the pause is at most
`CONVO_JOIN_GAP_SECONDS` (1.0) and the joined line at most `CONVO_JOIN_MAX_SECONDS`
(30): always for the same speaker; when one of them is `Unknown`, the line takes the
other's name; two different named speakers stay apart (often a real interruption).
`CONVO_JOIN_LINES=false` keeps whisper's lines. Only new recordings change.

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
  default on), so they are spelled right, plus `CONVO_WHISPER_PROMPT` (empty by default)
  and the words set in the hub's settings (Settings → AI Defaults → "Transcription
  vocabulary": comma-separated, at most 50, `GET`/`PUT /v1/settings/vocabulary
  {"words": "RokTalk, Meshtastic" | [...] | null}`, stored in `convo.db`, used from the
  next recording on). Order: `CONVO_WHISPER_PROMPT`, the words, then the names.
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
- Zoom names become people (`athena/meetings/people/names.py`, imported only by the meeting
  pipeline, never at startup). Each track's display names are cleaned first: pronouns
  ("(she/her)"), bracketed tags, "| Company", " - Company" (spaced dash only, so
  "Smith-Jones" stays), "@ Company", device suffixes ("Kath's iPhone", "(iPad)"), titles
  and credentials ("Dr.", ", PhD"), emoji and stray punctuation go (of the parts between separators the first that is a
  name is kept, so "she/her | Kath Dlamini" and "iPhone | Kath Dlamini" are Kath Dlamini; a
  room part makes the whole name a room); ALL-CAPS and
  all-lowercase names are title-cased ("MARCUS MCDONALD" → "Marcus McDonald"), mixed case
  is kept ("van der Berg", "O'Neil"). A name that is only a device, a room, conference
  hardware or a label ("iPhone", "Zoom user", "Conference Room 2", "Huddle 4", "Lobby",
  "Polycom", "Logitech Rally", "SM-G991B") makes no person ("Jordan Hall" and a bare
  "Hall", "Mac" or "Dell" are names; "Mac Pro" and "Dell XPS 13" are not): the track gets an unnamed
  label ("Speaker N"), which `name_speaker` can rename. A cleaned name that matches
  `CONVO_OWNER_NAME` or `CONVO_ZOOM_OWNER_NAMES` is the owner. The owner keeps their own name on every owner
  line (meetings, voice notes, written notes) and in `list_people`: the enrolled owner
  voice's name, else `CONVO_OWNER_NAME`, never the Zoom name. The track's Zoom names
  become aliases of the owner, so the next meeting finds them again; an owner person
  named by a Zoom name before this rule is renamed (same id, the old name an alias). Otherwise it is matched exactly (any case) against
  people names and aliases; an unknown name becomes a new person. There is no first-name
  guessing ("Kath Dlamini" is not merged into "Kath"): a wrong merge mixes two people,
  while `rename_speaker_everywhere("Kath Dlamini", "Kath")` merges them for good. Every
  display name a track used, raw and cleaned, is kept as an alias, so a participant who
  renames mid-meeting stays one person and the next meeting finds them again.
- Learning voices from meetings (`athena/meetings/people/voices.py`,
  `CONVO_LEARN_MEETING_VOICES`, default true; `false` turns it off; imported only after a
  meeting's lines are stored, and only with the setting on). A named participant's own
  track is one known voice, so a few seconds of it become that person's voice samples and
  the diarizer recognizes them in later in-person recordings:
  - Tracks: each named participant's own track. Not a room or device track ("Conference
    Room", "iPhone": several voices), decided exactly as transcription decides it (any
    display name the track ever had), not a personal device track ("Anna's iPhone",
    "Kath (mobile)": her lines carry her name, but a phone is passed around and put on
    speakerphone), not an unnamed "Speaker N" track, not a track with under 10 s of speech
    in its kept lines. The owner's track (`CONVO_ZOOM_OWNER_NAMES`) adds samples to
    the enrolled owner voice (`is_owner`) only; without an enrolled owner it is skipped, so
    a meeting never makes a second owner.
  - Windows: 3 s of the track's own VAD speech that also lies in its kept transcript
    lines (after bleed is dropped: speech that gave no line, such as a muted neighbour's
    murmur, is never learned), never while another track has VAD speech (±0.5 s): no
    crosstalk, no bleed of someone else's voice. Up to 12 windows spread over
    the meeting are decoded and the 3 loudest are embedded.
  - Storage: `voice_samples` rows in `convo.db` with `source='meeting'` and the meeting's
    session id; voiceprints never leave the server. A person keeps at most 3 meeting
    samples (the oldest are replaced first; processing a meeting again replaces its own);
    enrolled and named samples are never replaced. Matching uses them like every other
    sample (the mean of the closest few). `rename_speaker_everywhere` moves them with the
    rest of the voice when two people merge.
  - Then unnamed speakers ("Speaker N") of other recordings whose stored voice clearly
    matches a learned person are renamed, exactly as after `name_speaker`
    (`CONVO_SPEAKER_RELABEL_THRESHOLD`, margin 0.05, never in a recording that already has
    lines by that person); those conversations are re-summarized. The log lists the counts.
  - Cost: only the speaker embedder the worker already holds for diarization (ECAPA), on
    at most 3 windows per person, after the meeting; never clustering and never pyannote.
    With `CONVO_DIARIZATION=false` (no embedder) or `CONVO_SPEAKER_EMBEDDER=pyannote` it is
    skipped with one log line per meeting.
- Decoding settings go with every whisper request (whisper-server form fields, the same
  flags for whisper-cli): `CONVO_WHISPER_BEAM_SIZE`, `_BEST_OF`, `_SUPPRESS_NON_SPEECH`,
  `_NO_SPEECH_THOLD`, `_LOGPROB_THOLD`, `_ENTROPY_THOLD`, `_CARRY_INITIAL_PROMPT`,
  `_TEMPERATURE_INC`. They override whatever whisper-server was started with.

## Deploy: one command, runs in Docker

```bash
server/scripts/deploy.sh deploy     # update to origin/main, build, restart, check; then Odysseus
server/scripts/deploy.sh odysseus   # only update Odysseus
server/scripts/deploy.sh status     # containers, checkout, an older native install
server/scripts/deploy.sh logs mcp   # serve | mcp | whisper; add -f to follow
server/scripts/deploy.sh stop       # start | restart | stop
```

Athena runs as the containers of `server/compose.yml` (next section); Docker is the one
deploy path (owner decision, 2026-10-29). It works from any directory, as your user
(`sudo docker` only if `docker` needs it). `deploy`:

1. Checks that `docker compose` works, and refuses an older native install that still runs
   unless you asked for the switch (below). Nothing is changed when it refuses.
2. Refuses tracked local changes, fetches the remote and fast-forwards `main` (or checks
   out the ref you pass).
3. Creates `server/.env` with new tokens if there is none.
4. Builds the images and (re)creates what changed (`docker compose up -d --build
   --remove-orphans --wait`), and waits until every container is healthy. If the new
   commit doesn't come up, it checks out the previous one and starts that. The containers
   have `restart: unless-stopped`, so Docker starts them at boot and after a crash; no
   systemd unit is written.
5. Updates Odysseus: runs `scripts/fork_deploy.sh deploy` in the Odysseus checkout, which
   rebuilds and restarts Odysseus (without running this script again). The checkout is
   found from the running Odysseus container (`docker ps`), or set
   `ATHENA_ODYSSEUS_DIR=/path/to/odysseus` in `server/.env`; `off` skips this step. If no
   Odysseus is found, it says so and stops there.

A deploy runs the `deploy.sh` that was on disk when it started, also after it moved the
checkout, so a change to `deploy.sh` takes effect from the deploy after the one that
brought it.

**During a meeting** `deploy`, `start` and `restart` refuse to run: restarting the API
would end the recording. They ask `GET /v1/meetings` (with `CONVO_INGEST_TOKEN`) first; if
a bot is active they stop with an error naming the meeting (title, Zoom number, state, id)
and change nothing, not even the checkout. Let the meeting end (or stop the bot), then run
it again, or add `--force` to restart anyway (`server/scripts/deploy.sh deploy --force`).
The hub's `scripts/fork_deploy.sh` passes no flags: there, `ATHENA_FORCE=1
scripts/fork_deploy.sh deploy` does the same (read from the environment only, never from
`.env`; `BARVIS_SOUND_FORCE=1` works too). Without forcing, the hub run deploys Odysseus
and ends with "module deploy failed" pointing at this message. If the API doesn't answer
(not running, no token, an error), there is no meeting to protect: the command goes on as
before and prints a note.

Settings are read from the environment or `server/.env` (bottom of `.env.example`):
`ATHENA_RUNNER` (`docker`, or `auto`, the default), `ATHENA_REMOTE`, `ATHENA_BRANCH`,
`ATHENA_ZOOM_SDK_DIR`, `ATHENA_ODYSSEUS_DIR`, `ATHENA_FORCE`. Each is also read under its
old name `BARVIS_SOUND_*` (`BARVIS_SOUND_RUNNER`, …): the environment wins over `.env`,
and within one of them the `ATHENA_*` name wins. The native runner's
`BARVIS_SOUND_EXTRAS` and `BARVIS_SOUND_WHISPER_CMD` are not used any more (the image takes
`CONVO_DOCKER_EXTRAS`; whisper is a container or `CONVO_DOCKER_WHISPER_URL`). The hub's
`scripts/fork_deploy.sh deploy` also runs this script before it rebuilds Odysseus, so
either command updates both
([hub docs/fork/deploy.md](https://github.com/SDarioosh/odysseus-expanded/blob/barvis/docs/fork/deploy.md)).

### Switching from the native install

An older `deploy.sh` ran Athena natively: systemd user services `barvis-sound`,
`barvis-sound-mcp` and `barvis-sound-whisper` (or background processes with pid files in
`data/run/`). While one of them still runs (or is enabled), `deploy`, `start`, `restart`
and `stop` refuse with `ATHENA_RUNNER` unset or `auto`, list what runs and change nothing;
`status` shows it too. A runner set to `systemd` or `background` is an error as well. To
switch, in `server/.env`:

```bash
ATHENA_RUNNER=docker               # BARVIS_SOUND_RUNNER=docker works too
COMPOSE_PROFILES=whisper-gpu       # or whisper-cpu; or CONVO_DOCKER_WHISPER_URL=... (next section)
```

then `server/scripts/deploy.sh deploy`. It builds the images first (a failed build stops
nothing), then stops and disables the native services, keeping their unit files and
`.venv` for the way back, and starts the containers on the same ports, with the same
`.env` and data dir, so Odysseus reaches MCP at the same URL. Without a whisper choice it
refuses, so the switch never leaves recordings untranscribed. Only this checkout's native
services are stopped (a unit's `WorkingDirectory`, the pid files in its data dir): a copy
next to the real install never stops the real one, and with a compose project name of its
own (`COMPOSE_PROJECT_NAME`) never takes Athena's containers down either.

### Back to the native install

```bash
cd ~/projects/<athena>            # the repo
docker compose -p odysseus-sound -f server/compose.yml down   # data stays in server/data
git checkout --detach d718bb6     # the last commit whose deploy.sh runs Athena natively
# in server/.env: remove ATHENA_RUNNER=docker, set BARVIS_SOUND_RUNNER=systemd
server/scripts/deploy.sh deploy d718bb6
```

The compose project is still called `odysseus-sound` (images `odysseus-sound*`).
`deploy.sh` already handles the rename to `athena`: it builds, then takes the other
name's containers down right before the new ones start (and the other way after a
rollback), never during a meeting. The rename itself comes once every server has deployed
with this `deploy.sh`, because the first deploy after a change runs the old one.

## Docker

Athena's services as containers, from `server/compose.yml`, configured by
`server/.env`:

| container | runs | notes |
|---|---|---|
| `sound-api` | `convo-server serve` | published on `CONVO_HOST:CONVO_PORT` (where devices reach it) |
| `sound-mcp` | `convo-server mcp --http` | published on `CONVO_MCP_HOST:CONVO_MCP_PORT`; needs `CONVO_MCP_TOKEN` |
| `whisper-gpu` | whisper-server, CUDA | profile `whisper-gpu`; needs the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) |
| `whisper-cpu` | whisper-server, CPU | profile `whisper-cpu` |

llama-server stays outside Docker. Loopback URLs in `.env`
(`CONVO_LLM_BASE_URL=http://127.0.0.1:8082/v1`, `CONVO_EMBED_BASE_URL`) are pointed at
the Docker host by the image's entrypoint, so a native install's `.env` works as is. The
server must listen on an address containers reach: `0.0.0.0`, or the Docker bridge
(`172.17.0.1`) through a forwarder. Data stays in `CONVO_DATA_DIR` on the host, mounted
at `/data`; the containers run as `PUID:PGID` (default `1000:1000`, see `id -u`), so the
files stay yours.

Add to `server/.env` (all keys: the Docker block at the bottom of `.env.example`):

```bash
COMPOSE_PROFILES=whisper-gpu           # or whisper-cpu; empty = whisper outside Docker
CONVO_DOCKER_CUDA_ARCH=86              # GPU compute capability without the dot (RTX 3060)
CONVO_DOCKER_WHISPER_MODELS=./data/whisper-models   # folder with the ggml model
CONVO_DOCKER_WHISPER_MODEL=ggml-large-v3-turbo-q5_0.bin
# CONVO_DOCKER_WHISPER_URL=http://127.0.0.1:8178     # only for whisper outside Docker
```

Get the model once, if it isn't on the machine already (point
`CONVO_DOCKER_WHISPER_MODELS` at the folder it is in otherwise):

```bash
mkdir -p data/whisper-models
curl -L -o data/whisper-models/ggml-large-v3-turbo-q5_0.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin
```

Then let `deploy.sh` run it (above), or run Compose yourself:

```bash
server/scripts/deploy.sh deploy
server/scripts/deploy.sh status        # docker compose ps
server/scripts/deploy.sh logs whisper  # serve | mcp | whisper

cd server && docker compose up -d --build --wait   # by hand
```

The first build takes a while (the CUDA whisper build ~10–20 min); later deploys reuse
the cache, and whisper is only restarted when its image or settings change.

One-off commands run in the API container, through the entrypoint so the model URLs
are rewritten: `docker compose exec sound-api convo-entrypoint process` (or `digest`,
`summarize-pending`).

Not in the containers yet: `whisper-cli` mode (`CONVO_WHISPER_CLI`; use whisper-server),
and paths outside the data dir (put a Parakeet model under `data/` and set
`CONVO_PARAKEET_MODEL_DIR=/data/...`). pyannote needs `CONVO_DOCKER_EXTRAS=vad,diarization,pyannote`
(`deploy.sh` refuses to build without it when `CONVO_DIARIZER=pyannote`) and runs on CPU
unless the API has the GPU, below. A stdio MCP client (Hermes) still uses the native `.venv`; it can
read the same data dir next to the containers. `deploy.sh` no longer installs into `.venv`,
so update it by hand when you use it (Setup above; the editable install follows the
checkout, only new dependencies need the install).

### pyannote on the GPU

The server image has CPU PyTorch by default. With pyannote (`CONVO_DIARIZER=pyannote`),
give the API container the GPU too, in `server/.env`:

```bash
CONVO_DOCKER_EXTRAS=vad,diarization,pyannote
CONVO_DOCKER_API_GPU=true        # deploy.sh adds compose.gpu.yml
# CONVO_DOCKER_TORCH_CUDA=cu126  # PyTorch CUDA wheels; cu126 runs on any CUDA 12+ driver
```

`compose.gpu.yml` builds the image with CUDA PyTorch (`odysseus-sound:current-cuda`,
about 3 GB more) and gives `sound-api` the NVIDIA GPU (the NVIDIA Container Toolkit,
as for `whisper-gpu`); `CONVO_PYANNOTE_DEVICE=auto` then picks it. It shares the GPU's memory with whisper
and llama-server.
By hand: `docker compose -f compose.yml -f compose.gpu.yml up -d --build --wait`.
Check: `docker compose exec sound-api python -c "import torch; print(torch.cuda.is_available())"`
prints `True`.

### Meeting bot in Docker

The Zoom meeting bot ([Meeting bot](../docs/api/meetings.md#meeting-bot), source in `meeting-bot/`) is built
only when `deploy.sh` finds the unpacked Zoom Meeting SDK for Linux (the folder with
`h/`, `libmeetingsdk.so` and `qt_libs/`; proprietary, so it stays outside the repo).
Unpack it to `~/zoom-sdk` and that's all: with `ATHENA_ZOOM_SDK_DIR` (old name
`BARVIS_SOUND_ZOOM_SDK_DIR`) unset, neither in the environment nor in `server/.env`,
`deploy.sh` uses `$HOME/zoom-sdk` when
`$HOME/zoom-sdk/h/zoom_sdk.h` exists and says so in one line (`$HOME` of the user running
`deploy.sh`; with `sudo docker` the folder is passed to compose on sudo's command line).
Elsewhere, or to turn the bot off:

```bash
ATHENA_ZOOM_SDK_DIR=/home/you/zoom-sdk   # another folder; ~ and $HOME work too
ATHENA_ZOOM_SDK_DIR=off                  # no meeting bot even with ~/zoom-sdk (empty works too)
CONVO_ZOOM_SDK_KEY=...                         # the Meeting SDK app's credentials
CONVO_ZOOM_SDK_SECRET=...
```

Without `CONVO_ZOOM_SDK_KEY` / `CONVO_ZOOM_SDK_SECRET` the deploy warns and goes on: the
bot is built, but joins answer 501 until they are set.

`deploy.sh` then adds `compose.meeting.yml` (after `compose.gpu.yml` if that is on). It
builds the Dockerfile's `meeting` target for `sound-api` only: `meeting-bot/` compiled
against the SDK, the SDK's system libraries, and `CONVO_MEETING_BOT_PATH` set to
`/opt/athena-meeting-bot/athena-meeting-bot-run` in the container (it wins over a path in
`.env`). The SDK and `meeting-bot/` come in as BuildKit build contexts (`zoomsdk`,
`meetingbot`), never through git or `server/`. The image is
`odysseus-sound-meeting:current` and **holds the SDK: keep it on this machine, never push
it to a registry.** `sound-mcp` keeps the plain image. The build fails if the bot misses a
library, checked with only the environment the launcher gives the bot.

There is no new service or container: the launcher starts the bot as a child process of
`convo-server serve` in `sound-api` when a meeting is requested. `systemd-run` isn't
there, so the `auto` runner picks `plain` (`nice 10`, `RLIMIT_AS` =
`CONVO_MEETING_BOT_MEMORY`). The launcher builds the bot's environment from scratch (no
`LD_LIBRARY_PATH`), so `CONVO_MEETING_BOT_PATH` points at a start script,
`docker/athena-meeting-bot-run`, next to the binary and the SDK (`zoomsdk/`): it puts the
SDK and its bundled Qt on the library path, keeps glibc's malloc arenas few (two, so
`RLIMIT_AS` measures what the bot uses) and `exec`s the bot, so the launcher's pid,
signals and limits are the bot's own. `--ldd` lists the bot's libraries as a run sees
them. Without an SDK (none found, or `off`) the image is exactly as before (no SDK, no extra
libraries) and meetings answer 501.

```bash
server/scripts/deploy.sh deploy
# by hand, from server/ (compose.meeting.yml reads the folder as BARVIS_SOUND_ZOOM_SDK_DIR):
BARVIS_SOUND_ZOOM_SDK_DIR=$HOME/zoom-sdk docker compose -f compose.yml \
  -f compose.gpu.yml -f compose.meeting.yml up -d --build --wait   # leave out gpu if off
docker compose exec sound-api /opt/athena-meeting-bot/athena-meeting-bot-run --ldd
```

### Meeting bot cost check

`scripts/meeting_bot_cost.py` (standard library only) measures what one bot costs and
checks that nothing of it is left afterwards: CPU (average, and peak over 1 s windows;
100 % = one core), peak RSS (`VmHWM`), `VmPeak` (peak address space: what the plain
runner's `RLIMIT_AS` caps), threads, sockets and the work dir's peak size. It fails (exit
1) when, after the bot exited, a process of its session or a descendant is still alive, a
socket it had is still open, a file is left in its work dir (the bot empties it after a
successful finish: chunks, `queue/`, `chat.jsonl` and `participants.json` are removed),
or `VmPeak` is over `CONVO_MEETING_BOT_MEMORY` (from the environment or `server/.env`,
else 2G); it warns above 75 % and names the value to set, with headroom.

- **Fixture run** (CI, sandbox): `python3 server/scripts/meeting_bot_cost.py
  build-bot-stub/athena-meeting-bot [--seconds 20] [--json]` runs the bot built against
  the stub SDK (scenario `fixture`) with a fictional PCM fixture it generates at run time,
  a fake upload API (checks Dana's chunks, one chat call, one `/end`), a private `TMPDIR`
  and `HOME` (any file left there fails too) and the launcher's `nice 10` + `RLIMIT_AS`.
  The stub's numbers say nothing about the real SDK and its Qt, which are much bigger.
- **Real meeting** (the owner, once per release): start `--watch` before or during a test
  meeting; it finds the running `athena-meeting-bot` (or `--pid`), samples it until it
  leaves, then checks for leftovers: processes, sockets, the meeting's dir under
  `<CONVO_DATA_DIR>/meetings/` (the launcher removes it; another dir there is a warning),
  files the bot had open under its `TMPDIR` or `HOME`. In Docker:
  `docker compose exec sound-api python3 /app/scripts/meeting_bot_cost.py --watch`
  (from `server/`). With the systemd runner the scope's `memory.peak` (`MemoryMax`) is
  judged instead of `VmPeak`.

If it says to raise the cap, set `CONVO_MEETING_BOT_MEMORY=<value>` in `server/.env` and
deploy (`server/scripts/deploy.sh deploy`). The script never prints a token or the
bot's environment (it reads only `TMPDIR` and `HOME` from it).

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

Or from the next recording, best on the watch you wear every day (its microphone is the
one your voice is recognized through): Odysseus' **Enroll my voice** window does this for
you and shows statements to read. By hand: `POST /v1/speakers/enroll/next` with JSON
`{"name": "<your name>", "is_owner": true}` (both optional: the name defaults to the
enrolled owner voice, else the owner's person, else `CONVO_OWNER_NAME`), then record 60–90 s
of yourself talking. The first recording whose first chunk reaches the server after that
(and that started at most 2 minutes before it, on any device but the Zoom bot, not a
meeting) is enrolled when it is processed, with at least 30 s of speech: it replaces
earlier enrollment samples (named and learned samples stay), renames matching unnamed
speakers in earlier recordings, and makes **no conversation** (read-aloud statements would
give the summarizer made-up facts); its audio is deleted unless `CONVO_KEEP_AUDIO`.
`GET /v1/speakers/enroll/next` shows the state (`none`, `waiting` with the `recording`
coming in, `processing`, `done` with `samples`, `speech_seconds`, `relabeled_lines`,
`failed` with `error`, `cancelled`, `expired` after 30 minutes without a recording),
`DELETE` cancels while waiting (409 while processing). `athena/voice_enroll.py`.

## API reference

The API is described in `docs/api/`:

- [docs/api/ingest.md](../docs/api/ingest.md): uploads (`POST /v1/ingest/chunk`), tracks, written notes, the read API, sharing a conversation.
- [docs/api/meetings.md](../docs/api/meetings.md): the meeting bot launcher, its bot contract, Zoom sign-in, meeting chat, topic and participants.
- [docs/api/mcp.md](../docs/api/mcp.md): the MCP tools, their JSON fields, facts, people, forget and retention, and the relay routes on the MCP HTTP app.

### Ingestion API

Moved to [docs/api/ingest.md](../docs/api/ingest.md).

### Meeting bot

Moved to [docs/api/meetings.md](../docs/api/meetings.md) (with the bot contract).

### Meeting chat, topic and participants

Moved to [docs/api/meetings.md](../docs/api/meetings.md#meeting-chat-topic-and-participants).

### Written notes

Moved to [docs/api/ingest.md](../docs/api/ingest.md#written-notes).

### Sharing a conversation

Moved to [docs/api/ingest.md](../docs/api/ingest.md#sharing-a-conversation).

## VAD and transcriber

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

## Knowledge layer (MCP)

`convo-server mcp` exposes the store to any MCP-capable frontend. The tools, their JSON fields and the relay routes are in [docs/api/mcp.md](../docs/api/mcp.md).

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
   (`athena/relevance_rules.py`): under 3 words (`too_short`), only filler words
   such as "okay", "mm", "yeah" (`filler`), or a microphone check such as "testing, one,
   two" (`mic_check`). These rules only discard: anything with one content word, a name,
   a number, "yes"/"no", or words in another language goes to the model. No rule keeps a
   conversation without the model, because the model is what spots a TV playing.
2. **Notes** (title, summary, key points, people, topics) with a template per kind, e.g.
   decisions and owners for meetings, instructions and the next date for appointments.
3. **Action items** (promises, requests, appointments), each written as a task, with
   `due_date` (YYYY-MM-DD). When the deadline names a day ("by Friday", "October
   thirty-first", "tomorrow") the date is worked out in code (`athena/dates.py`)
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
run without models. They are in `tests/unit/{summarize,store,mcp,api,transcribe}/`,
`tests/integration/` (the pipeline end to end), `tests/meetings/` (the meeting bot and
meeting processing) and `tests/contract/` (snapshots of what the hub reads); each folder
is a package, so a test imports shared helpers as `tests.conftest` or
`tests.unit.mcp.test_mcp`. `scripts/smoke_upload.py` uploads real audio files as 30 s Opus
chunks to a running server for an end-to-end check.
