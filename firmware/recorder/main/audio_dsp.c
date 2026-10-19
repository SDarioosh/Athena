#include "audio_dsp.h"

#include <math.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define LIMITER_CEILING_DBFS -1.0f
#define LIMITER_RELEASE_MS 100.0f

/* Biquads from the RBJ audio EQ cookbook. */
static void set_coeffs(audio_biquad_t *f, float b0, float b1, float b2, float a0, float a1,
                       float a2)
{
    f->b0 = b0 / a0;
    f->b1 = b1 / a0;
    f->b2 = b2 / a0;
    f->a1 = a1 / a0;
    f->a2 = a2 / a0;
}

static void highpass(audio_biquad_t *f, float rate_hz, float hz)
{
    const float w0 = 2.0f * (float)M_PI * hz / rate_hz;
    const float cw = cosf(w0);
    const float alpha = sinf(w0) / (2.0f * 0.70710678f);
    set_coeffs(f, (1.0f + cw) / 2.0f, -(1.0f + cw), (1.0f + cw) / 2.0f, 1.0f + alpha, -2.0f * cw,
               1.0f - alpha);
}

static void peaking(audio_biquad_t *f, float rate_hz, const audio_dsp_band_t *band)
{
    const float a = powf(10.0f, band->gain_db / 40.0f);
    const float w0 = 2.0f * (float)M_PI * band->hz / rate_hz;
    const float cw = cosf(w0);
    const float alpha = sinf(w0) / (2.0f * band->q);
    set_coeffs(f, 1.0f + alpha * a, -2.0f * cw, 1.0f - alpha * a, 1.0f + alpha / a, -2.0f * cw,
               1.0f - alpha / a);
}

/* Seed the state as if `x` had always been the input: a constant offset is then steady state.
 * The high-pass outputs 0 for it; a peaking filter (unity gain at DC) outputs `x`. */
static void prime(audio_biquad_t *f, float x, float y)
{
    f->x1 = f->x2 = x;
    f->y1 = f->y2 = y;
}

static inline float run(audio_biquad_t *f, float x)
{
    const float y = f->b0 * x + f->b1 * f->x1 + f->b2 * f->x2 - f->a1 * f->y1 - f->a2 * f->y2;
    f->x2 = f->x1;
    f->x1 = x;
    f->y2 = f->y1;
    f->y1 = y;
    return y;
}

void audio_dsp_init(audio_dsp_t *d, float rate_hz, float hp_hz, const audio_dsp_band_t *bands,
                    int band_count, int gain_db)
{
    highpass(&d->hp, rate_hz, hp_hz);
    d->eq_count = 0;
    for (int i = 0; bands && i < band_count && i < AUDIO_DSP_MAX_EQ_BANDS; i++) {
        peaking(&d->eq[d->eq_count++], rate_hz, &bands[i]);
    }
    if (gain_db < AUDIO_DSP_GAIN_MIN_DB) {
        gain_db = AUDIO_DSP_GAIN_MIN_DB;
    } else if (gain_db > AUDIO_DSP_GAIN_MAX_DB) {
        gain_db = AUDIO_DSP_GAIN_MAX_DB;
    }
    d->gain_db = gain_db;
    d->gain = powf(10.0f, (float)gain_db / 20.0f);
    d->ceiling = 32767.0f * powf(10.0f, LIMITER_CEILING_DBFS / 20.0f);
    d->release = expf(-1.0f / (rate_hz * LIMITER_RELEASE_MS / 1000.0f));
    audio_dsp_reset(d);
}

void audio_dsp_reset(audio_dsp_t *d)
{
    prime(&d->hp, 0.0f, 0.0f);
    for (int i = 0; i < d->eq_count; i++) {
        prime(&d->eq[i], 0.0f, 0.0f);
    }
    d->env = 1.0f;
    d->primed = false;
    d->limited = 0;
    d->clipped = 0;
}

void audio_dsp_process(audio_dsp_t *d, int16_t *samples, size_t n)
{
    if (!d->primed && n > 0) {
        /* Start as if the first sample had always been there: the mic's DC offset is then a
         * steady state (output 0) instead of a step the filter turns into a thump. */
        prime(&d->hp, samples[0], 0.0f);
        d->primed = true;
    }
    for (size_t i = 0; i < n; i++) {
        float y = run(&d->hp, samples[i]);
        for (int b = 0; b < d->eq_count; b++) {
            y = run(&d->eq[b], y);
        }
        y *= d->gain;
        /* Limiter: turn down at once when a peak would pass the ceiling, then recover. */
        const float mag = fabsf(y);
        const float need = mag > d->ceiling ? d->ceiling / mag : 1.0f;
        d->env = need < d->env ? need : 1.0f - (1.0f - d->env) * d->release;
        const float g = d->env < need ? d->env : need;
        if (g < 0.999f) {
            d->limited++;
        }
        const float out = y * g;
        if (out >= 32767.0f) {
            samples[i] = INT16_MAX;
            d->clipped++;
        } else if (out <= -32768.0f) {
            samples[i] = INT16_MIN;
            d->clipped++;
        } else {
            samples[i] = (int16_t)lrintf(out);
        }
    }
}
