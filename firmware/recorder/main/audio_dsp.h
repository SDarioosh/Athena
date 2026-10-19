/* Mic conditioning, in this order: 2nd-order high-pass (removes the DC offset and the rumble
 * below the voice band), optional peaking-EQ bands (correct the case acoustics), gain, and a
 * peak limiter (instant attack, smooth release) so the gain can't clip. Pure C, host-tested.
 *
 * Why (measured on the T-Watch Ultra, 2026-10-19): the S3's PDM converter has no high-pass
 * filter and no gain. Speech at ~30 cm arrives at about -55 dBFS, which is normal for a MEMS
 * mic (-26 dBFS at 94 dB SPL) but nearly inaudible in a WAV, on top of a +600 DC offset and
 * low-frequency rumble. The mic's port in the case also leaves a ~10 dB hole at 1-2.5 kHz
 * and a ~10 dB peak at 3-4 kHz, which sounds muffled. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define AUDIO_DSP_GAIN_MIN_DB 0
#define AUDIO_DSP_GAIN_MAX_DB 42
#define AUDIO_DSP_MAX_EQ_BANDS 4

typedef struct {
    float hz;
    float gain_db;
    float q;
} audio_dsp_band_t;

typedef struct {
    float b0, b1, b2, a1, a2; /* normalised (a0 = 1) */
    float x1, x2, y1, y2;
} audio_biquad_t;

typedef struct {
    audio_biquad_t hp;
    audio_biquad_t eq[AUDIO_DSP_MAX_EQ_BANDS];
    int eq_count;
    float gain; /* linear */
    int gain_db;
    float ceiling;    /* limiter ceiling, sample units */
    float release;    /* per-sample factor pulling the limiter gain back to 1 */
    float env;        /* current limiter gain, <= 1 */
    bool primed;      /* filters seeded from the first sample, so a DC offset doesn't thump */
    uint64_t limited; /* output samples the limiter turned down, since the last reset */
    uint64_t clipped; /* output samples that still hit full scale (should stay 0) */
} audio_dsp_t;

/* `rate_hz` sample rate, `hp_hz` high-pass corner (Butterworth), `bands` EQ bands (may be
 * NULL; at most AUDIO_DSP_MAX_EQ_BANDS are used), gain in whole dB (clamped to
 * AUDIO_DSP_GAIN_MIN_DB..MAX_DB). The limiter holds peaks at -1 dBFS and releases over
 * ~100 ms. Clears all state. */
void audio_dsp_init(audio_dsp_t *d, float rate_hz, float hp_hz, const audio_dsp_band_t *bands,
                    int band_count, int gain_db);
/* Clears the filter and limiter state and the counters; keeps the settings. */
void audio_dsp_reset(audio_dsp_t *d);
/* Processes `n` samples in place. */
void audio_dsp_process(audio_dsp_t *d, int16_t *samples, size_t n);
