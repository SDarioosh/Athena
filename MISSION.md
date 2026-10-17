You are building convo-ai in this repository. Read AGENTS.md
first; it describes the hardware, architecture, and checks.

## Goal
Build a conversation-recording wearable using Amazfit T-Rex 3 Pro with:
- Zepp OS Mini Program on watch (JavaScript, built with zeus-cli)
- 30-second Opus chunks recorded by @zos/media Recorder
- Chunks transferred watch → phone via @zos/ble/TransferFile
- Side Service on phone uploads chunks to server over HTTP
- Server runs Silero VAD for silence detection, triggers stop after N min silence
- Full pipeline: whisper.cpp transcription, diarization, summaries, SQLite

**Key changes from T-Watch plan:**
- No firmware work; all watch code is Zepp OS Mini Program
- Recording managed by app, not hardware-level VAD
- Server handles silence detection and stop signaling
- Side Service acts as bridge between watch and server

**Inspiration:** Bee Frontier, Bee Pro, Plaude — but using Amazfit hardware and Zepp OS.

## Plan
Work through these features in order, one at a time:

### Phase 1: Watch App (Zepp OS Mini Program)

1. **Feasibility Test**: Minimal app with Start/Stop button that records
   30-second Opus chunks, logs chunk count. Test on real device with
   screen on and off. See PROGRESS.md for build/install instructions.

2. **Full Watch App**: Enhanced UI with recording state, elapsed time,
   chunk upload count, Stop button. Screen keep-on/dimmed modes.
   Queue chunks locally if phone/server unreachable.

3. **Side Service Integration**: Integrate @zos/ble/TransferFile to send
   chunks to Side Service, which uploads to server. Handle server
   "stop": true response to end recording and vibrate.

4. **Server Silence Detection**: POST /upload endpoint that runs Silero
   VAD, tracks silence per session, returns {"ok": true, "stop": <bool>}.
   Server reassembles chunks by session/sequence before transcription.

5. **Upload Auth**: Add shared token header requirement for /upload to
   prevent random devices from posting audio. Token stored outside git.

6. **Server Pipeline**: whisper.cpp transcription, diarization,
   conversation splitting on gaps > 3 min, llama.cpp summaries,
   SQLite storage with embedding search, daily digest.

### Phase 2: Server & Mobile App

7. **Mobile App**: Receive chunks from Side Service, display conversation
   list, transcripts, summaries, search. Settings for voice enrollment,
  auto-delete raw audio.

8. **Full System Integration**: End-to-end test with real device, phone,
   and server.

## Process for each feature
1. Create a branch: feature/<short-name>.
2. For watch app: implement in zepp-app/, test build with `zeus build`,
   install on device in developer mode.
3. For server: implement with tests, run checks in AGENTS.md.
4. Commit, push.
5. Get review via delegate_task. Fix issues if needed.
6. Merge, switch back to main and pull.
7. Update PROGRESS.md: done, next, open questions. Commit to main.

## Zepp OS Mini Program Notes
- Use @zeppos/zeus-cli for building and testing
- Refer to Zepp OS docs: https://docs.zepp.com
- Media Recorder: @zos/media (create, setFormat, start, stop)
- BLE Transfer: @zos/ble/TransferFile (watch → phone)
- Side Service runs on phone app, handles upload to server
- JavaScript-based, not native code
- Test on real device; simulator may not reflect all behaviors
- Developer mode required for installation

## Server Notes
- POST /upload: receives chunk, session ID, sequence number, timestamp
- Run Silero VAD, track silence, return {"ok": true, "stop": <bool>}
- Reassemble chunks by session/sequence before transcription
- Auth: require shared token header (not committed to git)
- Pipeline unchanged: whisper.cpp, diarization, splitting, llama.cpp, SQLite

## Rules
- Never merge without an APPROVE review and passing checks.
- If you're stuck after 3 rounds or 3 failed attempts on the same
  problem, delegate the problem to a subagent for help. If that
  fails too, write the blocker in PROGRESS.md, skip to the next
  feature that doesn't depend on it, and continue.
- Never commit secrets or audio files.
- Note anything that needs testing on real hardware in the PR
  description.
- Keep going until every feature is merged or blocked.
