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

/* RMS of the last half second, after the filter has settled. */
static uint32_t settled_rms(void)
{
    audio_stats_t s;
    audio_stats_reset(&s);
    audio_stats_add(&s, buf + N / 2, N / 2);
    return audio_stats_rms(&s);
}

static int32_t settled_dc(void)
{
    audio_stats_t s;
    audio_stats_reset(&s);
    audio_stats_add(&s, buf + N / 2, N / 2);
    return audio_stats_dc(&s);
}

int main(void)
{
    audio_dsp_t d;

    /* 0 dB: a 1 kHz tone passes unchanged (RMS of amplitude 1000 = 707), the DC offset goes. */
    audio_dsp_init(&d, RATE, 100.0f, 0);
    sine(1000.0f, 1000.0f, 600);
    audio_dsp_process(&d, buf, N);
    CHECK(settled_rms() >= 700 && settled_rms() <= 714);
    CHECK(settled_dc() >= -2 && settled_dc() <= 2);
    CHECK_EQ(d.clipped, 0);

    /* 30 Hz rumble is cut by more than 20 dB (2nd order: ~21 dB at 30/100). */
    audio_dsp_init(&d, RATE, 100.0f, 0);
    sine(30.0f, 1000.0f, 0);
    audio_dsp_process(&d, buf, N);
    CHECK(settled_rms() < 707 / 10);

    /* At the corner a Butterworth high-pass is -3 dB (0.707 x): RMS ~500. */
    audio_dsp_init(&d, RATE, 100.0f, 0);
    sine(100.0f, 1000.0f, 0);
    audio_dsp_process(&d, buf, N);
    CHECK(settled_rms() >= 480 && settled_rms() <= 520);

    /* +24 dB = x15.85: 1 kHz amplitude 100 -> RMS ~1121. Processing in chunks gives the same
     * result as in one go (state carries over). */
    audio_dsp_init(&d, RATE, 100.0f, 24);
    CHECK_EQ(d.gain_db, 24);
    sine(1000.0f, 100.0f, 600);
    for (int off = 0; off < N; off += 1000) {
        audio_dsp_process(&d, buf + off, 1000);
    }
    CHECK(settled_rms() >= 1100 && settled_rms() <= 1140);
    CHECK_EQ(d.clipped, 0);

    /* Saturation: a loud tone with 30 dB gain clips to full scale instead of wrapping. */
    audio_dsp_init(&d, RATE, 100.0f, 30);
    sine(1000.0f, 5000.0f, 0);
    audio_dsp_process(&d, buf, N);
    CHECK(d.clipped > 0);
    int16_t lo = 0, hi = 0;
    for (int i = N / 2; i < N; i++) {
        lo = buf[i] < lo ? buf[i] : lo;
        hi = buf[i] > hi ? buf[i] : hi;
    }
    CHECK_EQ(hi, INT16_MAX);
    CHECK_EQ(lo, INT16_MIN);

    /* A constant DC offset at the start produces no thump, even at 30 dB gain. */
    audio_dsp_init(&d, RATE, 100.0f, 30);
    for (int i = 0; i < N; i++) {
        buf[i] = 600;
    }
    audio_dsp_process(&d, buf, N);
    int16_t worst = 0;
    for (int i = 0; i < N; i++) {
        const int16_t a = buf[i] < 0 ? (int16_t)-buf[i] : buf[i];
        worst = a > worst ? a : worst;
    }
    CHECK(worst <= 2);

    /* Gain is clamped; reset clears the clip count. */
    audio_dsp_init(&d, RATE, 100.0f, 99);
    CHECK_EQ(d.gain_db, AUDIO_DSP_GAIN_MAX_DB);
    audio_dsp_init(&d, RATE, 100.0f, -5);
    CHECK_EQ(d.gain_db, AUDIO_DSP_GAIN_MIN_DB);
    d.clipped = 7;
    audio_dsp_reset(&d);
    CHECK_EQ(d.clipped, 0);

    return checks_done("test_audio_dsp");
}
