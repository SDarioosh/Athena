#include "audio_stats.h"
#include "check.h"

#include <string.h>

int main(void)
{
    audio_stats_t s;
    audio_stats_reset(&s);
    CHECK_EQ(audio_stats_rms(&s), 0);
    CHECK_EQ(audio_stats_dc(&s), 0);

    /* All zeros: what a dead or wrong PDM slot looks like. */
    const int16_t zeros[8] = {0};
    audio_stats_add(&s, zeros, 8);
    CHECK_EQ(s.count, 8);
    CHECK_EQ(s.zeros, 8);
    CHECK_EQ(s.peak, 0);
    CHECK_EQ(audio_stats_rms(&s), 0);

    /* Square wave +-1000: RMS 1000, DC 0. Added in two calls. */
    audio_stats_reset(&s);
    const int16_t sq[4] = {1000, -1000, 1000, -1000};
    audio_stats_add(&s, sq, 4);
    audio_stats_add(&s, sq, 4);
    CHECK_EQ(s.count, 8);
    CHECK_EQ(s.peak, 1000);
    CHECK_EQ(s.zeros, 0);
    CHECK_EQ(audio_stats_dc(&s), 0);
    CHECK_EQ(audio_stats_rms(&s), 1000);

    /* Constant offset: DC 500, RMS (AC part) 0. */
    audio_stats_reset(&s);
    const int16_t dc[4] = {500, 500, 500, 500};
    audio_stats_add(&s, dc, 4);
    CHECK_EQ(audio_stats_dc(&s), 500);
    CHECK_EQ(audio_stats_rms(&s), 0);

    /* Square wave around an offset: 300 +- 200 -> DC 300, RMS 200. */
    audio_stats_reset(&s);
    const int16_t off[4] = {500, 100, 500, 100};
    audio_stats_add(&s, off, 4);
    CHECK_EQ(audio_stats_dc(&s), 300);
    CHECK_EQ(audio_stats_rms(&s), 200);

    /* QC R10: a mean that isn't a whole number. The integer version truncated the mean and
     * E[x^2] before subtracting, which leaves up to ~2|DC| in the variance: 600/601 (DC
     * 600.5, RMS 0.5) read 24, and 1011/990 (DC 1000.5, RMS 10.5) read 33. */
    audio_stats_reset(&s);
    const int16_t half[4] = {600, 601, 600, 601};
    audio_stats_add(&s, half, 4);
    CHECK_EQ(audio_stats_rms(&s), 0);
    audio_stats_reset(&s);
    const int16_t neg_half[4] = {-600, -601, -600, -601};
    audio_stats_add(&s, neg_half, 4);
    CHECK_EQ(audio_stats_rms(&s), 0);
    audio_stats_reset(&s);
    const int16_t small_ac[4] = {1011, 990, 1011, 990};
    audio_stats_add(&s, small_ac, 4);
    CHECK_EQ(audio_stats_rms(&s), 10);
    audio_stats_reset(&s);
    const int16_t big_dc[4] = {-32000, -31999, -32000, -31999};
    audio_stats_add(&s, big_dc, 4);
    CHECK_EQ(audio_stats_rms(&s), 0);

    /* Full scale: peak 32768 for INT16_MIN, both ends count as clipped. */
    audio_stats_reset(&s);
    const int16_t fs[4] = {INT16_MIN, INT16_MAX, INT16_MIN, INT16_MAX};
    audio_stats_add(&s, fs, 4);
    CHECK_EQ(s.peak, 32768);
    CHECK_EQ(s.clipped, 4);
    CHECK(audio_stats_rms(&s) >= 32767 && audio_stats_rms(&s) <= 32768);
    CHECK(audio_stats_rms_dbfs(&s) > -0.1f && audio_stats_rms_dbfs(&s) <= 0.0f);

    /* dBFS: RMS 1000 is 20*log10(1000/32768) = -30.3 dB; silence is -999. */
    audio_stats_reset(&s);
    audio_stats_add(&s, sq, 4);
    const float db = audio_stats_rms_dbfs(&s);
    CHECK(db > -30.4f && db < -30.2f);
    audio_stats_reset(&s);
    audio_stats_add(&s, zeros, 8);
    CHECK(audio_stats_rms_dbfs(&s) == -999.0f);

    /* The log line, and truncation into a short buffer. */
    char line[128];
    audio_stats_reset(&s);
    audio_stats_add(&s, sq, 4);
    audio_stats_describe(&s, line, sizeof(line));
    CHECK(strcmp(line, "rms 1000 (-30.3 dBFS), peak 1000, dc 0, clipped 0, zeros 0.0%, 4 samples") == 0);
    char tiny[8];
    audio_stats_describe(&s, tiny, sizeof(tiny));
    CHECK(strlen(tiny) == 7);

    return checks_done("test_audio_stats");
}
