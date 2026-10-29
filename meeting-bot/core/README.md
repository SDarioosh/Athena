# athena_core: the meeting bot's recording core

Static library behind `athena-meeting-bot` (lane 1A and 3A of
`docs/zoom-meeting-bot-plan.md`). It knows nothing about Zoom: the bot calls
`athena::Core` (`include/athena/core.h`) from the SDK's callbacks, and the host tests call
it with fictional audio. It builds and tests without the Zoom SDK:

```bash
sudo apt install cmake g++ pkg-config libcurl4-openssl-dev libopus-dev libopusenc-dev libogg-dev
sudo apt install libopusfile-dev   # optional: the unit test then decodes the Opus output
cmake -S meeting-bot -B build-bot && cmake --build build-bot -j && ctest --test-dir build-bot --output-on-failure
```

What it does:

- **Tracks and chunks.** One track per Zoom user id (the chunk `track`; ids are cleaned to
  `[A-Za-z0-9._:-]{1,64}`), its latest display name as `track_name`, a sequence per track
  from 0. Someone who leaves and comes back keeps their track and its sequence.
- **Padding rule.** A chunk covers at most 30 s of wall-clock time from its first sample,
  `[start, start + 30 s)`, and holds the audio at its true offsets: a pause inside that
  window (Zoom delivered nothing, e.g. only while someone speaks) is filled with silence,
  so whisper gets whole 30 s stretches with context, not one clip per reply. A chunk ends
  at its last real sample, never with trailing silence, so the server's duration is
  honest; audio after the window starts a new chunk with its own timestamp, and a pause
  that crosses the window's end is not padded across chunks. Delivery jitter up to
  300 ms is appended as it comes (`kJitterToleranceMs` in `src/chunker.h`).
  A chunk closes when its window is full or over (checked on incoming audio and by the
  uploader's ~1 s tick while a chunk is open, using the latest block's time plus the time
  since; no extra thread), when its participant leaves, on `finish()`, on a sample-rate
  change, or when the clock steps back more than 5 s. Padding is bounded by one 30 s
  chunk per track.
- **Encoding.** Ogg/Opus at 32 kbps (`codec=opus`, libopusenc) or WAV (`codec=wav`), mono
  at the sample rate Zoom delivers (32 kHz in the spike). Chunks are encoded block by
  block as they grow, so closing one is cheap and no PCM piles up. Opus uses DTX, so the
  silence of a padded pause costs almost nothing (30 s of it is about 7 KB).
- **Queue and upload.** A finished chunk is written to `<work_dir>/queue/` first and
  deleted only after the server answers 200 with `"ok": true`. One uploader thread
  (libcurl, never through a proxy) sends `POST /v1/ingest/chunk` multipart with `audio`,
  `device=zoom`, `session_id`, `sequence`, `timestamp` (epoch seconds of the first
  sample, with milliseconds), `codec`, `track`, `track_name` and
  `Authorization: Bearer $ATHENA_UPLOAD_TOKEN`. Answers follow the ingest client contract
  ([docs/api/ingest-client.md](../../docs/api/ingest-client.md)): network errors, 408,
  429, 5xx, a 200 without `"ok": true` and redirects (never followed: "check the URL") are
  retried with backoff (1 s doubling to 30 s); 401/403 stop the uploads and set
  `upload_rejected()`; other 4xx drop that chunk (logged). An answer with `"stop": true`
  sets `stop_requested()`. Unsent audio is capped at `--max-seconds`; the oldest chunk
  goes first (logged).
- **Chat, topic, participants.** Each chat message is appended to `<work_dir>/chat.jsonl`
  as it arrives, already shaped as a `lines[]` entry of `POST /v1/sessions/{id}/chat`
  (`docs/api/meetings.md`, "Meeting chat, topic and participants"); at most 2,000 messages
  and 1 MB. A message sent to Athena alone is dropped and never written. Participants
  (join, leave, rename) and the topic are kept in memory and in
  `<work_dir>/participants.json`.
- **finish(deadline).** Closes every open chunk (partial last chunks too), lets the
  uploader drain the queue until about 1 s before the deadline, then sends the chat call
  and `POST /v1/sessions/{id}/end`, and returns by the deadline even if the server never
  answers. It returns true when every chunk, the chat and the end call went through, and
  then removes the queue folder, `chat.jsonl` and `participants.json`.

Logs go to stderr, prefixed `athena-core:`; nothing goes to stdout (the launcher reads the
bot's status lines there). The core starts one thread (the uploader) and no timer; the uploader wakes once a
second only while a chunk is open.
