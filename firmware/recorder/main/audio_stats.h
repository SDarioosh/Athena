/* Level statistics for 16-bit PCM: peak, RMS, DC offset, clipped and zero samples. Pure C,
 * host-tested. Used to check that the mic delivers real audio (right PDM slot, sane gain)
 * without copying a WAV off the card. */
#pragma once

#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint64_t count;
    int64_t sum;          /* for the DC offset */
    uint64_t sum_squares; /* for the RMS */
    uint32_t peak;        /* largest |sample| */
    uint64_t clipped;     /* samples at full scale (-32768 or 32767) */
    uint64_t zeros;       /* exact zeros: a dead or wrong slot reads all zeros */
} audio_stats_t;

void audio_stats_reset(audio_stats_t *s);
void audio_stats_add(audio_stats_t *s, const int16_t *samples, size_t n);

/* Mean sample value (DC offset), rounded toward zero. 0 if empty. */
int32_t audio_stats_dc(const audio_stats_t *s);
/* RMS of the signal with the DC offset removed, floor of the square root. 0 if empty. */
uint32_t audio_stats_rms(const audio_stats_t *s);
/* RMS relative to full scale (32768) in dB; -999 for silence. */
float audio_stats_rms_dbfs(const audio_stats_t *s);
/* One line for the log, e.g. "rms 312 (-40.4 dBFS), peak 2100, dc -12, clipped 0,
 * zeros 0.0%, 160000 samples". Always NUL-terminated. */
void audio_stats_describe(const audio_stats_t *s, char *out, size_t len);
