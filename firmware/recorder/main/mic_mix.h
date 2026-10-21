/* Mixing an I2S mic array down to the recorder's mono 16-bit stream, and per-mic stats for
 * finding mics that aren't there. Pure C; host-tested (test/test_mic_mix.c).
 *
 * Input: one buffer per I2S port, interleaved stereo frames of 32-bit slots (left, right),
 * as the ESP32-S3 I2S driver delivers them. An INMP441 sends 24 bits, MSB first, so the top
 * 24 bits of each slot are the sample. Channel numbers: port * 2 + slot (0 = left), so with
 * two ports: 0 = pair A left, 1 = pair A right, 2 = pair B left, 3 = pair B right.
 *
 * Mix: the average of the mics in `mask`, then MIC_MIX_SHIFT bits down to 16 bits, saturated.
 * Averaging keeps the level the same whatever the number of mics; speech adds up in phase
 * (the mics are a few cm apart) while their self-noise doesn't, so four mics have ~6 dB
 * less noise than one. Beamforming can replace the plain average later. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define MIC_MIX_MAX_PORTS 2
#define MIC_MIX_MAX_CH (MIC_MIX_MAX_PORTS * 2)
/* 24-bit average >> 7 = 6 dB above plain truncation to 16 bits: an INMP441 (-26 dBFS at
 * 94 dB SPL) then clips at 114 dB SPL, far above a meeting room. */
#define MIC_MIX_SHIFT 7

typedef struct {
    uint64_t count;
    int64_t sum;     /* 24-bit samples */
    double sumsq;
    int32_t min;
    int32_t max;
} mic_ch_stats_t;

/* The 24-bit sample in a 32-bit I2S slot. */
static inline int32_t mic_mix_sample24(int32_t slot)
{
    return slot >> 8;
}

static inline int mic_mix_count(uint8_t mask)
{
    int n = 0;
    for (int ch = 0; ch < MIC_MIX_MAX_CH; ch++) {
        n += (mask >> ch) & 1;
    }
    return n;
}

/* Mix `frames` frames of `nports` ports into out[frames]. Channels in `mask` beyond the
 * ports given are ignored; no channel left gives silence. */
void mic_mix_frames(const int32_t *const ports[], int nports, size_t frames, uint8_t mask,
                    int16_t *out);

void mic_mix_stats_reset(mic_ch_stats_t stats[MIC_MIX_MAX_CH]);
/* Add `frames` frames of every channel of `nports` ports. */
void mic_mix_stats_add(mic_ch_stats_t stats[MIC_MIX_MAX_CH], const int32_t *const ports[],
                       int nports, size_t frames);
/* RMS of the channel without its DC offset, in dB below 24-bit full scale; -144 if silent. */
double mic_mix_rms_dbfs(const mic_ch_stats_t *s);
/* A connected mic delivers changing samples: its self-noise alone moves the low bits. A
 * missing one reads a constant (usually 0 or -1: nothing drives its slot of the data line;
 * the line's pull-down or the bus keeper holds it). Alive = more than a few LSBs of spread. */
bool mic_mix_alive(const mic_ch_stats_t *s);
/* Mask of the alive channels among the first nports * 2. */
uint8_t mic_mix_alive_mask(const mic_ch_stats_t stats[MIC_MIX_MAX_CH], int nports);

#ifdef __cplusplus
}
#endif
