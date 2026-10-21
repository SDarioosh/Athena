#include "mic_mix.h"

#include <math.h>
#include <string.h>

/* Spread (max - min, 24-bit LSBs) above which a channel counts as a connected mic. An
 * INMP441's self-noise (-87 dBFS) spans hundreds of LSBs at 24 bits. */
#define ALIVE_MIN_SPREAD 8

void mic_mix_frames(const int32_t *const ports[], int nports, size_t frames, uint8_t mask,
                    int16_t *out)
{
    if (nports > MIC_MIX_MAX_PORTS) {
        nports = MIC_MIX_MAX_PORTS;
    }
    mask &= (uint8_t)((1u << (nports * 2)) - 1u);
    const int n = mic_mix_count(mask);
    if (n == 0) {
        memset(out, 0, frames * sizeof(out[0]));
        return;
    }
    /* sum / n as a multiply by 65536 / n (rounded up, so 3 equal mics give the exact value):
     * a 64-bit divide per sample is a slow library call on the ESP32-S3. The sum of four
     * 24-bit samples fits 26 bits; times 2^16 fits 64. */
    static const int32_t RECIP[MIC_MIX_MAX_CH + 1] = {0, 65536, 32768, 21846, 16384};
    const int32_t recip = RECIP[n];
    for (size_t f = 0; f < frames; f++) {
        int32_t sum = 0;
        for (int ch = 0; ch < nports * 2; ch++) {
            if (mask & (1u << ch)) {
                sum += mic_mix_sample24(ports[ch / 2][f * 2 + (ch & 1)]);
            }
        }
        /* Arithmetic shift: floor, like truncating a single mic. */
        int64_t v = ((int64_t)sum * recip) >> (16 + MIC_MIX_SHIFT);
        if (v > INT16_MAX) {
            v = INT16_MAX;
        } else if (v < INT16_MIN) {
            v = INT16_MIN;
        }
        out[f] = (int16_t)v;
    }
}

void mic_mix_stats_reset(mic_ch_stats_t stats[MIC_MIX_MAX_CH])
{
    for (int ch = 0; ch < MIC_MIX_MAX_CH; ch++) {
        stats[ch] = (mic_ch_stats_t){.min = INT32_MAX, .max = INT32_MIN};
    }
}

void mic_mix_stats_add(mic_ch_stats_t stats[MIC_MIX_MAX_CH], const int32_t *const ports[],
                       int nports, size_t frames)
{
    if (nports > MIC_MIX_MAX_PORTS) {
        nports = MIC_MIX_MAX_PORTS;
    }
    for (int ch = 0; ch < nports * 2; ch++) {
        mic_ch_stats_t *s = &stats[ch];
        const int32_t *p = ports[ch / 2] + (ch & 1);
        for (size_t f = 0; f < frames; f++) {
            const int32_t v = mic_mix_sample24(p[f * 2]);
            s->sum += v;
            s->sumsq += (double)v * (double)v;
            if (v < s->min) {
                s->min = v;
            }
            if (v > s->max) {
                s->max = v;
            }
        }
        s->count += frames;
    }
}

double mic_mix_rms_dbfs(const mic_ch_stats_t *s)
{
    if (s->count == 0) {
        return -144.0;
    }
    const double mean = (double)s->sum / (double)s->count;
    double var = s->sumsq / (double)s->count - mean * mean;
    if (var <= 0.0) {
        return -144.0;
    }
    const double db = 20.0 * log10(sqrt(var) / 8388608.0); /* 2^23 */
    return db < -144.0 ? -144.0 : db;
}

bool mic_mix_alive(const mic_ch_stats_t *s)
{
    return s->count > 0 && (int64_t)s->max - (int64_t)s->min > ALIVE_MIN_SPREAD;
}

uint8_t mic_mix_alive_mask(const mic_ch_stats_t stats[MIC_MIX_MAX_CH], int nports)
{
    if (nports > MIC_MIX_MAX_PORTS) {
        nports = MIC_MIX_MAX_PORTS;
    }
    uint8_t mask = 0;
    for (int ch = 0; ch < nports * 2; ch++) {
        if (mic_mix_alive(&stats[ch])) {
            mask |= (uint8_t)(1u << ch);
        }
    }
    return mask;
}
