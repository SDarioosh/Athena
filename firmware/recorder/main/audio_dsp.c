#include "audio_dsp.h"

#include <math.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

void audio_dsp_init(audio_dsp_t *d, float rate_hz, float hp_hz, int gain_db)
{
    /* RBJ audio EQ cookbook high-pass. */
    const float w0 = 2.0f * (float)M_PI * hp_hz / rate_hz;
    const float cw = cosf(w0);
    const float alpha = sinf(w0) / (2.0f * 0.70710678f);
    const float a0 = 1.0f + alpha;
    d->b0 = (1.0f + cw) / 2.0f / a0;
    d->b1 = -(1.0f + cw) / a0;
    d->b2 = d->b0;
    d->a1 = -2.0f * cw / a0;
    d->a2 = (1.0f - alpha) / a0;
    if (gain_db < AUDIO_DSP_GAIN_MIN_DB) {
        gain_db = AUDIO_DSP_GAIN_MIN_DB;
    } else if (gain_db > AUDIO_DSP_GAIN_MAX_DB) {
        gain_db = AUDIO_DSP_GAIN_MAX_DB;
    }
    d->gain_db = gain_db;
    d->gain = powf(10.0f, (float)gain_db / 20.0f);
    audio_dsp_reset(d);
}

void audio_dsp_reset(audio_dsp_t *d)
{
    d->x1 = d->x2 = d->y1 = d->y2 = 0.0f;
    d->primed = false;
    d->clipped = 0;
}

void audio_dsp_process(audio_dsp_t *d, int16_t *samples, size_t n)
{
    if (!d->primed && n > 0) {
        /* Start as if the first sample had always been there: the mic's DC offset is then a
         * steady state (output 0) instead of a step the filter turns into a thump. */
        d->x1 = d->x2 = samples[0];
        d->y1 = d->y2 = 0.0f;
        d->primed = true;
    }
    float x1 = d->x1, x2 = d->x2, y1 = d->y1, y2 = d->y2;
    for (size_t i = 0; i < n; i++) {
        const float x = samples[i];
        const float y = d->b0 * x + d->b1 * x1 + d->b2 * x2 - d->a1 * y1 - d->a2 * y2;
        x2 = x1;
        x1 = x;
        y2 = y1;
        y1 = y;
        const float out = y * d->gain;
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
    d->x1 = x1;
    d->x2 = x2;
    d->y1 = y1;
    d->y2 = y2;
}
