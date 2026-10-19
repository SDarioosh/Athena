/* PDM microphone capture: I2S PDM RX, 16 kHz, mono, 16-bit. Shared across boards; only
 * the pins come from the board layer. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "board.h"
#include "esp_err.h"

#define MIC_SAMPLE_RATE 16000
#define MIC_BITS 16
#define MIC_CHANNELS 1

esp_err_t mic_init(board_mic_pins_t pins);
/* Fails with ESP_ERR_INVALID_STATE if someone else is already using the mic. Blocks ~250 ms
 * while the start-up transient is read and dropped. */
esp_err_t mic_start(void);
/* Blocks until `max_samples` are read or the timeout passes. */
esp_err_t mic_read(int16_t *buf, size_t max_samples, size_t *got, int timeout_ms);
esp_err_t mic_stop(void);
