# Ingestion API

Moved here from [server/README.md](../../server/README.md) on 2026-10-29. Setup, configuration and running are there; the meeting bot is in [meetings.md](meetings.md), the MCP tools in [mcp.md](mcp.md).

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
| `track`       | optional: one participant's audio in a session with several (e.g. a Zoom user id), `[A-Za-z0-9._:-]{1,64}`; omitted or empty = no track |
| `track_name`  | optional: that participant's display name, at most 200 characters; needs `track` |

A **voice note** (`kind=note`) is the owner talking to themselves: no speaker matching
(every line is the owner), never split on gaps or joined with nearby conversations, never
discarded as too short, and summarized with a note prompt (the idea, plus the to-dos it
implies). It shows up as a conversation with `kind` `note`. A chunk whose `kind` differs
from its session's is rejected with 400. `POST /v1/ingest/chunk.json` takes the same
`kind` field.

**Tracks** (a meeting: one track per participant). Without `track` a session works as
before tracks. With it, sequences count per track, and a retry of the same
(`session_id`, `track`, `sequence`) is a no-op (`"duplicate": true`; its `track_name` is
ignored too). Each track is reassembled by its own sequence and transcribed on its own, so
two people talking at once stay apart. `track_name` is stored per chunk: a participant who
renames mid-meeting stays one track, and `GET /v1/sessions/{id}` lists every name seen
(`tracks`: `track`, `name` (of the latest chunk in time, as sent), `names` (every name,
in order of first use), `chunks`, `started_at`, `ended_at`; each
chunk also has `track` and `track_name`, null without one). A session with any tracked
chunk (a meeting) never stops on trailing silence, so a quiet break doesn't end it: it
stops at `CONVO_MAX_SESSION_SECONDS`, on `POST /v1/sessions/{id}/end`, or after
`CONVO_SESSION_IDLE_SECONDS` without uploads (the uploader died; not while the meeting
bot that records it still runs, since Zoom sends no audio while everyone is muted: its exit
ends the session); once ended, every track
is told `stop: true`. If an untracked chunk stopped the session on silence first, the
first tracked chunk reopens it (`recording` again, no stop reason). Errors (400): `track must match
^[A-Za-z0-9._:-]{1,64}$`, `track_name is longer than 200 characters`, `track_name needs a
track`. `POST /v1/ingest/chunk.json` takes the same two fields.

**Meeting lines** (`athena/meetings/transcribe/tracks.py`, imported only when a session with
tracks is processed). Each track's VAD speech goes to whisper on its own (whisper's
context never crosses tracks; the participants' names are added to its prompt for
spelling), and every line from a track is that participant's:

- A line is named by the track's latest `track_name`. A track named `CONVO_OWNER_NAME`
  (unless it is the default "Me") or one of `CONVO_ZOOM_OWNER_NAMES` (extra names in
  addition to `CONVO_OWNER_NAME`, comma-separated, any case, e.g. "Shahyah (phone)") is
  the owner's: `is_owner=true`, named like the owner everywhere else (the enrolled owner
  voice's name, else `CONVO_OWNER_NAME`, never the Zoom name; the Zoom names become
  aliases of the owner). A track without a name is an unnamed "Speaker N".
