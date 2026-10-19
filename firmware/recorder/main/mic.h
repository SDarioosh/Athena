/* PDM microphone capture: I2S PDM RX, 16 kHz, mono, 16-bit. Shared across boards; only
 * the pins come from the board layer. mic_read() returns conditioned audio: high-passed
 * (DC offset and rumble removed) and amplified, see audio_dsp.h. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "board.h"
#include "esp_err.h"

#define MIC_SAMPLE_RATE 16000
#define MIC_BITS 16
#define MIC_CHANNELS 1

#define MIC_HIGHPASS_HZ 100
/* Starting point: speech at ~30 cm lands around -31 dBFS. Milestone 3 picks the final value
 * by transcription accuracy; `gain <dB>` on the console changes it (saved in NVS). */
#define MIC_DEFAULT_GAIN_DB 24

esp_err_t mic_init(board_mic_pins_t pins);
int mic_gain_db(void);
/* Saves the gain in NVS; takes effect at the next mic_start(). Not while the mic runs. */
esp_err_t mic_set_gain_db(int gain_db);
/* Samples clipped by the gain since the last mic_start(). */
uint64_t mic_clipped_samples(void);
/* Fails with ESP_ERR_INVALID_STATE if someone else is already using the mic. Blocks ~250 ms
 * while the start-up transient is read and dropped. */
esp_err_t mic_start(void);
/* Blocks until `max_samples` are read or the timeout passes. */
esp_err_t mic_read(int16_t *buf, size_t max_samples, size_t *got, int timeout_ms);
esp_err_t mic_stop(void);
