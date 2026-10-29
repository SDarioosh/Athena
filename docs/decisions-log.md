# Decisions log

The owner's decisions so far, moved here from [PROGRESS.md](../PROGRESS.md) on 2026-10-29.

## Decisions so far

- Knowledge layer: **hybrid**. Our SQLite is the source of truth, exposed over MCP.
- Frontend: **Odysseus, forked** (the fork was called Barvis until 2026-10-19), modified
  for our system and kept in sync with upstream. Chosen after the side-by-side test; fork plan in `docs/frontend-decision.md`.
- Focus: the **XIAO pendant** and the **T-Rex 3 Pro** (owned). Omi only if the XIAO falls short.
- Phone: **Android**, with Tailscale access to the server.
- Omi: the current consumer pendant (CV1 in Omi's firmware), not bought yet. The T-Rex 3 Pro
  comes first.
- Silence stop: **5 min** trailing silence (`CONVO_SILENCE_STOP_SECONDS=300`), 4 h max
  session. These are already the defaults.
- Timezone: **UTC** (`CONVO_TIMEZONE`, already the default).
- Retention: **keep full transcripts forever**. No pruning job by default; an opt-in
  `CONVO_TRANSCRIPT_RETENTION_DAYS` exists (2026-10-23, off unless set).
- Forget (2026-10-23): one Forget in the hub that always erases, with a confirmation, and
  no restore; later repeats linked "same" are erased too; beliefs forgotten before this
  get an "Erase" button in the hub, nothing is erased automatically.
- Calendar/tasks: action items stay in SQLite for now; a real calendar/task integration
  comes **later** and is wanted.
- Security: **as tight as possible; reachable only over Tailscale.** Services bind to
  loopback or the Tailscale interface, never `0.0.0.0`. That covers Odysseus' ChromaDB
  (8100), ntfy (8091), and llama-server (8082, no auth).
  **Deferred:** while testing, services stay open to the LAN and Tailscale. Lock down
  before the system goes into daily use.
- The old T-Watch `firmware/` is removed and `firmware-archive/` is deleted and ignored.
- Recorder (2026-10-19): **T-Watch Ultra first, XIAO later**, one shared ESP-IDF firmware
  (can be split later). Buttons as in `docs/t-watch-ultra-plan.md` §3, screen off until a
  button press, microSD overflow, T-Watch S3 left out for now. Late-chunk policy: finalise
  on `/end` + 24 h timeout, re-open on late chunks. Omi live-audio service: skip. A daily
  charge is OK, but battery life should be as long as possible without hurting recording
  quality.
- Pendant: the eventual plan is our own **XIAO ESP32-S3 Sense** pendant
  (`docs/xiao-esp32s3-sense-plan.md`). It sends audio over BLE to the Android app,
  has Wi-Fi off, and is tuned for battery life.
- Recognition: answered 2026-10-23: the owner runs pyannote. The sandbox found pyannote
  names more right but with more wrong names than ECAPA (5.3 vs 1.3 % on the table mic);
  split + line check bring it to 1.6 %. Default diarizer stays `ecapa` in code until the
  server run of the 4.3 PR.
