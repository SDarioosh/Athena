#include "audio_stats.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

void audio_stats_reset(audio_stats_t *s)
{
    memset(s, 0, sizeof(*s));
}

void audio_stats_add(audio_stats_t *s, const int16_t *samples, size_t n)
{
    for (size_t i = 0; i < n; i++) {
        const int32_t v = samples[i];
        const uint32_t mag = (uint32_t)(v < 0 ? -v : v);
        s->sum += v;
        s->sum_squares += (uint64_t)((int64_t)v * v);
        if (mag > s->peak) {
            s->peak = mag;
        }
        if (v == INT16_MIN || v == INT16_MAX) {
            s->clipped++;
        }
        if (v == 0) {
            s->zeros++;
        }
    }
    s->count += n;
}

int32_t audio_stats_dc(const audio_stats_t *s)
{
    return s->count ? (int32_t)(s->sum / (int64_t)s->count) : 0;
}

static uint32_t isqrt64(uint64_t x)
{
    uint64_t r = 0, bit = 1ULL << 62;
    while (bit > x) {
        bit >>= 2;
    }
    while (bit) {
        if (x >= r + bit) {
            x -= r + bit;
            r = (r >> 1) + bit;
        } else {
            r >>= 1;
        }
        bit >>= 2;
    }
    return (uint32_t)r;
}

uint32_t audio_stats_rms(const audio_stats_t *s)
{
    if (!s->count) {
        return 0;
    }
    /* Variance = E[x^2] - E[x]^2, in integers. mean_sq <= 2^30, so no overflow. */
    const uint64_t mean_sq = s->sum_squares / s->count;
    const int64_t mean = s->sum / (int64_t)s->count;
    const uint64_t dc_sq = (uint64_t)(mean * mean);
    return isqrt64(mean_sq > dc_sq ? mean_sq - dc_sq : 0);
}

float audio_stats_rms_dbfs(const audio_stats_t *s)
{
    const uint32_t rms = audio_stats_rms(s);
    return rms ? 20.0f * log10f((float)rms / 32768.0f) : -999.0f;
}

void audio_stats_describe(const audio_stats_t *s, char *out, size_t len)
{
    const double zeros_pct = s->count ? 100.0 * (double)s->zeros / (double)s->count : 0.0;
    snprintf(out, len, "rms %lu (%.1f dBFS), peak %lu, dc %ld, clipped %llu, zeros %.1f%%, %llu samples",
             (unsigned long)audio_stats_rms(s), (double)audio_stats_rms_dbfs(s),
             (unsigned long)s->peak, (long)audio_stats_dc(s), (unsigned long long)s->clipped,
             zeros_pct, (unsigned long long)s->count);
}
