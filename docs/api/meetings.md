# Meeting bot API

Moved here from [server/README.md](../../server/README.md) on 2026-10-29. The bot itself is in [meeting-bot/](../../meeting-bot/README.md); building it in Docker is under "Meeting bot in Docker" in [server/README.md](../../server/README.md#meeting-bot-in-docker).

## Meeting bot

Athena can join a Zoom meeting as a participant named "Athena" and record one track per
participant (the bot itself, `athena-meeting-bot`, is lane 1A of
`docs/zoom-meeting-bot-plan.md`). The launcher (`athena/meetings.py`) is imported
on the first meeting request; nothing about it runs between meetings: no service, thread,
timer or poller. Same bearer token as every route.

| route | answer |
|---|---|
| `POST /v1/meetings` JSON `{"url": "<Zoom link>"}` or `{"meeting_id": "123 4567 8901", "passcode": "…"}`, optional `"title"` (≤ 200) | `202 {"id", "state": "starting", "session_id"}`; `503 {"detail": "zoom_signin_needed", "message"}` when the join token mode needs Athena's Zoom sign-in and it is missing or expired (the message says to sign in from Odysseus or run `convo-server zoom-login`); `502 {"detail": "zoom_unavailable", "message"}` when Zoom's API can't be reached; `400 {"detail"}` for a link that isn't a Zoom meeting or a bad body (`detail` is always a message string, e.g. `title is longer than 200 characters`; a title is stripped first); `413 {"detail"}` for a body over 16 KB; `409 {"detail": "busy", "message", "active": "<meeting id>"}` when `CONVO_MEETING_BOT_MAX` bots are active; `501 {"detail"}` when meetings are off (`CONVO_MEETING_BOT_PATH` unset or not executable, no SDK key/secret, a bad meeting setting); `500` when the bot can't be started |
| `GET /v1/meetings/{id}` | `{"id", "state", "reason", "active", "session_id", "meeting_id", "title", "join_token_kind" (`zak`, `obf` or `none`), "created_at", "started_at", "ended_at"}` (ISO-8601; `started_at` = first recording); 404 |
| `GET /v1/meetings?limit=20` | `{"meetings": [...]}`, newest first (the current one, if any, is first) |
| `DELETE /v1/meetings/{id}` | the bot leaves: SIGTERM, it flushes and ends the session; SIGKILL after 10 s. Answers once it exited, with the final row (`ended`, `stopped`; never `active` for a bot that exited), within about 15 s. Idempotent; 404 |

States: `starting` → `waiting_room` → `waiting_permission` → `recording` → `ended` or
`failed`, each with a `reason` where it helps. `waiting_permission`: Zoom gives raw audio
only to a host, co-host or an app the host allowed to record, so once fully in the meeting
the bot asks for recording permission once (Zoom shows the host its own prompt) and waits;
after a Deny the reason is `denied` and it keeps waiting, because the host can still allow
it later from the participant menu. A bot that is already host, co-host or allowed goes
straight to `recording`. `starting` may carry `waiting_for_owner` (join token `obf`: Zoom
refuses the join until the owner is in the meeting; the bot keeps trying). `recording` may
carry `no_audio` (Zoom delivers no audio). `ended` reasons: `meeting_ended`, `alone` (only
participant for `CONVO_MEETING_ALONE_SECONDS`), `removed`, `owner_left` (join token
`obf`: Zoom ends the bot's session when the owner leaves), `stopped` (DELETE),
`max_duration`, `server_stop`, `exited`. `failed` reasons come from the bot
(`wrong_passcode`, `meeting_not_started`, `waiting_room_timeout`, `owner_not_joined` (the
owner didn't join within the wait), `join_token_expired`, `join_failed`, `auth_failed`,
`sdk_error`, …) or the launcher (`bot_not_started`, `bot_crashed`, `server_restarted`). The bot asks
for recording permission once, never again, never leaves over it, and never leaves
because someone asks it to.

**Links** accepted: `https://zoom.us/j/<number>?pwd=…` and any subdomain of `zoom.us`
(`us06web.zoom.us`, vanity `acme.zoom.us`; `http` or no scheme is read too),
`/wc/join/<number>` and `/wc/<number>/join`, and `zoommtg://` / `zoomus://` links with
`confno`; path words (`j`, `wc`, `join`) in any case. The number has 9-11 digits; `pwd` is
optional. Refused with a message: personal links `/my/<name>` (no number in them),
webinars `/w/` (not yet), registration and host start (`/s/`) links, Zoom for Government
(`zoomgov.com`, with or without a scheme, its own message), any other host.
`parse_zoom_link` holds the rules; the hub mirrors them.

**Safety caps.** With `CONVO_MEETING_BOT_RUNNER=auto` (default) the first join checks once
whether `systemd-run --user --scope` works. If so: `systemd-run --user --scope --quiet
--collect --unit=athena-meeting-<id> -p CPUQuota=$CONVO_MEETING_BOT_CPU -p
MemoryMax=$CONVO_MEETING_BOT_MEMORY -p RuntimeMaxSec=<CONVO_MAX_SESSION_SECONDS + 90> nice
-n 10 <bot> …`. The launcher itself stops the bot at `CONVO_MAX_SESSION_SECONDS` + 60 s
(SIGTERM, SIGKILL 10 s later, `ended`/`max_duration`); systemd's limit, 30 s after that,
is only a backstop, and a bot it kills after its max duration is `max_duration` too, not
`bot_crashed`. Else (Docker, background runner) the bot runs in its own process group at
`nice 10` with `RLIMIT_AS` = `CONVO_MEETING_BOT_MEMORY` (no CPU cap), and the launcher
stops it at the same lifetime. When the bot exits, anything it left in its process group
(and the scope) is killed and its work dir is removed.

**State, no polling.** The launcher starts the bot from the request's event loop and keeps
one asyncio task per running bot: it reads the bot's status lines and waits for its exit
on a pidfd (no thread). The task ends with the bot. After a server restart, active rows
are checked on the next meeting request: if the pid is no longer that bot, the row
becomes `failed`/`server_restarted`; a bot still running counts as busy and `DELETE`
stops it. On exit the session is ended (`stop_reason` `meeting_<reason>`) if the bot
didn't end it. When the server is its container's PID 1 (the sound-api container), its exit
would kill every bot, so shutdown first sends them SIGTERM (final rows `ended server_stop`).

**Upload token.** The bot never gets `CONVO_INGEST_TOKEN`. Each meeting gets a random
token (stored only as a SHA-256 hash, cleared when the bot exits) that is accepted only
for `POST /v1/ingest/chunk` with that meeting's `session_id`, `device=zoom` and `kind`
`conversation` (the default; `note` answers 403), and
`POST /v1/sessions/<that session>/end` and `/chat` (Phase 3). Every other route answers
403; after the bot exits, 401.

**SDK signature.** The Meeting SDK signature is signed locally (HS256) from
`CONVO_ZOOM_SDK_KEY` / `CONVO_ZOOM_SDK_SECRET`: `appKey`, `mn`, `role` 0, `iat`, `exp` and
`tokenExp` (Zoom: 30 min to 48 h; here `CONVO_MAX_SESSION_SECONDS` + 1 h).

**Zoom sign-in (meetings in other accounts).** Since 2026-03-02 Zoom lets a Meeting SDK
app join a meeting hosted by another Zoom account only on behalf of a signed-in Zoom user
(developers.zoom.us/docs/meeting-sdk/obf-faq/). The owner signs Athena in once with
**their own Zoom account**:

1. In the Meeting SDK app on the Zoom Marketplace, add OAuth (user-managed) with the scope
   `user:read:token` and a redirect URL. Nothing needs to answer at that URL.
2. Set `CONVO_ZOOM_CLIENT_ID`, `CONVO_ZOOM_CLIENT_SECRET` and `CONVO_ZOOM_REDIRECT_URL`
   in `server/.env`.
3. Sign in **from Odysseus**: Record a call → **Sign in to Zoom**. Odysseus opens a Zoom
   page; sign in with your own Zoom account and allow the app, then paste the address the
   browser went to (or just its `code`) into Odysseus. No terminal needed. Or, on the
   server, run `convo-server zoom-login`: it prints the Zoom URL and asks for the address
   the same way. Both use PKCE and check `state`: a pasted address (or query) must carry
   this sign-in's `state`, and one with `error=` is refused; a bare code has no state to
   check (PKCE ties it to this sign-in).
4. The refresh token is saved to `<data dir>/zoom/refresh_token.json` (mode 0600,
   replaced atomically). `convo-server zoom-login --status` (or Odysseus) says whether
   Athena is signed in and which join token the next meeting gets. Delete the file to
   sign out.

The routes behind Odysseus's button (`athena/zoom_signin.py`, imported on the first
sign-in request; bearer `CONVO_INGEST_TOKEN` like every `/v1` route). The MCP HTTP app
forwards `GET /zoom/signin`, `POST /zoom/signin/start` and `POST /zoom/signin/finish`
unchanged (status and JSON) with the ingest token, behind the MCP token, like `/meetings`;
they are not MCP tools.

| Route | Answer |
|---|---|
| `GET /v1/zoom/signin` | `200 {"configured": bool, "signed_in": bool, "join_token": "obf" \| "zak" \| "none", "missing": [...]}`. `configured`: client id, secret and redirect URL all set; `missing` names the unset ones (`CONVO_ZOOM_CLIENT_ID`, `CONVO_ZOOM_CLIENT_SECRET`, `CONVO_ZOOM_REDIRECT_URL`); `join_token` is what the next join uses (an invalid `CONVO_ZOOM_JOIN_TOKEN` shows as `none`; joins then answer 501). No network. |
| `POST /v1/zoom/signin/start` (no body) | `200 {"url": "<Zoom authorize URL>", "expires_in": 600}`: open the URL, sign in, allow. The attempt (state, PKCE verifier, time) waits in `<data dir>/zoom/pending_login.json` (0600, replaced atomically) for 600 s; a new start replaces it. The verifier is never returned. `501 {"detail"}` naming the missing settings when not configured. |
| `POST /v1/zoom/signin/finish` JSON `{"pasted": "<the address the browser went to, or its code>"}` | `200 {"ok": true, "signed_in": true, "join_token"}`, the refresh token saved and the attempt removed. `400 {"detail"}` (plain words) for no attempt waiting or one older than 10 minutes ("start the sign-in again"), a bad paste (another attempt's `state`, no `state`, `error=`, nothing pasted, more than 4096 characters) or Zoom refusing the code or the client; a bad paste or a refused code keeps the attempt until it expires, so the owner can paste again. `502 {"detail"}` when Zoom can't be reached; `501` when not configured; `413` for a body over 8 KB. The pasted text and tokens are never logged. |

`CONVO_ZOOM_JOIN_TOKEN` picks the join token:
- `auto` (default): `obf` once signed in, else `none`.
- `obf`: an on-behalf-of token for the signed-in owner and this meeting number. Athena
  shows as "Athena" but is admitted only once the owner is in the meeting: until then the
  bot keeps trying (`starting`/`waiting_for_owner`) and gives up after
  `CONVO_MEETING_WAITING_ROOM_SECONDS` (`failed`/`owner_not_joined`). Zoom ends the bot's
  session when the owner leaves (`ended`/`owner_left`).
- `zak`: join as the signed-in user, whether or not the owner is in the meeting. Meant
  for a later option, a dedicated Zoom account for Athena signed in with `zoom-login`;
  whether Zoom accepts a ZAK alone for meetings in other accounts is not settled yet.
- `none`: no token, only meetings in the app's own account.

A ZAK or OBF token is fetched when the join is requested and lasts about 2 hours (an OBF
token is for that one meeting). The bot's wait (waiting room, owner) is therefore capped
at the smaller of `CONVO_MEETING_WAITING_ROOM_SECONDS` and the token's lifetime minus
5 minutes (`--waiting-room-seconds`), so it gives up with `owner_not_joined` or
`waiting_room_timeout` rather than trying a dead token; if Zoom still says the token
expired, the bot fails with `join_token_expired`. Start a new join to get a fresh token.

Only on a join request, the launcher trades the refresh token for an access token, saves
the new refresh token Zoom hands back (it rotates on every use) before using the access
token, and asks `GET https://api.zoom.us/v2/users/me/token?type=zak` (or
`type=onbehalf&meeting_id=<number>`). These are the only network calls, to Zoom's API,
with a 5 s timeout each; no audio or transcript goes there, nothing refreshes in the
background, and no token is logged or put on a command line. Zoom expires a refresh token
that goes unused for a long time, and the owner can revoke it: when Zoom refuses it, the
file is moved aside to `refresh_token.expired.json` (so `GET /v1/zoom/signin` says
`signed_in: false`), `POST /v1/meetings` answers 503 `zoom_signin_needed` ("Athena's Zoom
sign-in expired … sign in from Odysseus (Record a call → Sign in to Zoom) or run
convo-server zoom-login") and starts nothing; sign in again either way.

**Bot contract** (lane 1A implements it):

```text
<CONVO_MEETING_BOT_PATH> --meeting-id <9-11 digits> --session-id zoom-<id>
    --upload-url http://127.0.0.1:<CONVO_PORT> --name Athena
    --max-seconds <CONVO_MAX_SESSION_SECONDS> --alone-seconds <CONVO_MEETING_ALONE_SECONDS>
    --waiting-room-seconds <min(CONVO_MEETING_WAITING_ROOM_SECONDS, 6900 with a join token)>
    --codec opus|wav --work-dir <data dir>/meetings/<id>
```

- Environment (secrets never go in argv): `ATHENA_MEETING_JWT` (the SDK join token),
  `ATHENA_UPLOAD_TOKEN` (bearer for uploads), `ATHENA_MEETING_PASSCODE` (the passcode or
  the link's `pwd` value; absent when there is none), `ATHENA_ZOOM_JOIN_TOKEN` (ZAK or OBF
  token from the owner's Zoom sign-in, fetched for this join, valid about 2 h) and `ATHENA_ZOOM_JOIN_TOKEN_KIND` (`zak` or `obf`;
  both absent when the join token mode is `none`). Plus `PATH`, `HOME`, `LANG`, `TZ`, `TMPDIR`, `XDG_RUNTIME_DIR`,
  `DBUS_SESSION_BUS_ADDRESS` from the server, nothing else.
- stdin is `/dev/null`; the working directory is `--work-dir` (for unsent chunks; removed
  after exit). stderr: free-form logs, into the server's log.
- stdout: one line per state change, exactly `STATE <state>` or `STATE <state> <reason>`;
  `<state>` one of `starting` (the launcher sets it; the bot sends it only with a
  reason, `waiting_for_owner`, `waiting_for_host` or `reconnecting`), `waiting_room`, `waiting_permission`, `recording`,
  `ended`, `failed`, `<reason>` `[a-z0-9_]{1,40}`. Other lines are ignored. Flush each line.
  Ignore SIGPIPE/EPIPE on stdout: after a server restart nobody reads it, and the bot
  keeps recording.
- Uploads: `POST <upload-url>/v1/ingest/chunk` with `Authorization: Bearer
  $ATHENA_UPLOAD_TOKEN`, `device=zoom`, the given `session_id`, `track` (Zoom user id),
  `track_name` (display name), a sequence per track. Obey `"stop": true` (only for
  `max_duration` or an ended session: a tracked session never stops on silence) by leaving
  with `ended server_stop` (or `max_duration`). End with `POST
  <upload-url>/v1/sessions/<session_id>/end`.
- Leave on: meeting ended, alone for `--alone-seconds`, removed, SIGTERM, `--max-seconds`
  after start. Waiting room longer than `--waiting-room-seconds`: `failed
  waiting_room_timeout`, or `ended waiting_room_timeout` when the host moved the bot back
  to the waiting room after it was in the meeting (let in again: back to `recording` or
  `waiting_permission`, same session, same per-track sequences). While breakout rooms are
  open the alone rule waits (the bot stays in the main room).
- Network drop after joining (lane 5B): `STATE starting reconnecting` while the SDK
  reconnects or the bot rejoins (up to 3 tries within 2 minutes of the drop); back in:
  `recording` or `waiting_permission` again, same session id, same per-track sequences
  (chunks of a track continue, never restart at 0). Given up: `STATE ended
  connection_lost`; the meeting gone meanwhile: `ended meeting_ended`.
- Recording permission: once fully in the meeting, without host, co-host or recording
  rights, call the SDK's `RequestLocalRecordingPrivilege` **once** and print `STATE
  waiting_permission` (once more after a rejoin, only if the host had allowed it before:
  Zoom forgets it when the bot rejoins). Allowed: `STATE recording`. Denied: `STATE waiting_permission
  denied`, then keep waiting (an Allow later still gives `STATE recording`). Never ask
  again and never leave over permission; `--waiting-room-seconds` does not apply here
  (the meeting's end, the alone rule, SIGTERM, `owner_left` and `--max-seconds` still
  end it). If the host takes the permission away while recording, print `STATE
  waiting_permission` again and resume (`STATE recording`, same tracks) once it is given
  back. Already host, co-host or allowed: `STATE recording` straight away.
- With an `obf` token: while Zoom refuses the join because the authorizing user isn't in
  the meeting yet (SDK `MEETING_FAIL_AUTHORIZED_USER_NOT_INMEETING`), retry and print
  `STATE starting waiting_for_owner` (once); after `--waiting-room-seconds` give up with
  `STATE failed owner_not_joined`. Zoom ending the session because the owner left is
  `STATE ended owner_left` (also when a rejoin after a network drop is refused because
  the owner is no longer in the meeting). A join token Zoom calls expired: `STATE failed
  join_token_expired`.
- SIGTERM: flush the last chunks, end the session, print `STATE ended stopped`, exit 0
  within 5 s (SIGKILL follows after 10 s), in every phase, also while rejoining.
- Chat but no chunk ever uploaded (no recording permission): before the chat call the bot
  uploads one second of silence without a `track` (sequence 0), so the session exists;
  being silent it adds no transcript line, and a `zoom` session without tracks is a
  meeting.
- Exit 0 after `STATE ended <reason>`; non-zero after `STATE failed <reason>`. A non-zero
  exit without a `failed` line becomes `failed`/`bot_crashed`.

## Meeting chat, topic and participants

`POST /v1/sessions/{id}/chat` (JSON, same bearer token; lane 1B's per-meeting bot token
also works for its own session) is the Zoom bot's one call when a meeting ends, before or
after `/end`:

```json
{"lines": [{"at": "2026-10-13T15:00:21Z", "from": "Dana Example",
            "zoom_user_id": "16782336", "text": "Here's the sheet: https://…/budget-v3.xlsx",
            "private": false}],
 "topic": "Q4 budget review",
 "participants": [{"zoom_user_id": "16782336", "name": "Dana Example",
                   "joined_at": "2026-10-13T15:00:02Z", "left_at": null}]}
```

- `at`, `joined_at`, `left_at`: ISO-8601 (naive = UTC) or epoch s/ms, 2000..2100.
  `zoom_user_id`: the sender's Zoom user id, the same value as the chunk `track`
  (`[A-Za-z0-9._:-]{1,64}`), optional. `from`, `name`: display names, at most 200
  characters kept. `text`: control characters and line breaks become spaces, cut at 4,000
  characters; empty lines are skipped. `private: true` (a message to Athena alone) is
  dropped unread. Every field but `lines[].at` and `lines[].text` is optional; unknown
  fields are ignored. `topic` (at most 200 characters) and `participants` replace the
  stored ones when given; leaving them out keeps them.
- A chat line becomes a transcript line of kind `chat` at `at` (start = end), from the
  sender's person: the line is named exactly as the sender's track is (owner included);
  a sender without a track by their cleaned names, made a person when new. One
  `zoom_user_id` is one person: every name it uses in the call (chat `from`, then its
  `participants` name, preferred as Zoom's latest) is matched together and kept as
  aliases. No VAD, no whisper. A participant who never spoke or typed is only looked up, never made a
  person.
- Idempotent: each line is stored once per session (SHA-256 of its cleaned time, sender id,
  sender name and text), also after `erase_fact` redacted it or retention removed it.
- Order: before the meeting is processed the lines wait and join its conversation then;
  during processing the session goes back for one more pass; afterwards they are added to
  its conversation, which is summarized again on the worker's next pass.
- Limits: body at most 1,000,000 bytes, at most 2,000 lines and 1,000 participants (413).
  Errors: 404 unknown session, 409 not a meeting (no tracks and device not `zoom`), 422
  not JSON or wrong types, 400 a bad time or `zoom_user_id`, 401 no token.
- Response: `{"ok": true, "session_id", "result": "queued"|"attached"|"unchanged",
  "added", "duplicates", "private_dropped", "conversation_id"}` (`conversation_id` once
  the meeting has one; `attached`: added to a processed meeting).

The summary prompt gets "This is an online meeting with the topic …", the participants, and
chat lines as `[21] Dana Example (chat): …` (links and file names kept as written).
`GET /v1/conversations/{id}` has `meeting` (`topic`, `participants`) for every
conversation of a meeting session, a session with tracked chunks (any device) or device
`zoom`, also when the bot never made the chat call (then `topic` is null and
`participants` are the speakers heard); any other conversation has `meeting: null`. JSON
fact and action-item rows say `"source": "meeting"` for exactly those conversations. Each
transcript segment has `kind` (`speech` or `chat`). `participants` (`name`,
`person_id`, `is_owner`, `zoom_name`, `joined_at`, `left_at`): the list the bot sent, one
entry per `zoom_user_id` (two people with one name stay two; entries without an id are
told apart by join time), each by their person's current name, then everyone else heard
or seen in the chat, in order of first line. A participant Zoom shows only as a device or
room ("iPhone", "Zoom user") whose lines are named "Speaker N" (no person) is listed by
those speakers instead of twice; with one such participant they take its `zoom_name` and
times. `athena/meeting_chat.py`
is imported on the first chat call (or when a meeting is summarized), never at startup.

