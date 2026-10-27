# Athena meeting bot

Athena's Zoom meeting bot (`docs/zoom-meeting-bot-plan.md`). `core/` holds the SDK-free
library (chunks, encoding, upload queue, chat); `bot/` holds `athena-meeting-bot`, the Zoom
side. `spike/` is the Phase 0 spike, kept for reference.

## Bot (`bot/`, `athena-meeting-bot`)

The Zoom side: joins one meeting as "Athena" with the Zoom Meeting SDK for Linux, feeds each
participant's raw audio, the participant list, the chat and the topic to `athena::Core`,
and leaves on its own. The launcher (`server/convo_server/meetings.py`) starts it per
meeting; nothing of it runs between meetings. Its command line, environment and status
lines are the launcher's "Bot contract" in `server/README.md`.

### Build

The SDK is proprietary and never in git; point `ZOOM_SDK_DIR` at the unpacked folder (it
holds `h/`, `libmeetingsdk.so` and `qt_libs/`):

```bash
cmake -S meeting-bot -B build-bot -DZOOM_SDK_DIR=$HOME/zoom-sdk
cmake --build build-bot -j
ctest --test-dir build-bot --output-on-failure   # core tests + bot_logic
# -> build-bot/athena-meeting-bot (CONVO_MEETING_BOT_PATH)
```

As the spike: the link `build-bot/sdklib/libmeetingsdk.so.1` (the SDK's soname) is made in the
build dir, and the binary carries an old-style RPATH (`--disable-new-dtags`) to the SDK,
`qt_libs` and `qt_libs/Qt/lib`, so it runs without `LD_LIBRARY_PATH` on the machine it was
built on. It needs the SDK's system libraries (`SDK_LIBS` in `spike/Dockerfile`).

**Sandbox / CI without the SDK:** `-DZOOM_SDK_DIR=$PWD/meeting-bot/bot/sdkstub` builds the
bot against our own stub headers and a fake `libmeetingsdk.so` (`sdkstub/stub_meetingsdk.cpp`).
They are written by us for compile checks, are not Zoom's headers, and the binary never
talks to Zoom: it plays fictional meeting scenarios (`ATHENA_SDKSTUB_SCENARIO`), which the
`bot_stub_scenarios` test runs end to end against a local fake upload API.

### What it does

- Joins as `--name` with video off; never subscribes to video or screen share. Joins with
  the JWT only, or with `ATHENA_ZOOM_JOIN_TOKEN` as `onBehalfToken` (`obf`) or `userZAK`
  (`zak`).
- Once in: asks for recording permission once if it may not record yet
  (`waiting_permission`, `waiting_permission denied`; retries `CanStartRawRecording` every
  15 s; never asks again in that stay, never leaves over it), then `recording`. Each user's raw audio
  goes to `Core::on_audio` (track = Zoom user id, 32 kHz mono s16le on the real SDK); the
  bot's own audio is skipped. `recording no_audio` after 60 s without frames.
- Participants: the list is read once a second while in the meeting; joins, leaves and
  renames go to `Core::on_participant`. Topic at join and on change goes to
  `Core::set_topic`. Chat goes to `Core::on_chat`; a message not sent to everyone is
  private to Athena and flagged so Core drops it. Athena never posts in chat.
- Network drop (lane 5B): when the SDK reports `MEETING_STATUS_RECONNECTING` or the
  session fails after joining, the bot stops taking audio, closes its open chunks (they
  upload during the gap) and prints `starting reconnecting`. While the SDK reconnects on its
  own the bot waits; once the SDK gives up, the bot calls `Join` again, at most 3 tries,
  not before 2 s, 20 s and 60 s after the drop, and gives up 2 minutes after the drop
  (`ended connection_lost`). Back in, it is the same `athena::Core`: same session id, each
  track's sequences go on. It re-reads its own user id (a rejoin gives a new one; its old
  entry is never taken for a participant nor recorded), sets the chat and recording events
  again, joins computer audio and starts raw audio again. Recording permission: asked once
  more only if the host had allowed it before the drop (Zoom forgets it on a rejoin); a
  host who declined or never answered is not asked again. A rejoin refused because the
  meeting is gone ends `meeting_ended`; a wrong passcode or expired token ends
  `connection_lost` at once.
- `obf` and drops: Zoom ends an `obf` bot's session when the owner leaves (spike), with no
  reconnect first, so a session failure after joining *without* `RECONNECTING` stays
  `ended owner_left` (no rejoin). A drop *with* `RECONNECTING` is rejoined; if Zoom then
  refuses because the owner is no longer in, it is `ended owner_left` too.
- Host moves Athena back to the waiting room: like a drop (audio stops, chunks closed),
  status `waiting_room`; let in again, recording goes on with the same Core; after
  `--waiting-room-seconds`, `ended waiting_room_timeout` (`failed` only if it never got in).
- Breakout rooms: the bot is never assigned and stays in the main room. While breakout
  rooms are open (`IMeetingBOController::IsBOStarted`, polled with the participant list)
  the alone rule waits. When fewer than half of the participants who were there when they
  opened are left in the main room, Core notes it and the chat call carries
  `"breakouts": [{"from": <ISO>, "until": <ISO or null>}]`. Today's server ignores unknown
  fields (`ChatIn`), so this note reaches the server's log only; storing it is a proposed
  server change (lane 5B PR). Built without the breakout header, the bot skips all this.
