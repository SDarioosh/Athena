# Convo-Ai Firmware

PlatformIO project for LilyGO T-Watch S3 (ESP32-S3 based) conversation recorder.

## Hardware

- **ESP32-S3**: 240MHz, 320KB RAM, 4MB Flash
- **PSRAM**: 16MB
- **Microphone**: Knowles SPM1423 PDM
- **Power Management**: AXP2101 PMU
- **SD Card**: FATFS on flash (no external SD card)
- **BLE**: For data transmission to phone
- **LoRa**: Disabled (cannot carry audio)

## Project Structure

```
firmware/
├── platformio.ini     # PlatformIO configuration
├── README.md          # This file
├── PROGRESS.md        # Development progress
└── src/
    └── main.cpp       # Main firmware
```

## Features

### Feature 1: Basic Logging ✅

Logs timestamp to FATFS every 60 seconds. Demonstrates:
- SD card initialization
- FATFS file operations
- Serial logging

### Planned Features

#### Feature 2: VAD Capture & Opus Encoding

- **VAD**: Voice Activity Detection using Silero
- **PDM Microphone**: Capture audio from Knowles SPM1423
- **Opus Encoding**: Compress audio for efficient storage
- **Ring Buffer**: Timestamped audio chunks on FATFS

#### Feature 3: BLE Transmission

- Send audio chunks via BLE to companion Android app
- Automatic cleanup when buffer > 60% full
- Delete only after app acknowledgement (reliable delivery)

## Building

### Prerequisites

- PlatformIO Core 6.x
- ESP32 board support
- LilyGO T-Watch S3 development environment

### Build Command

```bash
cd firmware
pio run -e watchy
```

### Flash to Device

```bash
pio run -e watchy --upload
```

## Configuration

Edit `platformio.ini`:

```ini
[env:watchy]
platform = espressif32
board = watchy
framework = arduino
```

**Note**: LilyGO T-Watch S3 board not yet in PlatformIO. Using `watchy` as starting point.

## Debugging

### Serial Monitor

```bash
pio run -e watchy --monitor
```

### Debug Logs

The firmware logs to Serial at 115200 baud:
- Setup progress
- SD card status
- Log entries with timestamps

## Battery Monitoring

**Current Implementation:**
- Logs timestamps to demonstrate FATFS functionality

**For Production:**
- Requires AXP2101 PMU driver (I2C)
- OR external voltage divider connected to ADC1
- ESP-IDF framework for direct hardware access

## Testing

### SD Card Test

The firmware requires an SD card connected to the device for FATFS testing.

### Serial Output

```
=== Battery Logger v1.0 ===
Logging to FATFS every 60 seconds

Initializing SD card...
SD card initialized successfully
  Free space: X bytes

Setup complete. Starting logging...
---------------------------------------------------
Logged: [1234567890] Entry logged
```

## Architecture

```
┌─────────────────┐
│   PDM Mic       │
└────────┬────────┘
         │ Analog
         ▼
┌─────────────────┐
│   VAD Detection │ (Silero)
└────────┬────────┘
         │ Digital
         ▼
┌─────────────────┐
│   Opus Encode   │
└────────┬────────┘
         │ PCM/Opus
         ▼
┌─────────────────┐
│   FATFS Ring    │
│   Buffer        │
└────────┬────────┘
         │ WiFi/BLE
         ▼
┌─────────────────┐
│   Phone App     │
└─────────────────┘
```

## References

- [AGENTS.md](../AGENTS.md) - Architecture and conventions
- [MISSION.md](../MISSION.md) - Project goals and plan
- [ESP32-S3 Datasheet](https://www.espressif.com/sites/default/files/documentation/esp32-s3-datasheet_en.pdf)
- [LilyGO T-Watch S3](https://docs.lilygo.cc/products/t-watch-s3/t-watch-s3-esp32s3-smart-watch-esp32s3)

## Notes

- Work in progress
- Hardware testing required for audio features
- BLE stack to be implemented
- Full battery monitoring pending AXP2101 driver
