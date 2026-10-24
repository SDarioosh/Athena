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

uint32_t audio_stats_rms(const audio_stats_t *s)
{
    if (!s->count) {
        return 0;
    }
    /* Variance = E[x^2] - E[x]^2, in double: the integer version truncated both means first,
     * which leaves up to ~2|DC| in the variance (QC R10). */
    const double n = (double)s->count;
    const double mean = (double)s->sum / n;
    const double var = (double)s->sum_squares / n - mean * mean;
    return var > 0.0 ? (uint32_t)sqrt(var) : 0;
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
