# Convo-Ai Zepp App Progress

**Hardware:** Amazfit T-Rex 3 Pro with Zepp OS 5

---

## Current Status

- **Step 1 (Feasibility Test):** In progress
- **Step 2-8:** Not started

---

## Step 1: Feasibility Test

### Goal

Verify that 30-second Opus chunk recording works on T-Rex 3 Pro with screen on and off.

### Files Created

- `zepp-app/package.json` - Dependencies and build config
- `zepp-app/src/recorder.js` - Recording logic
- `zepp-app/src/main.js` - Main entry point
- `zepp-app/src/app.js` - UI components
- `zepp-app/.gitignore` - Ignore build artifacts

### Build & Install Instructions

```bash
# Enable Developer Mode on T-Rex 3 Pro
# (Hold power button, slide to enable)

# Install zeus-cli globally
npm install -g @zeppos/zeus-cli

# Navigate to app directory
cd /home/steam/projects/convo-ai/zepp-app

# Build the app
zeus build

# Install on device
zeus install
```

### Test A: Record 10 minutes with screen ON

1. Open the Convo-Ai app on watch
2. Press Start button
3. Keep screen on (press power button or enable keep-on mode)
4. Record for 10 minutes (should create ~2 chunks of 30s each)
5. Press Stop button
6. **Report:** Number of chunks recorded, any errors in console

### Test B: Record 10 minutes with screen OFF

1. Open the Convo-Ai app on watch
2. Press Start button
3. Let screen auto-dim and turn off completely
4. Record for 10 minutes
5. Press Stop button
6. **Report:** Number of chunks recorded, any errors, screen behavior

### Expected Behavior

- Each 30-second recording creates a new Opus file
- Chunks are queued on watch until transferred to phone
- Console logs show chunk count
- No crashes or memory issues

### Success Criteria

✅ Recording survives both tests
✅ Chunks are properly created and queued
✅ No crashes or memory issues
✅ Console logs show correct chunk count

---

## Next Steps (Pending Test Results)

1. If Test A and B pass: Proceed to Step 2 (Full Watch App)
2. If tests fail: Debug and fix issues before proceeding

---

## Known Issues

- None yet (waiting for hardware test)

---

## Notes

- **Developer mode required** for all testing
- **Real device testing required** - simulator may not reflect all behaviors
- **Zepp OS docs:** https://docs.zepp.com
- **Media Recorder API:** @zos/media (create, setFormat, start, stop)
