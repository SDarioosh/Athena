/* Mic conditioning: 2nd-order high-pass (removes the DC offset and the rumble below the voice
 * band) followed by a fixed gain with saturation. Pure C, host-tested.
 *
 * Why (measured on the T-Watch Ultra, 2026-10-19): the S3's PDM converter has no high-pass
 * filter and no gain. Speech at ~30 cm arrives at about -55 dBFS, which is normal for a MEMS
 * mic (-26 dBFS at 94 dB SPL) but nearly inaudible in a WAV, on top of a +600 DC offset and
 * low-frequency rumble that held most of the energy in quiet stretches. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define AUDIO_DSP_GAIN_MIN_DB 0
#define AUDIO_DSP_GAIN_MAX_DB 42

typedef struct {
    float b0, b1, b2, a1, a2; /* biquad, normalised (a0 = 1) */
    float x1, x2, y1, y2;
    float gain;    /* linear */
    int gain_db;
    bool primed;      /* state seeded from the first sample, so a DC offset doesn't thump */
    uint64_t clipped; /* output samples that hit full scale since the last reset */
} audio_dsp_t;

/* High-pass corner `hp_hz` (Butterworth, Q = 0.7071) at `rate_hz`, gain in whole dB
 * (clamped to AUDIO_DSP_GAIN_MIN_DB..MAX_DB). Clears the filter state. */
void audio_dsp_init(audio_dsp_t *d, float rate_hz, float hp_hz, int gain_db);
/* Clears the filter state and the clip count; keeps the coefficients and gain. */
void audio_dsp_reset(audio_dsp_t *d);
/* Filters and amplifies `n` samples in place. */
void audio_dsp_process(audio_dsp_t *d, int16_t *samples, size_t n);