- Host ends the meeting for all (`ended meeting_ended`) or removes Athena (`ended
  removed`): the last chunks, the chat call and `/end` are sent (scenario tests check it).
- Chat but no audio ever uploaded (permission never given): the server has no session for
  the chat call yet, so `Core::finish` first uploads one second of silence without a track
  (sequence 0). It makes the session (device `zoom` without tracks is a meeting) and adds no
  transcript line; then the chat call and `/end`.
- Status lines on stdout only (`STATE <state> [<reason>]`, flushed, one per change); logs on
  stderr. Secrets are never printed. Exit 0 after `ended`, 1 after `failed`, 2 for bad
  arguments (`failed bad_arguments`).

| Status line | When |
| --- | --- |
| `starting waiting_for_owner` | `obf`: Zoom refuses until the owner is in; retried every 3 s |
| `starting waiting_for_host` | the meeting hasn't started; Zoom waits for the host |
| `starting reconnecting` | dropped after joining; the SDK reconnects or the bot rejoins |
| `waiting_room` | in the waiting room (also when the host moved Athena back there) |
| `failed owner_not_joined` / `meeting_not_started` / `waiting_room_timeout` | one of the three waits above passed `--waiting-room-seconds` |
| `ended waiting_room_timeout` | moved back to the waiting room after being in, not let in again in time |
| `failed wrong_passcode` / `meeting_not_found` / `join_token_expired` / `join_failed` / `auth_failed` / `sdk_error` / `work_dir` | join or start-up failed (the SDK code is logged) |
| `ended meeting_ended` / `removed` | the host ended the meeting / removed Athena |
| `ended owner_left` | `obf`: Zoom ended the session without the host ending it |
| `ended connection_lost` | dropped and not back in: 3 rejoin tries failed or 2 minutes passed |
| `ended alone` | no other participant for `--alone-seconds` |
| `ended max_duration` | `--max-seconds` after the bot started |
| `ended server_stop` | an upload answered `"stop": true` |
| `ended stopped` | SIGTERM/SIGINT, or the upload token was refused (401/403) |

Stub-only: `ATHENA_SDKSTUB_FAST_REJOIN=1` makes the rejoin times ten times shorter in a
stub build (compiled out of real builds) so `bot_stub_scenarios` stays quick.

Leaving: the bot leaves the meeting, waits for Zoom's confirmation (at most 2 s, 0.6 s on
SIGTERM), then `Core::finish` flushes the last partial chunks, sends the chat, topic and
participants and ends the session (deadline 20 s after a meeting, 4 s from SIGTERM, 3 s if it
never joined), and prints the final line. On SIGTERM it then exits at once without the SDK's
teardown, well within the 5 s rule.

### SDK names beyond the spike

Everything else the bot calls was compiled and run against SDK 7.1.5 by
`spike/cpp/athena_spike.cpp`. These names are new and checked only against our stub; if
the real build complains, fix them here (and in `sdkstub/h/athena_sdkstub.h`):

- `IMeetingParticipantsController::GetParticipantsList()` returning `IList<unsigned int>*`,
  with `IList::GetCount()` / `GetItem(int)`
- `IMeetingService::GetMeetingInfo()` and `IMeetingInfo::GetMeetingTopic()`
- `MEETING_STATUS_WAITINGFORHOST`
- `MeetingFailCode` values `MEETING_FAIL_PASSWORD_ERR`, `MEETING_FAIL_MEETING_NOT_START`,
  `MEETING_FAIL_MEETING_NOT_EXIST`, `MEETING_FAIL_CANNOT_START_TOKENEXPIRE`
- `EndMeetingReason_JBHTimeOut`
- Now used for behaviour (the spike only had empty overrides or logged them):
  `IMeetingServiceEvent::onMeetingTopicChanged`, `IChatMsgInfo::IsChatToAll` as the private flag.
- Lane 5B: `MEETING_STATUS_RECONNECTING` (the bot only logged status numbers before),
  `IMeetingService::GetMeetingBOController()`, `IMeetingBOController::IsBOStarted()` and
  the header `meeting_service_components/meeting_breakout_rooms_interface_v2.h` (included
  only if it exists, `__has_include`; without it breakout rooms are not detected).
  Behaviour not yet seen on the real SDK: which status/result a network drop gives (the
  bot treats `RECONNECTING`, and any `FAILED` after joining, as a drop), whether `Join`
  can be called again after `FAILED`/`ENDED` in the same process (the owner wait already
  relies on it), whether raw recording must be started again after a reconnect (the bot
  does; a `StartRawRecording` error is then tolerated), and whether participants in
  breakout rooms leave the main room's participant list.

## Cost check

`server/scripts/meeting_bot_cost.py` runs the stub-SDK bot through the `fixture` scenario
(`ATHENA_SDKSTUB_PCM`: a fictional PCM fixture the script generates at run time;
`ATHENA_SDKSTUB_SECONDS`: when the stub's host ends the meeting) and reports CPU, peak RSS
and VmPeak; it fails if any process, socket or file of the bot is left after it exits. CI
runs it after the stub build. With `--watch` it measures the real bot in a real meeting
(once per release). See `server/README.md` "Meeting bot cost check".
