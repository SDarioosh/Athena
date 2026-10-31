# MCP tools

Moved here from [server/README.md](../../server/README.md) on 2026-10-29. How to connect Hermes or Odysseus is under "Knowledge layer (MCP)" there; the module contract is in [AGENTS.md](../../AGENTS.md#mcp-tool-contract). Settings are written `CONVO_<NAME>` here; each also works as `ATHENA_<NAME>`, which wins when both are set.

## Version notes

Each change is marked inline with the MCP version that brought it (for example "MCP 0.15.0, additive").

- MCP 0.20.1 (patch): the server reports its own name as `athena` (was `convo-ai`). The hub registers the server as `convo` and doesn't read this name; nothing else changed.

## Tools

`convo-server mcp` exposes the store to any MCP-capable frontend. The store stays the
source of truth, so the frontend can be swapped. Tools:

| tool | answers |
|---|---|
| `search_transcripts(speaker, query, since, until)` | "what did X say (about Y) on/since DATE": actual lines, with links |
| `search_conversations(query, person, since, until, kind)` | conversations by topic / person / date; `kind`: `conversation`, `note` (voice and written notes), `voice` or `written` (MCP 0.14.0), `meeting` (online meetings only) or `in_person` (conversations that are no meeting; MCP 0.18.0) |
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
  "with": [{"id": 4, "name": "Greta"}, {"id": null, "name": "Tom"}], "key_points": [],
  "source": null, "written_by_hand": false, "note_link": null}]
```

`key_points` (MCP 0.15.0, additive) is set on a note that is a whole voice note ("Make a
note, AMOLED T4-S3 dimensions. The screen is 49 mm by 60 mm. ..."): its `text` is then
the voice note's summary, and `key_points` are the voice note's key points ("Screen: 49mm
x 60mm"), so the hub's note holds what was said. `[]` for every other item (to-dos,
notes added to named notes, a voice note with several notes or with to-dos besides).

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
            "fact": "Alice is vegetarian", "kind": "preference",
            "conversation_id": "c-…", "quote": "Alice doesn't eat meat, remember?",
            "times_said": 3, "days": 2,
            "first_said": "2025-10-09", "last_said": "2025-10-10",
            "confirmed": false, "valid_until": null, "source": null,
            "link": "<CONVO_PUBLIC_URL>/view/<id>?sig=…#t=2"}]}
```