- No diarizer runs for these tracks (with `CONVO_LEARN_MEETING_VOICES` on, the speaker
  embedder alone runs on at most 3 windows per person afterwards, see "Learning voices from
  meetings" under Speakers). A track whose name is a room or
  a device ("Conference Room", "Meeting Room 2", "Zoom Rooms", "Anna's iPhone", "Galaxy
  S23", "Zoom user", "Polycom", a dial-in number) can hold several people: the configured
  diarizer runs on that track only, and its voices are named from enrolled voices or
  become "Speaker N" (numbered across the meeting) with a stored voice, so
  `name_speaker` and `list_unnamed_speakers` work on them. With diarization off the
  room's lines carry the room's name.
- Lines of different tracks may overlap (crosstalk); all are kept, in start-time order.
  `CONVO_JOIN_LINES` joins fragments within one track only.
- Bleed (two participants in one room, one voice on both mics): a line on another track
  that overlaps by at least half the shorter line and says nearly the same words is
  dropped, keeping the louder line (RMS dBFS of each line on its own track). Nearly the
  same: word similarity >= 0.9, or >= 0.7 when one line is at least 6 dB quieter
  (whisper slips words on the far mic; two people saying the same sentence at once are
  about as loud and both stay).
- One meeting is one conversation: its lines are never split at a speech gap
  (`CONVO_CONVERSATION_GAP_SECONDS` doesn't apply to a break inside a meeting), it never
  extends the conversation before it on the same device, and a session without tracks
  never joins a meeting's conversation. Two back-to-back meetings are two conversations.
- Late chunks: once a meeting is being or has been processed, a new chunk (a new
  (`track`, `sequence`)) is answered `stop: true` with the session's stop reason and is
  not stored, so it can't replace the other tracks' lines, whose audio is gone. Until
  processing starts (status `ended`), chunks are still stored: the other tracks' chunks
  of the round that reached `CONVO_MAX_SESSION_SECONDS` are kept. A retry of a stored
  chunk is `duplicate: true` as before. Sessions without tracks still take late chunks
  and transcribe them on the next pass.
- Live transcription (`athena/meetings/transcribe/live.py`, `CONVO_MEETING_LIVE_TRANSCRIBE`,
  default true): while a meeting records, the worker's existing 15 s tick transcribes each
  person's track window by window, so when it ends only the last window of each track (and
  any shared-room track) is left before the summary, which starts in the same tick. No
  process, thread or timer is added: with no meeting the tick asks one indexed query
  (`sessions_status`) and imports nothing; `false` skips even that and transcribes the
  meeting at its end. Each tick spends at most about `CONVO_MEETING_LIVE_BUDGET_SECONDS`
  (20) on it, one whisper request at a time, round robin over tracks, continuing next tick.
  The result is the end-of-meeting result: windows are planned from the stored VAD spans
  exactly as at the end, and only windows no later audio can change are sent (a window
  is closed once the next speech region didn't fit in it, or its run ended), so a word
  across a chunk boundary is never cut and whisper hears the same clips with the same
  per-track context. Line joining continues from the track's last line, and each line's
  loudness is stored for the bleed check, which runs at the end over all tracks. The
  participants' names in whisper's prompt are those known at the time, so a late
  joiner's name only reaches later windows (the one possible difference, spelling only).
  Shared-room tracks wait for the end (diarized over the whole track). Progress is saved
  per window (`meeting_live` table) and checked against the chunks before use: a restart
  redoes at most one window, and a late chunk that changes a run restarts that track.
  Audio is kept until the end pass succeeds (`CONVO_KEEP_AUDIO` rules unchanged), and
  the live transcript is deleted once it is in the meeting's lines.

Response: `{"ok": true, "stop": bool, "reason": "silence"|"max_duration"|"client"|"idle"|null,
"duplicate": bool, "speech_seconds", "trailing_silence_seconds", "session_seconds"}`, plus
`"track"` when the upload had one.

`POST /v1/sessions/{id}/end` marks a session finished (device Stop button).

## Written notes

`POST /v1/ingest/text` (JSON, same bearer token) takes a note the owner wrote by hand in
the hub (Odysseus) and reviewed there:

| field        | notes                                                                  |
|--------------|------------------------------------------------------------------------|
| `source_id`  | the hub's stable id: `hub-note:<id>` (id: `[A-Za-z0-9._-]{1,100}`)     |
| `text`       | the reviewed text, at most 200,000 characters and 5,000 lines; a line over 4,000 characters is wrapped at a space |
| `written_at` | when the page was written: ISO-8601 (naive = UTC) or epoch s/ms        |
| `link`       | the note in the hub, `<hub>/#open=notes&note=<id>` (http/https, ≤ 2,000; no spaces, quotes, `( ) < > { } \| \ ^` or user info, `[ ]` only around an IPv6 host) |
| `title`      | optional, ≤ 200 characters; shown with the link, not used as the summary title |

It becomes one conversation of kind `note` on device `handwriting`: each non-empty line
(spaces trimmed, control characters dropped) is a line by the owner at `written_at` plus
one second per line, so the usual `/view/<id>?sig=…#t=<seconds>` link points at a line.
No audio, VAD, transcription or diarization. The worker summarizes it with the
voice-note prompts (summary, to-dos, facts; never discarded). The text is the owner's
words, never instructions: the model gets it as the note's text, like a transcript.

Answer: `{"ok": true, "source_id", "result": "created"|"replaced"|"unchanged",
"conversation_id", "lines", "erased_lines", "transcript_link"}`; 400 with a `detail` for a
bad `source_id`, `link`, `written_at` or text with no words, 422 for a field over its
size.

- **Resend** (same `source_id`): the lines are replaced, the digests that covered the note
  are taken down and written again, and the worker summarizes it again. Facts and to-dos
  said again keep their ids (and a to-do its status); the others are dropped, so the
  hub's syncs retire them. A retired fact stays retired. The same lines and `written_at`
  is a no-op (`"unchanged"`; a new `link` or `title` is stored). A resend that lands while
  the model is summarizing the old text is summarized again on the next pass.
- **Erased facts stay erased.** The hub still has the words of a fact the owner erased
  (`erase_fact`), so a resend brings them back: a line with the same words as a line
  `erase_fact` redacted in this note, or one that says an erased fact of any note or
  recording (every content word of a fact of up to three, three quarters of a longer
  one), is stored as `[forgotten]` (`erased_lines` counts them). The same holds for a new
  note.
- Until the re-summary, a resend keeps the note's summary, facts and to-dos, and moves
  each one's line offset (`#t=`) to the same line (or the one with most words in
  common); a line crossed out leaves the fact without an offset.
- `DELETE /v1/ingest/text/{source_id}` removes the note: its lines, conversation, to-dos and
  facts (a fact another conversation said too stays, owned by that one), and its digests
  are written again. An erased fact's sentence stays (never served) so a later note that
  writes it again keeps it `[forgotten]`, and what a forgotten fact of the note had
  replaced stays replaced. `{"ok": true, "result": "deleted", "conversation_id"}`, or
  `"already"` when there is no such note (idempotent).
- The `/view` page says "Written by hand" and links to the note in the hub. Conversations
  in the API carry `written: {"source_id", "link", "title"}` (null for recordings).

## Read API

Read API: `/v1/conversations`, `/v1/conversations/{id}`, `/v1/conversations/{id}/transcript`
(`?start=&end=` offsets), `/v1/search?q=`, `/v1/action-items` (`?include_notes=true`
adds note items), `/v1/digests/{date}`,
`/v1/sessions`, `/v1/speakers`; `PATCH /v1/action-items/{id}` takes any of `status`,
`kind`, `due_date`, `due_time`, `list_name`, `title` (`null` clears a date, time or list,
and gives the title back from the text). Conversations carry a
signed `link`, and action items and facts a `source_url`, of the form `<CONVO_PUBLIC_URL>/view/<id>?sig=…#t=<seconds>`: an HTML
transcript page that opens in a phone browser without the bearer token and jumps to that
moment. Rotating `CONVO_INGEST_TOKEN` revokes all links.

## Sharing a conversation

`GET /v1/conversations/{id}/export?format=md|html&include=...` answers with one file to send
to other people (`Content-Disposition: attachment`, a name like
`2025-10-09 Planning the trip.md`, `Cache-Control: no-store`). The hub's Share button gets
it through the MCP server's relay route `GET /conversations/{id}/export` (MCP 0.19.0).

| parameter | values |
|---|---|
| `format` | `md` (Markdown, the default) or `html` (one self-contained page, the `/view` look, prints to PDF from a browser) |
| `include` | comma list of `summary` (with key points), `action_items`, `facts`, `transcript`; default `summary,action_items,transcript` |

What a shared copy holds: the title, date and time in `CONVO_TIMEZONE`, people, the
meeting topic and participants, "Written by hand" for a written note, and the sections
asked for; transcript lines as `HH:MM:SS Speaker: text` (`Name (chat)` for meeting chat).
What it leaves out: signed transcript links, the link to the hub note, ids, the jump
script, dismissed action items, and facts unless asked for. Lines erased with
`erase_fact` read `[forgotten]`; a summary waiting for its rewrite after a forget stays
hidden; a transcript removed by retention says so. Errors: 400 for an unknown `format` or
`include` name, 404 `conversation not found`.

