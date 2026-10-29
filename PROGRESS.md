# Athena progress

Athena (formerly convo-ai / barvis-ai / barvis-sound / odysseus-sound) is the first module of
[Odysseus](https://github.com/SDarioosh/odysseus-expanded), our Odysseus fork: wearables → local server
(VAD, whisper.cpp, diarization, llama-server, SQLite) → MCP server → Odysseus. See MISSION.md and AGENTS.md.

_Last updated: 2026-10-29. Shortened: the Done log and each row's full status are in
[docs/changelog/2026-10.md](docs/changelog/2026-10.md), the decisions in [docs/decisions-log.md](docs/decisions-log.md)._

## Status

| # | Item | Status | Where |
|---|------|--------|-------|
| 1 | Omi local-ingestion research | ✅ done | [docs/omi-ingestion.md](docs/omi-ingestion.md) |
| 2 | Frontend decision | ✅ decided: hybrid via MCP, frontend an Odysseus fork | [docs/frontend-decision.md](docs/frontend-decision.md) |
| 3 | Rewrite AGENTS.md / MISSION.md / PROGRESS.md | ✅ done | [AGENTS.md](AGENTS.md) |
| 4 | Ingestion API | ✅ done, tested | [docs/api/ingest.md](docs/api/ingest.md) |
| 5 | Pipeline: VAD, whisper.cpp, diarization, splitting, summaries, digest | ✅ done, tested | `server/` |
| 6 | SQLite storage, search, read API with transcript links | ✅ done, tested | [server/README.md](server/README.md) |
| 7 | MCP server | ✅ done, tested | [docs/api/mcp.md](docs/api/mcp.md) |
| 7b | Hermes vs Odysseus side-by-side test | ✅ done: Hermes 15/20, Odysseus 3/20 | [docs/frontend-decision.md](docs/frontend-decision.md) |
| 7c | Odysseus fork fixed for our MCP tools | in progress: 9/20, target ≥ 15/20 | hub [FORK.md](https://github.com/SDarioosh/odysseus-expanded/blob/barvis/FORK.md), [roadmap](https://github.com/SDarioosh/odysseus-expanded/blob/barvis/docs/fork/roadmap.md) |
| 7d | Agent docs and module contract | ✅ merged | [AGENTS.md](AGENTS.md), hub [modules.md](https://github.com/SDarioosh/odysseus-expanded/blob/barvis/docs/fork/modules.md) |
| 7e | Cloud sandbox stack (CPU, fictional data) | ✅ working | hub [sandbox.md](https://github.com/SDarioosh/odysseus-expanded/blob/barvis/docs/fork/sandbox.md) |
| 8 | Omi ingestion path (Android BLE relay) | optional, depends on the XIAO | [docs/omi-ingestion.md](docs/omi-ingestion.md) |
| 9 | T-Rex 3 Pro Zepp test app | merged, **waiting for the owner's watch test** | [zepp-app/README.md](zepp-app/README.md) |
| 9d | Watch voice profile (owner enrollment) | PR open, **needs the real watch** | `zepp-app/page/enroll.js` |
| 9b | Tailscale-only access to host services | **deferred** until the system is close to ready | — |
| 9c | Calendar and to-dos from action items | built on both sides, **needs owner verification on real voice notes** | [docs/api/mcp.md](docs/api/mcp.md) |
| 11 | XIAO ESP32-S3 Sense pendant | after the T-Watch Ultra; plan written | [docs/xiao-esp32s3-sense-plan.md](docs/xiao-esp32s3-sense-plan.md) |
| 11b | T-Watch Ultra recorder | milestone 1 on the owner's watch; BLE upload path built | [firmware/recorder/README.md](firmware/recorder/README.md), [plan](docs/t-watch-ultra-plan.md) |
| 11d | T-Watch S3 recorder | built, needs checks on the real watch | `firmware/recorder/boards/twatch_s3/` |
| 11e | T4-S3 conference recorder | built, on the owner's board, mics not yet wired | `firmware/recorder/boards/t4_s3/` |
| 11c | Android relay app | built, unit-tested, **needs a real phone** | [android-relay/README.md](android-relay/README.md) |
| 11d | T5 PDA, phone side | built, unit-tested, not on hardware | [docs/t5-pda-plan.md](docs/t5-pda-plan.md) |
| 12 | One-command deploy | built, **needs the owner's server run** | `server/scripts/deploy.sh`, [server/README.md](server/README.md) |
| 12b | Docker, step 1 (module containers) | built, **needs the owner's server run** | [server/README.md](server/README.md#docker) |
| 13 | Speaker recognition and focused summaries | PRs open, **owner: Server verification** | SDarioosh/Athena#31, SDarioosh/Athena#38 |
| 14 | T5 E-Paper Pro PDA firmware | milestones 0–3 on the owner's T5; relays and power still open | [firmware/pda/README.md](firmware/pda/README.md) |
| 15 | Today insight and confirmed facts | built, tested, **owner: Server verification** | [docs/api/mcp.md](docs/api/mcp.md) |
| 16 | Forget propagates (`retire_fact`) | built, tested, **owner: Server verification** | [docs/api/mcp.md](docs/api/mcp.md) |
| 17 | People ids, explicit action items | built, tested, **owner: Server verification** | [docs/api/mcp.md](docs/api/mcp.md) |
| 18 | One-step pairing through Odysseus | merged, **needs a real phone** | [docs/api/mcp.md](docs/api/mcp.md), [android-relay/README.md](android-relay/README.md) |
| 19 | Open loops for the hub's Today insight (`with`) | ✅ built and tested | [docs/api/mcp.md](docs/api/mcp.md) |
| 20 | Complete forget (`erase_fact`) and retention | built, tested, **owner: Server verification** | [docs/api/mcp.md](docs/api/mcp.md) |
| 21 | Capture quality (pyannote attribution) | built, default with pyannote, **owner: one real recording** | [docs/omi-audio-quality.md](docs/omi-audio-quality.md) |
| 22 | Written notes from the hub | built, tested, **owner: Server verification** | [docs/api/ingest.md](docs/api/ingest.md) |
| 23 | Athena rename in code, settings and services | folded into the cleanup (25), step 1 (links) done; Phase 4: `ATHENA_*` settings beside `CONVO_*`, `athena.db` (an existing `convo.db` used as is), phone relay on `/api/codex/athena/*` with fallback, built and tested, **owner: Server verification on a data copy and a phone update** | [docs/athena-rename-plan.md](docs/athena-rename-plan.md) |
| 24 | Zoom meeting bot | built through Phase 5; **owner: real-SDK meeting test** | [docs/zoom-meeting-bot-plan.md](docs/zoom-meeting-bot-plan.md), [meeting-bot/README.md](meeting-bot/README.md) |
| 10 | End-to-end with real devices | not started | — |
| 25 | Cleanup of Athena and the hub | in progress: Phases 0 and 1 merged, Phase 2 merged (gate next) | [docs/cleanup-plan.md](docs/cleanup-plan.md) |

## Next

- **Owner: capture quality** (4.3 PR, Server verification step 2): the GPU eval is done
  (2026-10-24); still one real recording with a third person joining, and the owner-hint
  log lines.
- **Owner: voice-note commands on real speech** (this branch's PR, Server verification): record a
  few of the PR's sample voice notes on the watch ("um, make a todo to get eggs", "make a note in
  voice recorder that the case needs to be 3 mm bigger", "add eggs, milk and bread to my shopping
  list") and check where each lands in the hub.
- **Owner: forget propagates** (this branch's PR, Server verification): forget one belief on the
  hub's `/beliefs` page on the test instance and confirm the fact is gone from `/sound what facts
  do you know about …` and stays gone after the next hourly run.
- **Owner: Today insight end to end** (this branch's PR, Server verification): confirmed-fact
  counts at the default thresholds, one insight on `qwen3.5-35b` (time), the phone relay with
  a token that has `insight:read`, the T5 Today tab (look, tick, taps) on the real panel.
- **Owner: transcription quality** (PRs #39 and #42, Server verification): the AMI eval on
  the GPU before/after, switch whisper to large-v3-turbo-q5_0, then the 24 kbps firmware on
  the watch (encode time per frame, drain time, a fictional-speech recording).
0. **Owner: Server verification** of SDarioosh/barvis-sound#31 (recognition eval with
   large-v3-turbo, optional pyannote on the GPU) and SDarioosh/barvis-sound#38 (re-summarize a copy of
   the real DB, bakeoff on the test instance with qwen3.5-35b), then a real-device check:
   name a speaker once, see the name in the next recording.
- **Owner: voice notes end to end**: buttons and notes are verified on the watch (round 17);
  still to do: drain a note with `ble_relay.py --once` and check it arrives as kind `note`
  with a title and summary.
0. **Owner: T-Watch Ultra upload path test** (round 12 PR, Server verification): `--list`
   from the Windows PC works, and so does `--once` into the server (round 14). Still to do:
   the Android app; report the BLE transfer time (PC: ~2 s per 53 KB chunk incl. upload). Encoder:
   ~10.3 ms avg per 20 ms frame at 80 MHz (about half a core).
1. **Owner: T-Watch Ultra milestone 1 test** (`firmware/recorder/README.md`, PR Server
   verification). Then milestone 2: power baseline (plan §7.3, §8): current for board
   idle, mic on, SD write, screen on; whether I2S PDM RX allows light sleep; whether the
   AXP2101 reports discharge current (it has no battery-current ADC channel in its
   register map, so expect a USB meter or PPK2); full-discharge run.
2. **Owner: T-Rex watch test.** The steps are in `zepp-app/README.md`. Server URL
   `http://<server-tailscale-ip>:8000`; the token is in that checkout's `server/.env`.
3. **Odysseus fork:** citations from 9/20 to ≥ 15/20 (hub `docs/fork/roadmap.md`).
4. **Owner: server verification** of the failed-summary retry (steps in its PR), and of
   SDarioosh/Barvis#5 (full eval on the test instance; expect no approvals for read-only
   tools and a score ≥ 9/20; steps in that PR).
5. Build the real T-Rex recording app from the watch-test results.
6. ~~systemd units for `whisper-server`, `convo-server serve`, and MCP over HTTP.~~ Built
   (round 13, `server/scripts/deploy.sh`); owner: Server verification.
7. Owner voice enrollment from the watch (round 10), plus speaker-threshold check on
   real audio.
8. T-Watch Ultra recorder: milestones 3+ (`docs/t-watch-ultra-plan.md` §8) once the
   power baseline says go.
9. XIAO pendant, after the T-Watch Ultra: a second `boards/` directory on the same firmware.
10. Room pickup (`docs/omi-audio-quality.md` §7, sandbox results 2026-10-20): the firmware
    Opus default is now 24 kbps (16 kbps cost 4–5 WER points at 2–3 m). Parakeet + hole
    re-decoding beat whisper base.en, but **whisper large-v3-turbo beats Parakeet on the
    people around the wearer** (3 m, others: 52 vs 64 % WER), so turbo stays the default;
    Parakeet is better on the owner and on speaker labels. Next: turbo's speaker confusion on
    the table recording (32.6 %). **Owner: Server verification**, then watch battery and
    drain time at 24 kbps.
    VAD gain stays off (it helps only on very quiet input).
11. T5 E-Paper Pro PDA (`docs/t5-pda-plan.md`): firmware milestones 0–3 done on the owner's
    T5 (2026-10-20, portrait); the PC relay (`ble_relay.py pda`) and the phone's `PdaSync` are
    built and tested against fakes; the hub calendar REST is confirmed (plan §6). Next: run both
    relays against the real T5, `item_status` through to the sandbox server's `PATCH`, then
    milestone 4 (phone + calendar on the Odysseus test instance, 127.0.0.1:7010) and milestone 5
    (sleep, power, flash encryption). Owner: create an Odysseus token with only the calendar
    scopes, and find the CalDAV calendar's id for the relay settings.

Decisions so far: [docs/decisions-log.md](docs/decisions-log.md).

## Open questions for you

The questions from round 2 were answered on 2026-10-18 and are under "Decisions so far".
Still open:
- Voice-note commands: "add eggs, milk and bread to my shopping list" is now three lines; "salt
  and pepper" (no comma) stays one. Should "eggs and milk" (no comma) be two lines too?
- Today insight: the T5's rule-based fallback puts today's all-day events in DO FIRST
  ("today, all day") before the timed ones. Keep, or leave all-day events out?
- Confirmed facts: are 3 conversations over 2 days the right bar once real counts are in,
  or should a fact the owner confirms by hand (Phase 1 review page) be the main path?
- Diarization (4.3 PR): split + line check are the pyannote defaults now. Use
  `CONVO_PYANNOTE_VBX_FB=0.2` against newcomers getting an existing name (more
  "Speaker N" in exchange)? Decide after a real recording.
- Enrollment without the Zepp watch (quality checks, enrolling from T-Watch voice notes)
  and a "speaker unsure" flag for the hub: proposed in the 4.3 session, not decided.
- Insights: should doctor's instructions like "avoid hard food until the follow-up" be
  action items? The model lists them; they're not things to do by a date.
- Owner loudness hint: it never fired in the eval (also not with simulated arm movement).
  It now logs `owner hint: speaker N of M named …`; check the log on real wrist
  recordings, and set `CONVO_OWNER_HINT_MARGIN=0` if it names someone else. Remove it then?
- Voice notes: is ending the conversation session and resuming it after a note right, or
  should a note be refused while a conversation records? Should notes get their own place
  in the daily digest?
- T-Watch upload path: 16 kbps was not good enough in the eval (round 16); 24 kbps is
  the new default. Confirm on your recordings that it beats 16 kbps and that encode time
  and drain time are fine. (Pairing: answered, a 6-digit passkey on the watch screen.)
- T-Watch Ultra: everything under "Needs the real watch" above. Also: does the battery
  take a 4.2 V charge target (set conservatively; LilyGoLib asks the chip for 4.288 V)?
- T5 PDA: the questions in `docs/t5-pda-plan.md` §11 (firmware layout, feed window,
  Wi-Fi at home, transcript QR, auto-blank, languages). New: should the phone send a POSIX
  time zone (`tz_posix`) in the feed, so times after a DST change are right? And how does
  the real panel look to you (contrast, VCOM 1560 mV, ghosting after a day)?
- Android app: `docs/android-app-plan.md` (Odysseus WebView shell + the relay in one app).
  Owner decisions in its §10: repo layout, app ID/label, how native, tokens, distribution.
- Odysseus fork: enable GitHub Actions on `SDarioosh/Barvis` (Actions tab) so CI runs on PRs.
- The remaining XIAO pendant decisions in `docs/xiao-esp32s3-sense-plan.md` §10
(battery/form factor, fallback board, extra parts, microSD). They wait until XIAO work
starts, after the T-Watch Ultra.
