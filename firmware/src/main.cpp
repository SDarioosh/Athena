/*
 * Battery Logger for T-Watch S3
 * Logs timestamp to FATFS every minute
 * 
 * Feature 1: Basic logging demonstration
 */

#include <Arduino.h>
#include <SPI.h>
#include <SD.h>

// SD card configuration
#define SD_CS_PIN    20

// FATFS configuration
#define FATFS_SIZE 5000
#define FATFS_BLOCK_SIZE 512

// Logging interval in seconds
#define LOG_INTERVAL_SEC 60

// File path
const char* LOG_FILE = "/battery.log";

void setup() {
  Serial.begin(115200);
  delay(1000);

  // Initialize logging
  Serial.println("=== Battery Logger v1.0 ===");
  Serial.println("Logging to FATFS every " + String(LOG_INTERVAL_SEC) + " seconds");
  Serial.println();

  // Initialize SD card
  Serial.println("Initializing SD card...");
  if (!SD.begin(SD_CS_PIN)) {
    Serial.println("SD card initialization failed!");
    while (1) {
      delay(1000);
    }
  }
  Serial.println("SD card initialized successfully");
  Serial.println("  Free space: " + String(SD.cardSize()) + " bytes");
  Serial.println();

  // Format SD card if needed
  if (!SD.exists(LOG_FILE)) {
    Serial.println("Creating log file...");
    File f = SD.open(LOG_FILE, FILE_WRITE);
    if (f) {
      f.println("=== Battery Log ===");
      f.println("Started: " + String(millis()) + "ms");
      f.close();
      Serial.println("Log file created");
    }
  }

  Serial.println("Setup complete. Starting logging...");
  Serial.println("---------------------------------------------------");
}

void loop() {
  // Log every LOG_INTERVAL_SEC seconds
  static uint32_t lastLogTime = 0;
  uint32_t currentTime = millis();

  if (currentTime - lastLogTime >= LOG_INTERVAL_SEC * 1000) {
    // Log entry
    File f = SD.open(LOG_FILE, FILE_WRITE);
    if (f) {
      String timestamp = String(millis());
      String line = "[" + timestamp + "] Entry logged";
      
      f.print(line);
      f.println();
      
      Serial.println("Logged: " + line);
      f.close();
    } else {
      Serial.println("Error: Could not open log file for writing");
    }
    lastLogTime = currentTime;
  }

  delay(100);
}
