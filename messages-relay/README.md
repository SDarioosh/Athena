# Athena messages relay

A small program for the Mac that keeps a copy of your iMessage history: it reads the
Messages app's database (read-only), looks up names in Contacts, and uploads new and
changed messages to the Athena server. Athena then makes each chat's threads searchable
and summarizes them (design: [docs/messages.md](../docs/messages.md)).

- Standard library only, Python 3.8 or newer: it runs on a 2012 MacBook Pro with macOS
  10.15 Catalina.
- It only reads `~/Library/Messages/chat.db` and never writes, copies or moves it.
- It talks only to your own Athena server, over Tailscale, with a token.

## Privacy

- **Codes stay on the Mac.** One-time codes and verification messages, and anything from
  5–6 digit short-code senders (banks, deliveries, sign-in codes), are never sent.
- **You choose the chats.** `exclude` leaves a chat out by its id, group name, phone
  number, email or contact name (also every group that person is in). Excluding a chat
  that was already sent removes it from the server too.
- **No files.** Attachments are sent as their file name and type only.
- Message text is never written to the log; the log has counts only.

## Setup on the MacBook

1. **Messages.** Open Messages, sign in with your Apple ID, and turn on Messages in iCloud
   (Messages > Preferences > iMessage > Enable Messages in iCloud). Leave it open: the
   history downloads in the background. Check that a test text to your phone number and
   one to your Apple ID email both show up.
2. **Python.** Run `python3 --version` in Terminal. On Catalina, macOS offers to install the
   Command Line Tools, which have Python 3.8; that's enough. Or install Python 3.12 from
   python.org. (Homebrew no longer supports Catalina well; you don't need it.)
3. **The relay.** Copy this `messages-relay` folder to the Mac, for example to
   `~/athena-messages-relay`, and run everything below from inside it.
4. **Server and token.**

   ```bash
   python3 -m athena_messages_relay config \
     --url http://<server>:8000/v1/ingest/messages --token <CONVO_INGEST_TOKEN>
   ```

   Or use the MCP port and token: `--url http://<server>:8001/ingest/messages --token
   <CONVO_MCP_TOKEN>`. `--since 2025-01-01` sends only from that day on (the first upload
   of years of history takes a while; the server summarizes the last year). `--no-groups`
   leaves group chats out.
5. **Full Disk Access.** Run `python3 -m athena_messages_relay doctor`. The first time it
   says it can't open the database and prints the path of the Python program. Open
   System Preferences > Security & Privacy > Privacy > Full Disk Access, click the lock,
   click +, press Cmd-Shift-G, paste that path and press Open. Add Terminal too while you
   run commands by hand. Run `doctor` again until every line says ok.
6. **Choose chats.** `python3 -m athena_messages_relay chats` lists chats, newest first.
   `python3 -m athena_messages_relay exclude "Mia Chen"` (or a group name, a number, an
   email, or the id in brackets) leaves one out.
7. **First upload.** `python3 -m athena_messages_relay once` sends everything and prints
   the counts. You can stop it with Ctrl-C at any point; the next run goes on from there.
8. **Keep it running.** `python3 -m athena_messages_relay install` starts it at login and
   restarts it if it stops (a launchd agent, `org.athena.messages-relay`). It checks for
   new messages every 15 seconds and for edits, unsends, exclusions and Contacts changes
   every 5 minutes.

Keep the MacBook plugged in, with sleep turned off in Energy Saver ("Prevent computer
from sleeping automatically when the display is off"). A Mac that slept just catches up.

## Commands

| Command | What it does |
|---|---|
| `config` | sets `--url`, `--token`, `--since`, `--no-groups` / `--groups` |
| `doctor` | checks Full Disk Access, the database, Contacts and the server |
| `chats` | lists chats with their message count and last date |
| `exclude <match>` | leaves chats out; removes them from the server on the next pass |
| `include <match>` | undoes `exclude`; the chats are sent again from the start |
| `once` | sends what's new, then exits |
| `run` | keeps sending (what the launchd agent runs) |
| `install` / `uninstall` | starts or removes the launchd agent |

Files: settings in `~/Library/Application Support/Athena Messages/config.json` (readable by
you only: it holds the token), progress in `state.json` next to it, logs in
`~/Library/Logs/Athena Messages/`.

## Troubleshooting

- **"Can't open … chat.db"**: Full Disk Access is missing for the Python path `doctor`
  prints. After a Python update the path changes: add the new one, then run `install`
  again.
- **Only some chats arrive**: Messages in iCloud is still downloading (it can take hours
  for years of history; they arrive later and are filed in the right place), or texts to
  your phone number don't reach the Mac (step 1).
- **Names show as numbers**: the contact isn't in the Mac's Contacts, or Contacts is
  synced with another account that isn't on this Mac. Names sent later update the lines.
- **"the server refused an upload"** in the log: a wrong URL or token (`doctor` says which);
  the relay tries again every 10 minutes and loses nothing. "the server refused message
  <id>, left out": one message the server can't take (its id, never its text, is in the
  log); the rest of the batch was sent.

## Tests

```bash
python3 -m pytest -q messages-relay      # from the repo root; no Mac needed
ruff check messages-relay
```

The tests build fictional Messages and Contacts databases with the real table names, for
macOS 10.15 and 13+.
