# Text messages in Athena

The owner's text messages (iMessage first) become Athena conversations: searchable line by
line, summarized with the local model, their facts and promises in the hub's belief ledger,
their people the same people Athena hears in recordings. Status: **built, not yet on the
owner's Mac** (see [PROGRESS.md](../PROGRESS.md)). Hub side of the plan:
[context-growth.md](https://github.com/SDarioosh/odysseus-expanded/blob/barvis/docs/fork/context-growth.md).

Two parts:

| Part | Where | What it does |
|---|---|---|
| **Messages relay** | `messages-relay/`, on the owner's always-on Mac | Reads the Messages app's database (read-only), the Mac's Contacts for names, filters, and uploads new and changed messages |
| **Text threads** | `server/athena/texts/`, in the Athena server | Stores messages, groups each chat into threads (one conversation each), indexes them, schedules summaries |

Nothing in this path leaves the owner's network: the relay talks only to the owner's server
over Tailscale, with the ingest token (or the MCP token through the relay routes).

## Why it's built this way

- **A message is a transcript line with a receipt.** Each message is stored once as a line
  (`segments`, kind `text`) and once as its receipt (`text_messages`: its id, chat, sender
  handle, edit time, reply, attachments). The line is what every existing feature already
  reads: search, transcripts, the `/view` page, facts' source links, `erase_fact`, retention,
  speaker renames, the hub's facts job. The receipt is what makes uploads idempotent and
  edits, unsends and contact renames exact.
- **A thread is a conversation.** A chat is split into threads at quiet gaps; each thread is
  one conversation, summarized like a recording. So facts are confirmed across threads and
  recordings alike ("said in 3 conversations on 2 days"), and nothing in the hub changes.
- **Threads stay right when messages arrive out of order.** Messages in iCloud downloads old
  history late, with new row ids. Threading is a pure function of a chat's messages; after
  every upload only the affected stretch of the chat is threaded again, and the new threads
  keep the conversation ids (and so the fact and to-do ids) of the threads they overlap most.
- **The model's time goes where it matters.** Years of history are tens of thousands of
  threads. Recordings are always summarized first; text threads wait until they have been
  quiet for a while (a chat in progress isn't summarized after every message), go newest
  first, a few per worker pass, and threads older than a year or too short are stored and
  searchable but not summarized (they can be, on request).
- **The relay is boring on purpose.** Standard library only (the Mac is a 2012 MacBook on
  macOS 10.15 with Python ≥ 3.8), reads the live database read-only (SQLite WAL readers
  don't block Messages), keeps a cursor, and retries without losing or repeating anything.

## Upload contract

`POST /v1/ingest/messages` (ingest token), or `POST /ingest/messages` on the MCP HTTP app
(MCP token; forwarded unchanged). JSON:

```json
{
  "source": "imessage",
  "handles": [{"handle": "+15550100", "name": "Mia Chen"}],
  "chats": [{"id": "iMessage;-;+15550100", "name": null, "group": false,
             "handles": ["+15550100"]}],
  "messages": [{"id": "6F1A…", "chat": "iMessage;-;+15550100",
                "at": "2026-10-01T18:02:11Z", "from_me": false, "handle": "+15550100",
                "text": "Dinner at 7?", "edited_at": null, "deleted": false,
                "reply_to": null, "attachments": [{"name": "IMG_0042.HEIC",
                "type": "image/heic"}]}]
}
```

| Field | Rules |
|---|---|
| `source` | `imessage` (later `sms`, `whatsapp`); ids are per source |
| `handles` | optional; a phone number or email and its name in the Mac's Contacts (`null` = no contact). At most 2,000 |
| `chats` | optional for chats the server already knows; `name` is a group's name (`null` for none), `group` true for a group chat, `handles` its other participants. At most 1,000 |
| `messages` | at most 1,000 per call. `id` the message's own id (iMessage `guid`), `chat` a chat id from this or an earlier call, `at` ISO-8601 (or epoch seconds), `from_me`, `handle` the sender (null when `from_me`), `text` at most 4,000 characters, `edited_at` when it was last edited, `deleted` true once unsent, `reply_to` the id it answers, `attachments` file name and type only (never the file) |

Ids are `[A-Za-z0-9;:+@._%=-]{1,200}` (handles: an email or a phone number, at most 200
characters, no control characters).

- **Idempotent.** Sending a message again with the same text and edit time changes nothing.
  A new `text` or `edited_at` updates the line (unless `erase_fact` forgot it: it stays
  `[forgotten]`); `deleted: true` removes the line and keeps the receipt, so a retry can't
  bring it back.
- A message with no text and only attachments reads `[photo]`, `[video]`, `[audio]` or
  `[file: name]`. A message with neither is skipped.
- A handle's name updates the lines of that handle when they still show the old contact name
  or the bare handle; a name the owner gave in Athena (`rename_speaker_everywhere`) wins.
- Answer: `{"ok": true, "messages": {"added", "updated", "unchanged", "deleted",
  "skipped"}, "threads": {"changed", "created", "removed"}}`. Errors are 400 with a message
  naming the first bad field; nothing of a refused call is stored.

`DELETE /v1/ingest/messages/chats/{source}/{chat id}` (relay `DELETE
/ingest/messages/chats/{source}/{chat id}`, the chat id URL-encoded) removes a chat: its
messages, threads, their facts and to-dos. The relay calls it when the owner excludes a chat.
Idempotent (`"result": "already"`).

## Threads

A chat's messages, in time order, start a new thread at:

- a gap of more than `CONVO_TEXT_THREAD_GAP_SECONDS` (default 6 h), or
- a gap of more than 30 minutes once the thread has 150 messages, or
- 300 messages (a chat that never pauses still gets summaries of a size the model handles).

After an upload, each chat with new, changed or removed messages is threaded again over the
stretch from the start of the thread before its earliest change to the end of the thread
after its latest. Each new thread takes the conversation id of the old thread it shares the
most messages with (each old id at most once); old threads that end up empty are merged into
the one that took most of their messages (their facts and to-dos move, as when two
recordings join), and a thread with no old one gets a new conversation. A thread whose
messages and texts are unchanged isn't touched, so its summary stays.

A thread's conversation: device = the source (`imessage`), its lines' speaker = the sender's
name (the owner's name and `is_owner` for the owner's messages), each line at the message's
time. `get_conversation`, `search_conversations` and the `/view` page mark it
`(Text messages with Mia Chen)` or `(Group text: Weekend crew)`; its search entry starts with
the same words, so "texts with Mia" finds it. `search_conversations(kind="text")` lists only
text threads (also read: `texts`, `message`, `imessage`, `sms`, `chat`); `in_person`
leaves them out; `conversation` and the default include them.

## People

A handle (phone number or email) with a name in the owner's contacts is a person in
Athena's `people`: the same person as a recorded speaker of that name, with the same stable
id. So one person's conversations, texts, facts and to-dos are found together, and
`list_people(format="json")` lists each person's `handles`, which the hub uses to link the
person to its contacts by number or email (hub plan:
[people-context.md](https://github.com/SDarioosh/odysseus-expanded/blob/barvis/docs/fork/people-context.md)).

- A handle without a contact shows as the handle itself until a name arrives; then its
  lines take the name.
- A contact renamed on the Mac renames the person everywhere (a rename, so the old name
  stays an alias), unless the owner already named that person differently in Athena: the
  owner's name wins and the contact name becomes an alias.
- Two different people with one name are one person until the owner tells them apart (as
  for recorded speakers).

## Summaries

A thread is summarized like a recorded conversation, with the chat's name, its people and
"these are text messages" put before the transcript, and:

- only once it has been quiet for `CONVO_TEXT_SUMMARY_QUIET_SECONDS` (default 2 h); a new
  message after that re-summarizes it (facts and to-dos said again keep their ids);
- after every waiting recording, newest thread first, at most
  `CONVO_TEXT_SUMMARIES_PER_PASS` (default 10) per worker pass;
- threads that ended more than `CONVO_TEXT_SUMMARY_DAYS` (default 365; 0 = all) ago are
  stored without a summary (`discarded`, reason `archived`); `POST
  /v1/conversations/{id}/summarize` summarizes one anyway;
- a thread with fewer than `CONVO_SUMMARY_MIN_WORDS` words is not sent to the model (as for
  recordings); it reads as "Texts with Mia" with no summary.

Such a thread stays searchable line by line either way.

## Messages relay (on the Mac)

`messages-relay/README.md` has the setup. In short:

- `python3 -m athena_messages_relay doctor` checks Full Disk Access, the database, Contacts
  and the server; `run` keeps it going (a `launchd` agent installed by `install`), `once`
  uploads what's new and exits, `exclude` and `include` change the excluded chats.
- Reads `~/Library/Messages/chat.db` with `mode=ro`; never writes it, never copies it. Polls
  `MAX(ROWID)` every 15 s (cheap) and uploads in batches of up to 500.
- Cursor: the last row id the server accepted, and (macOS 13+) the last edit and unsend
  times; written atomically after each accepted batch, so a crash or a failed upload sends
  the same batch again (idempotent on the server).
- Text: `message.text`, else decoded from `attributedBody` (newer macOS keeps the text only
  there). Tapbacks (reactions), group events (renames, people added) and messages with
  neither text nor attachments are skipped.
- Names: from the Mac's Contacts databases, phone numbers matched on their last 10 digits;
  read again when Contacts change.
- A message the server refuses (a 400) is found by splitting the batch and left out (its
  id, never its text, is logged), so one odd message can't stall the relay. A wrong token
  or URL stops it until fixed.
- **Filters on the Mac, before anything is sent:** excluded chats (by chat id, group name or
  a participant's handle or name), one-time codes and verification messages, and anything
  before the start date (`since`). Excluding a chat later also deletes it on the server.

## Privacy

- Other people's words are in every chat: the owner decides what is excluded, and it is
  filtered on the Mac.
- Codes and verification messages never leave the Mac.
- Attachments: only their names and types are sent, never the files.
- Retention, `erase_fact` and `retire_fact` work on text threads as on recordings.
- Tests and the demo data use fictional chats only.

## Measure

- Server tests: `server/tests/unit/texts/` (threading, ingest, scheduling, MCP output).
- Relay tests: `python -m pytest -q messages-relay` with a fictional `chat.db` built by the
  tests (no Mac needed).
- `server/scripts/seed_demo.py --texts` adds fictional text threads for the eval.
