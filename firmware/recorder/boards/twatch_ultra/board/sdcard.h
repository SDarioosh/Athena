/* microSD over SPI (shared bus with LoRa and NFC). Mounted at SDCARD_MOUNT_POINT. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#define SDCARD_MOUNT_POINT "/sdcard"

esp_err_t sdcard_mount(void);
esp_err_t sdcard_unmount(void);
bool sdcard_is_mounted(void);
esp_err_t sdcard_info(uint64_t *total_bytes, uint64_t *free_bytes);