`about` is `owner` when the subject (trimmed, any case) is me, I, myself, owner, the
owner, user, the user, `CONVO_OWNER_NAME` or the name of the enrolled owner voice;
`person` when it is an enrolled speaker, a person named in a conversation or one of their
former names (aliases, below); else `other`. `person_id` is the subject's person id (below) when `about` is `person`, and
the owner's person id when it is `owner` (the owner person, below); null for `other`
or when no such person exists.
`kind` (MCP 0.11.0) is what sort of fact it is: `preference`, `plan`, `decision`,
`relationship`, `personal` or `other` (null for facts stored before it). `conversation_id`
is the conversation the fact was learned in, and `quote` the transcript line it was said
on (at most 300 characters; null when there is no line, the transcript was pruned or the
line erased), so a review page can show the words and group facts by conversation.
`times_said` counts distinct conversations, `days` distinct local dates, and
`first_said`/`last_said` are the first and last of those dates. `confirmed` is true when
the fact meets the thresholds above, so a program can take every current fact with its
counts without knowing them (`confirmed=true` as an argument still filters).
`valid_until` (MCP 0.13.0, additive) is the last day a fact about one occasion is worth
keeping, `YYYY-MM-DD`, else null. It is worked out when the facts are read
(`fact_valid_until` in `dates.py`), from the fact's words and the local date
(`CONVO_TIMEZONE`) of the conversation that first said it: a day or date it names
("has a meeting with the Meshtastic team on Tuesday at 11 a.m." → that Tuesday; "is in
Berlin until December 3" → December 3), "this/next week" or "the weekend" (that
Sunday), "this/next month" or "in May" / "by the end of March" (the month's last day).
None for a fact about something that repeats (birthday, anniversary, "every", "on
Mondays"), that already happened ("was", "moved", "since", "last week"), or for a day
more than a year ahead. The text format adds "until <date>". The hub ends the belief
after that day. `link` is
the same transcript link as in the text. `limit` goes up to 1000 in JSON mode (200 in text), so a
program can hold every fact and reconcile by id: the hub asks for 1000 and retires any
fact id that stops being returned (replaced, retired, or its conversation deleted).

`source` (MCP 0.16.0, additive) is on every JSON fact row and every JSON
`list_action_items` row: `"meeting"` when the fact or item comes from a Zoom meeting's
conversation, else null (recordings, voice notes and written notes are told apart by
`conversation_kind` and `written_by_hand`; other values may be added later, so treat an
unknown one as null). In the text results a meeting's conversation is marked `(Online
meeting: <topic>)` (`(Online meeting)` without a topic; MCP 0.17.0, before that `(Zoom
meeting…)`: the mark names no platform) and lists its participants
(`search_conversations`, `get_conversation` with join and leave times), and chat lines
read `<name> (chat): …` (`get_transcript`, `search_transcripts`, the `/view` page).

`written_by_hand` and `note_link` (MCP 0.12.0, additive) are on every JSON fact row and
every JSON `list_action_items` row: true and the note's URL in the hub when the fact or
item comes from a written note (`POST /v1/ingest/text`), else false and null. The text
listings mark such facts, items and conversations `(written by hand: <link>)` instead of
`(voice note)`.

**Fact quality.** Small models list too many facts. The facts prompt leaves out
thinking aloud, requests to someone else (action items), which tool was used and passing
details of what was being worked on, never guesses anyone's gender, and asks for at most
twice `CONVO_MAX_FACTS_PER_CONVERSATION` candidates (at least 5). Code drops what the model
still returns of the first two: thinking aloud in the fact's main clause ("is
considering …", "might …", "hasn't decided …"; not "an allergy that might cause …"), and
requests: asking or telling someone to do something ("asked the landlord to fix …"), or
"wants X to <errand>" when X is a person of the conversation ("wants Bo to send …", not
"needs German to work in Zurich" or "wants Leo to attend the bilingual school"). Then one
more model call scores each fact 1–5 (worth recalling in three months?), shown the
line(s) it was said on; facts under `CONVO_FACT_MIN_SCORE` (default 4; 1 = no call) are
left out (a fact it gives no score counts as that minimum), and at most
`CONVO_MAX_FACTS_PER_CONVERSATION` (default 3; 0 = no cap) are kept, best first. A fact
that shares its subject and a word with a stored fact (likely said again, or an update) is
never cut by the cap, so an update can't be lost and a re-summary doesn't lose a fact that
had replaced another. If the call fails, the facts are kept. In a voice note, facts that
only repeat a dated event go first, and the owner's decisions are neither reviewed nor
capped. Facts already stored are unchanged; a fact said again with a kind gives it to an
older fact that had none. A fact's `quote` is the line `erase_fact` would redact: the line
that starts in the cited second (the one sharing most words with the fact), else the
nearest, else a nearby line that says the fact's words; for a pruned transcript, the
newest conversation that said it again.

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

## Relay routes

**Uploads through the MCP server.** `convo-server mcp --http` also serves plain HTTP
routes next to `/mcp`, behind the same token (`Authorization: Bearer` or
`?access_token=`). They are not MCP tools, so no model ever sees them:

| route | same as |
|---|---|
| `POST /ingest/chunk` | `POST /v1/ingest/chunk` (same multipart fields, same JSON answer) |
| `POST /sessions/{session_id}/end` | `POST /v1/sessions/{id}/end` |
| `POST /ingest/text` | `POST /v1/ingest/text` (same JSON fields and answer; written notes) |
| `DELETE /ingest/text/{source_id}` | `DELETE /v1/ingest/text/{source_id}` |
| `GET /health` | `GET /health` (here behind the token) |
| `GET /conversations`, `GET /conversations/{id}`, `GET /conversations/{id}/transcript` | the `/v1` routes, same query |
| `GET /action-items`, `PATCH /action-items/{id}` | the `/v1` routes |
| `GET /digests/{date}`, `POST /digests/{date}` | the `/v1` routes |
| `GET /settings/llm`, `PUT /settings/llm` | the `/v1` routes (the hub's settings page: the summary model) |
| `GET /settings/vocabulary`, `PUT /settings/vocabulary` | the `/v1` routes (the hub's settings page: words whisper is nudged toward) |
| `GET`, `POST`, `DELETE /speakers/enroll/next` | the `/v1` routes (the hub's "Enroll my voice": enroll the next recording; MCP 0.20.0) |
| `POST /meetings` | `POST /v1/meetings` (same JSON `{url}` or `{meeting_id, passcode}`, `title?`; same answer: 202, 400, 409 busy with `message` and `active`, 501 meetings off, 500) |
| `GET /meetings` | `GET /v1/meetings` (only `limit`, bounded to 1–100) |
| `GET /meetings/{id}`, `DELETE /meetings/{id}` | the `/v1` routes (state; leave, answering once the bot exited) |
| `GET /conversations/{id}/export` | `GET /v1/conversations/{id}/export` (same `format`, `include`; the file comes back as it is, not JSON; errors are JSON `detail`; the hub's Share button; MCP 0.19.0) |

The meeting routes are for the hub's "Send Athena to a meeting" box (lane 4B of
`docs/zoom-meeting-bot-plan.md`). An `{id}` that isn't a meeting id (32 lowercase hex
characters) gets the API's own 404 `{"detail": "meeting not found"}` without being
forwarded; `POST /meetings` takes `application/json` only (415 otherwise) and caps its
body at 16 KB (413). `GET` and `POST` wait up to 15 s for the API, `DELETE` up to 30 s
(the bot gets SIGTERM, SIGKILL after 10 s, and the answer waits for it to exit); a timeout
or an API that isn't running is a 502 with a readable `detail`. The relay only forwards
HTTP: it never imports the meeting code, so `mcp --http` loads and starts nothing new.

The read routes are for the phone relay's T5 sync when it is paired through Odysseus
(Odysseus forwards them at `/api/codex/athena/*` for a token with `athena:read` /
`athena:write`; the old `/api/codex/sound/*` and `sound:*` names keep working, and a hub from
before the Athena names has only those); the MCP token can
read the same data through the tools anyway.

Each request is forwarded unchanged to this module's own ingest API
(`http://<CONVO_HOST>:<CONVO_PORT>`, `127.0.0.1` when `CONVO_HOST` is `0.0.0.0`) with
`CONVO_INGEST_TOKEN`, so there is one ingest code path and the ingest token, which also
signs transcript links, never leaves the module. The API's status and `detail` come back
as they are; the body is capped like the API's (25 MB of audio); if `convo-server serve`
isn't running the answer is 502 with a readable `detail`. `/ingest/text` takes
`application/json` only (415 otherwise) and caps its body at about 1.2 MB. Odysseus uses these routes to
add recordings (its call recorder, phones paired through it) with the URL and token of
the `convo` MCP server it already has. **The MCP token can therefore add recordings**:
treat it like the ingest token, and keep the MCP port on loopback, the Docker bridge or
Tailscale. uvicorn's access log shows `access_token=***` instead of the token.

