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
  15 s; never asks again, never leaves over it), then `recording`. Each user's raw audio
  goes to `Core::on_audio` (track = Zoom user id, 32 kHz mono s16le on the real SDK); the
  bot's own audio is skipped. `recording no_audio` after 60 s without frames.
- Participants: the list is read once a second while in the meeting; joins, leaves and
  renames go to `Core::on_participant`. Topic at join and on change goes to
  `Core::set_topic`. Chat goes to `Core::on_chat`; a message not sent to everyone is
  private to Athena and flagged so Core drops it. Athena never posts in chat.
- Status lines on stdout only (`STATE <state> [<reason>]`, flushed, one per change); logs on
  stderr. Secrets are never printed. Exit 0 after `ended`, 1 after `failed`, 2 for bad
  arguments (`failed bad_arguments`).

| Status line | When |
| --- | --- |
| `starting waiting_for_owner` | `obf`: Zoom refuses until the owner is in; retried every 3 s |
| `starting waiting_for_host` | the meeting hasn't started; Zoom waits for the host |
| `waiting_room` | in the waiting room |
| `failed owner_not_joined` / `meeting_not_started` / `waiting_room_timeout` | one of the three waits above passed `--waiting-room-seconds` |
| `failed wrong_passcode` / `meeting_not_found` / `join_token_expired` / `join_failed` / `auth_failed` / `sdk_error` / `work_dir` | join or start-up failed (the SDK code is logged) |
| `ended meeting_ended` / `removed` | the host ended the meeting / removed Athena |
| `ended owner_left` | `obf`: Zoom ended the session without the host ending it |
| `ended connection_lost` | no join token and Zoom dropped the session after joining |
| `ended alone` | no other participant for `--alone-seconds` |
| `ended max_duration` | `--max-seconds` after the bot started |
| `ended server_stop` | an upload answered `"stop": true` |
| `ended stopped` | SIGTERM/SIGINT, or the upload token was refused (401/403) |

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
