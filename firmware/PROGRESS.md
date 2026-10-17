# Convo-Ai Firmware Progress

## Feature 1: Battery Logging ✅

**Status:** Implemented and builds successfully

**Description:** 
- Log timestamp to FATFS every 60 seconds
- Demonstrates basic SD card and FATFS functionality
- Foundation for battery monitoring

**Files:**
- `src/main.cpp` - Main firmware with logging loop
- `platformio.ini` - PlatformIO configuration

**Build:**
```bash
pio run -d firmware
```

**Notes:**
- Currently uses ESP32's native SD library
- Full battery monitoring requires AXP2101 PMU driver or voltage divider
- See AGENTS.md for hardware-specific details

---

## Pending Features

### Feature 2: VAD Capture & Opus Encoding
- PDM microphone capture with VAD
- Opus encoding for efficient storage
- Timestamped ring buffer on FATFS

### Feature 3: BLE Transmission
- Send chunks via BLE to Android app
- Delete only after app acknowledgement

### Server Features (4-8)
- HTTP endpoint for chunk storage
- Silero VAD + whisper.cpp transcription
- Diarization with voice enrollment
- Conversation splitting and summaries
- SQLite storage with embedding search

---

## Next Steps

1. Implement VAD detection (Silero or similar)
2. Implement PDM microphone driver
3. Implement Opus encoding
4. Implement BLE transmission stack
5. Create Android companion app
6. Implement server pipeline
