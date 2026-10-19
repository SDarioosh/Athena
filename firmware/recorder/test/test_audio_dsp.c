#include "audio_dsp.h"
#include "audio_stats.h"
#include "check.h"

#include <math.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define RATE 16000
#define N 16000 /* 1 s */

static int16_t buf[N];

static void sine(float hz, float amp, int16_t dc)
{
    for (int i = 0; i < N; i++) {
        buf[i] = (int16_t)lrintf(dc + amp * sinf(2.0f * (float)M_PI * hz * i / RATE));
    }
}

/* RMS of samples [from, to), after the filters have settled. */
static uint32_t rms_of(int from, int to)
{
    audio_stats_t s;
    audio_stats_reset(&s);
    audio_stats_add(&s, buf + from, (size_t)(to - from));
    return audio_stats_rms(&s);
}

static int32_t dc_of(int from, int to)
{
    audio_stats_t s;
    audio_stats_reset(&s);
    audio_stats_add(&s, buf + from, (size_t)(to - from));
    return audio_stats_dc(&s);
}

static int16_t peak_of(int from, int to)
{
    int16_t p = 0;
    for (int i = from; i < to; i++) {
        const int16_t a = buf[i] < 0 ? (int16_t)(buf[i] == INT16_MIN ? INT16_MAX : -buf[i]) : buf[i];
        p = a > p ? a : p;
    }
    return p;
}

int main(void)
{
    audio_dsp_t d;
    const audio_dsp_band_t watch_eq[] = {{1600.0f, 9.0f, 1.0f}, {3600.0f, -6.0f, 2.0f}};

    /* High-pass only, 0 dB: a 1 kHz tone passes unchanged (RMS of amplitude 1000 = 707) and
     * the DC offset goes. */
    audio_dsp_init(&d, RATE, 100.0f, NULL, 0, 0);
    sine(1000.0f, 1000.0f, 600);
    audio_dsp_process(&d, buf, N);
    CHECK(rms_of(N / 2, N) >= 700 && rms_of(N / 2, N) <= 714);
    CHECK(dc_of(N / 2, N) >= -2 && dc_of(N / 2, N) <= 2);
    CHECK_EQ(d.limited, 0);

    /* 30 Hz rumble is cut by more than 20 dB (2nd order: ~21 dB at 30/100). */
    audio_dsp_init(&d, RATE, 100.0f, NULL, 0, 0);
    sine(30.0f, 1000.0f, 0);
    audio_dsp_process(&d, buf, N);
    CHECK(rms_of(N / 2, N) < 707 / 10);

    /* At the corner a Butterworth high-pass is -3 dB (0.707 x): RMS ~500. */
    audio_dsp_init(&d, RATE, 100.0f, NULL, 0, 0);
    sine(100.0f, 1000.0f, 0);
    audio_dsp_process(&d, buf, N);
    CHECK(rms_of(N / 2, N) >= 480 && rms_of(N / 2, N) <= 520);

    /* +24 dB = x15.85: 1 kHz amplitude 100 -> RMS ~1121. Processing in chunks gives the same
     * result as in one go (state carries over). */
    audio_dsp_init(&d, RATE, 100.0f, NULL, 0, 24);
    CHECK_EQ(d.gain_db, 24);
    sine(1000.0f, 100.0f, 600);
    for (int off = 0; off < N; off += 1000) {
        audio_dsp_process(&d, buf + off, 1000);
    }
    CHECK(rms_of(N / 2, N) >= 1100 && rms_of(N / 2, N) <= 1140);
    CHECK_EQ(d.limited, 0);

    /* EQ: ~+8.7 dB at 1.6 kHz (RMS ~1924) and ~-4.4 dB at 3.6 kHz (RMS ~427): the two bands
     * overlap a little. 300 Hz, well below both, is almost untouched. */
    audio_dsp_init(&d, RATE, 100.0f, watch_eq, 2, 0);
    sine(1600.0f, 1000.0f, 0);
    audio_dsp_process(&d, buf, N);
    CHECK(rms_of(N / 2, N) >= 1850 && rms_of(N / 2, N) <= 2000);
    audio_dsp_init(&d, RATE, 100.0f, watch_eq, 2, 0);
    sine(3600.0f, 1000.0f, 0);
    audio_dsp_process(&d, buf, N);
    CHECK(rms_of(N / 2, N) >= 400 && rms_of(N / 2, N) <= 460);
    audio_dsp_init(&d, RATE, 100.0f, watch_eq, 2, 0);
    sine(300.0f, 1000.0f, 0);
    audio_dsp_process(&d, buf, N);
    CHECK(rms_of(N / 2, N) >= 700 && rms_of(N / 2, N) <= 800);

    /* Limiter: a tone 30 dB too loud is held at the -1 dBFS ceiling (~29204), never clipped. */
    audio_dsp_init(&d, RATE, 100.0f, NULL, 0, 30);
    sine(1000.0f, 5000.0f, 0);
    audio_dsp_process(&d, buf, N);
    CHECK(d.limited > 0);
    CHECK_EQ(d.clipped, 0);
    CHECK(peak_of(N / 2, N) <= 29205 && peak_of(N / 2, N) >= 28000);

    /* ...and recovers: after a 0.25 s loud burst, a quiet tone is back to full gain within
     * 0.5 s (100 ms release). 30 dB = x31.6: amplitude 100 -> RMS ~2236. */
    audio_dsp_init(&d, RATE, 100.0f, NULL, 0, 30);
    for (int i = 0; i < N; i++) {
        const float amp = i < N / 4 ? 5000.0f : 100.0f;
        buf[i] = (int16_t)lrintf(amp * sinf(2.0f * (float)M_PI * 1000.0f * i / RATE));
    }
    audio_dsp_process(&d, buf, N);
    CHECK(rms_of(3 * N / 4, N) >= 2180 && rms_of(3 * N / 4, N) <= 2290);

    /* A constant DC offset at the start produces no thump, even with EQ and 30 dB gain. */
    audio_dsp_init(&d, RATE, 100.0f, watch_eq, 2, 30);
    for (int i = 0; i < N; i++) {
        buf[i] = 600;
    }
    audio_dsp_process(&d, buf, N);
    CHECK(peak_of(0, N) <= 2);

    /* Gain is clamped; more bands than fit are ignored; reset clears the counters. */
    audio_dsp_init(&d, RATE, 100.0f, NULL, 0, 99);
    CHECK_EQ(d.gain_db, AUDIO_DSP_GAIN_MAX_DB);
    audio_dsp_init(&d, RATE, 100.0f, NULL, 0, -5);
    CHECK_EQ(d.gain_db, AUDIO_DSP_GAIN_MIN_DB);
    const audio_dsp_band_t many[6] = {{500, 1, 1}, {600, 1, 1}, {700, 1, 1},
                                      {800, 1, 1}, {900, 1, 1}, {1000, 1, 1}};
    audio_dsp_init(&d, RATE, 100.0f, many, 6, 0);
    CHECK_EQ(d.eq_count, AUDIO_DSP_MAX_EQ_BANDS);
    d.limited = 7;
    d.clipped = 3;
    audio_dsp_reset(&d);
    CHECK_EQ(d.limited, 0);
    CHECK_EQ(d.clipped, 0);

    return checks_done("test_audio_dsp");
}
