# convo-ai: agent guide

convo-ai is a local, always-on conversation recorder. A LilyGO T-Watch S3 records
speech; a home server transcribes, diarizes, and summarizes it with local models.
Nothing goes to the cloud.

## Layout

- `firmware/`: PlatformIO project for the T-Watch S3 (ESP32-S3, Arduino framework, LilyGoLib).
- `server/`: Python pipeline and HTTP API.
- `docs/`: architecture notes and the privacy/consent note.
- `orchestrator/`: the build loop that runs agents. Don't modify it from a feature branch.

## Hardware facts (T-Watch S3)

- ESP32-S3, 8 MB PSRAM, 16 MB flash (about 9.9 MB usable as FATFS). No SD card.
- PDM mic (Knowles SPM1423), AXP2101 PMU with fuel gauge, DRV2605 haptics, PCF8563 RTC.
- 470 mAh battery. Battery life is the main constraint: keep the screen off, the CPU
  clock low, and radios off except during uploads.
- LoRa (SX1262) cannot carry audio. Leave it off.

## Architecture

1. Watch: VAD-gated capture, Opus encode, timestamped ring buffer on FATFS.
2. Watch uploads chunks over Wi-Fi to the server when on a known network and the
   buffer passes 60%. Chunks are deleted only after the server acknowledges them.
3. Server: Silero VAD, whisper.cpp transcription, diarization with the owner's voice
   enrolled, conversation splitting on gaps of more than 3 minutes.
4. llama-server (OpenAI-compatible) produces per-conversation JSON (title, summary,
   key points, action items, people), then a daily digest.
5. SQLite stores transcripts and summaries, with embeddings for search.
6. Raw audio is deleted after transcription by default.

## Checks (run these before finishing)

- Firmware: `pio run -d firmware`
- Server tests: `cd server && python -m pytest -q`
- Server lint: `cd server && ruff check .`

Skip a check only if its project doesn't exist yet.

## Conventions

- Python 3.11+, type hints, `ruff` clean, pytest for tests.
- Firmware: no blocking calls on the audio path; log errors to serial with a clear prefix.
- Config and credentials come from files listed in `.gitignore` (for example
  `firmware/include/secrets.h`, `server/.env`). Commit only `.example` versions.
- Small, focused commits with descriptive messages.

## Rules for agents

- Work only on your branch. Never push, merge, or open PRs; the orchestrator does that.
- Never commit secrets or audio files.
- If you're stuck, end your reply with `ESCALATE: <reason>` instead of guessing.
- Code you can't verify on real hardware is fine, but say what still needs testing on
  the device in your summary.
