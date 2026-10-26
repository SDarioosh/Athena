/* Microphone capture, 16 kHz, mono, 16-bit. Shared across boards; only the wiring comes from
 * the board layer (board_mic_pins_t): one PDM mic (I2S PDM RX), or up to four I2S mics
 * (INMP441) mixed down to mono (mic_mix.h). mic_read() returns conditioned audio: high-passed
 * (DC offset and rumble removed) and amplified, see audio_dsp.h. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "board.h"
#include "esp_err.h"
#include "mic_mix.h"

#define MIC_SAMPLE_RATE 16000
#define MIC_BITS 16
#define MIC_CHANNELS 1

#define MIC_HIGHPASS_HZ 100
/* With the board's EQ, speech at ~30 cm lands around -22 dBFS and the limiter catches the
 * loudest syllables; the owner picked this level in a listening test (2026-10-19). Milestone 3
 * picks the final value by transcription accuracy; `gain <dB>` on the console changes it. */
#define MIC_DEFAULT_GAIN_DB 30

esp_err_t mic_init(board_mic_pins_t pins);
int mic_gain_db(void);
/* Saves the gain in NVS; takes effect at the next mic_start(). Not while the mic runs. */
esp_err_t mic_set_gain_db(int gain_db);
/* The board's EQ (board_mic_eq), on by default. Saved in NVS; next mic_start(). */
bool mic_eq_enabled(void);
int mic_eq_band_count(void);
esp_err_t mic_set_eq_enabled(bool on);
/* Samples the limiter turned down / that still clipped, since the last mic_start(). */
uint64_t mic_limited_samples(void);
uint64_t mic_clipped_samples(void);
/* Fails with ESP_ERR_INVALID_STATE if someone else is already using the mic. Blocks ~250 ms
 * while the start-up transient is read and dropped. */
esp_err_t mic_start(void);
/* Blocks until `max_samples` are read or the timeout passes. */
esp_err_t mic_read(int16_t *buf, size_t max_samples, size_t *got, int timeout_ms);
esp_err_t mic_stop(void);
/* I2S mic array: DMA buffers dropped since mic_start() because reads came too late. */
uint32_t mic_overflows(void);

/* ---- I2S mic array only (BOARD_MIC_I2S) ----
 * Channels: 0 = pair A left, 1 = pair A right, 2 = pair B left, 3 = pair B right. By default
 * every mic that answers at mic_start() is mixed (MIC_MASK_AUTO); a fixed mask picks mics. */
#define MIC_MASK_AUTO 0xFF
/* 4 with two pairs, 2 with one, 0 for a PDM mic. */
int mic_array_channels(void);
/* "A-L", "A-R", "B-L", "B-R". */
const char *mic_array_channel_name(int ch);
/* Mics that answered / that are mixed, as of the last mic_start(). */
uint8_t mic_array_alive_mask(void);
uint8_t mic_array_mask(void);
uint8_t mic_array_mask_setting(void);
/* Pairs delivering audio while the mic runs (2, or 1 if pair B gets no clock). */
int mic_array_pairs_live(void);
/* Reads since mic_start() where pair B delivered less than pair A (0 = in step). */
uint32_t mic_array_pair_b_short_reads(void);
/* Saved in NVS; next mic_start(). MIC_MASK_AUTO = every mic that answers. */
esp_err_t mic_array_set_mask(uint8_t mask);
/* Per-mic stats of everything mic_read() reads while on (costs CPU: console only). */
void mic_array_stats_enable(bool on);
void mic_array_stats(mic_ch_stats_t out[MIC_MIX_MAX_CH]);
