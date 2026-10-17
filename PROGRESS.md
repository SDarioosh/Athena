# Convo-Ai Development Progress

**Hardware:** Amazfit T-Rex 3 Pro with Zepp OS 5

---

## Phase 1: Watch App (Zepp OS Mini Program)

### Step 1: Feasibility Test

**Goal:** Verify 30-second Opus chunk recording works on T-Rex 3 Pro with screen on/off.

**Status:** Not started

**Files to create:**
- `zepp-app/` directory with minimal test app
- `src/recorder.js` - basic recording with start/stop
- `src/app.js` - UI with Start/Stop buttons

**Test Instructions (for user):**

1. **Install on device:**
   - Enable Developer Mode on T-Rex 3 Pro
   - Install `zeus-cli` globally: `npm install -g @zeppos/zeus-cli`
   - Navigate to project: `cd /home/steam/projects/convo-ai/zepp-app`
   - Build: `zeus build`
   - Install: `zeus install`

2. **Test A: Record 10 minutes with screen ON:**
   - Open app, press Start
   - Keep screen on (press power button or enable keep-on)
   - Record for 10 minutes (should create ~2 chunks of 30s each)
   - Press Stop
   - Report: number of chunks recorded, any errors

3. **Test B: Record 10 minutes with screen OFF:**
   - Open app, press Start
   - Let screen auto-dim and turn off
   - Record for 10 minutes
   - Press Stop
   - Report: number of chunks recorded, any errors, screen behavior

**Expected behavior:**
- Each 30-second chunk creates a new Opus file
- Chunks are queued on watch until transferred to phone
- Logs show chunk count to serial/phone

**Success criteria:**
- Recording survives both tests
- Chunks are properly created and queued
- No crashes or memory issues

---

### Step 2: Full Watch App

**Goal:** Enhanced UI with recording state, elapsed time, chunk count, Stop button.

**Status:** Not started

**Features:**
- Show recording state (recording/paused/stop)
- Show elapsed timer
- Show number of chunks uploaded
- Clear Stop button with haptic feedback
- Screen keep-on/dimmed modes
- Queue chunks locally if phone/server unreachable

---

### Step 3: Side Service Integration

**Goal:** Integrate @zos/ble/TransferFile to send chunks to Side Service.

**Status:** Not started

**Features:**
- Send Opus chunks via BLE when complete
- Handle Side Service upload to server
- Receive server "stop": true response
- Trigger recording stop and vibrate on watch

---

## Phase 2: Server

### Step 4: Upload Auth ✅

**Status:** IMPLEMENTED

**Implementation:**
- POST /upload checks `Authorization` header
- Validates token matches `settings.upload_token`
- Rejects unauthorized uploads with 401

**Token:** Stored in `server/config.py` as `upload_token` (should be in `.env` in production)

---

### Step 5: Silence Detection Endpoint ✅

**Status:** IMPLEMENTED

**Implementation:**
- POST /upload runs Silero VAD on each chunk
- Tracks silence duration per session
- Returns `{"ok": true, "stop": <bool>}`
- If silence > 180 seconds (3 min), sets `stop: true`
- Reassembles chunks by session/sequence before transcription

**Silero VAD:** Already loaded and configured

---

### Step 6: Server Pipeline

**Goal:** Full pipeline with whisper.cpp, diarization, summaries.

**Status:** Not started

**Components:**
- Transcription (whisper.cpp)
- Diarization (speaker tracking)
- Conversation splitting (gaps > 3 min)
- Summaries (llama.cpp)
- SQLite storage with embeddings
- Daily digest generation

---

## Phase 3: Mobile App (Side Service)

### Step 7: Side Service Implementation

**Goal:** Receive chunks from watch, upload to server.

**Status:** Not started

**Features:**
- BLE listener for watch chunks
- HTTP upload to server
- Retry logic for failed uploads
- Queue management
- Relay "stop" signal to watch

---

### Step 8: Full Mobile App UI

**Goal:** Display conversations, transcripts, summaries, search.

**Status:** Not started

**Features:**
- Conversation list with timestamps
- Full transcript view with speaker labels
- Summary view with key points and action items
- Semantic search
- Settings (voice enrollment, auto-delete raw audio)

---

## Current Status Summary

- [ ] Phase 1, Step 1: Feasibility test (NOT STARTED)
- [ ] Phase 1, Step 2: Full watch app
- [ ] Phase 1, Step 3: Side service integration
- [ ] Phase 2, Step 4: Upload auth
- [ ] Phase 2, Step 5: Silence detection
- [ ] Phase 2, Step 6: Server pipeline
- [ ] Phase 3, Step 7: Side service implementation
- [ ] Phase 3, Step 8: Full mobile app UI

---

## Open Questions

1. **Zepp OS API documentation:** Need to review @zos/media docs for exact API
2. **Side Service architecture:** Should this be a separate app or integrated into main app?
3. **Token security:** How to distribute and rotate the upload token?
4. **Queue management:** What happens if watch queue fills up?

---

## Notes

- **Do not start Step 2 until Step 1 feasibility test is confirmed**
- **Server work can proceed in parallel** while waiting for hardware test results
- **Zepp OS docs:** https://docs.zepp.com
- **Developer mode required** for all watch testing
- **Simulator limitations:** May not reflect all behaviors; real device testing required
