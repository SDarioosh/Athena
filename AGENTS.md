# convo-ai: agent guide

convo-ai is a conversation-recording wearable using Amazfit T-Rex 3 Pro
with Zepp OS. A JavaScript Mini Program on the watch records 30-second
Opus chunks, transfers them via BLE to a phone Side Service, which uploads
to a local server for AI processing (whisper.cpp, Silero VAD, llama.cpp).
Nothing goes to the cloud.

## Layout

- `firmware/`: Zepp OS Mini Program (JavaScript, built with zeus-cli).
- `server/`: Python pipeline and HTTP API for upload, silence detection,
  transcription, diarization, summaries.
- `docs/`: architecture notes, privacy/consent, Zepp OS integration.
- `orchestrator/`: build loop for Zepp app. Don't modify from feature branch.

## Hardware facts (Amazfit T-Rex 3 Pro)

- Zepp OS 5, built-in mic and speaker.
- No SD card; no custom firmware needed.
- All watch code is Zepp OS Mini Program (JavaScript).
- Recording managed by @zos/media Recorder API.
- BLE to phone via @zos/ble/TransferFile.
- No hardware-level VAD; app manages recording sessions.

## Architecture

1. Watch: User starts recording. App records 30-second Opus chunks using
   @zos/media Recorder. Each chunk has session ID, sequence number,
   timestamp. Chunks transferred to phone via BLE.

2. Phone: Side Service receives chunks from watch, uploads to server over
   HTTP. Side Service relays server's "stop": true response to watch,
   which ends recording and vibrates.

3. Server: POST /upload receives chunk + session ID + sequence number +
   timestamp. Runs Silero VAD, tracks silence per session. Returns
   {"ok": true, "stop": <bool>}. Reassembles chunks by session/sequence
   before transcription.

4. Pipeline: whisper.cpp transcription, diarization, conversation splitting
   on gaps > 3 minutes, llama.cpp (OpenAI-compatible) generates per-
   conversation JSON (title, summary, key points, action items, speakers),
   daily digest.

5. SQLite: stores transcripts and summaries with embeddings for search.
   Raw audio deleted after transcription by default.

## Checks

- Zepp app: `cd zepp-app && zeus build` (command may vary per Zepp docs)
- Server tests: `cd server && python -m pytest -q`
- Server lint: `cd server && ruff check .`

Skip a check only if its project doesn't exist yet.

## Conventions

- Python 3.11+, type hints, ruff clean, pytest for tests.
- Zepp app: JavaScript, use @zeppos/zeus-cli, test on real device.
- Config and credentials come from files listed in .gitignore (e.g.,
  server/.env, secrets for upload auth). Commit only .example versions.
- Small, focused commits with descriptive messages.

## Rules for agents

- Work only on your branch. Never push, merge, or open PRs; the orchestrator
  does that.
- Never commit secrets or audio files.
- If you're stuck, end your reply with `ESCALATE: <reason>` instead of guessing.
- Zepp app code must be tested on real device; simulator may not reflect
  all behaviors. Developer mode required for installation.
- Code you can't verify on real device is fine, but note what still needs
  testing in the PR description.

## Key APIs

### Zepp OS Mini Program (@zos/*)

- **Media**: @zos/media - Recorder (create, setFormat, start, stop)
- **BLE**: @zos/ble/TransferFile - watch → phone file transfer
- **Display**: Zepp OS display APIs - keep screen on/dimmed
- **Vibration**: Zepp OS haptics API - vibrate on stop
- **Build**: @zeppos/zeus-cli - build and test Mini Programs

### Server

- POST /upload - receives chunk, session ID, sequence, timestamp
- Silero VAD - silence detection per session
- whisper.cpp - transcription
- llama.cpp - summaries, action items
- SQLite - storage with embeddings
