/* Milestone-1 test recorder: N seconds from the mic straight to a WAV file on the card.
 * Later milestones replace this with the speech gate, Opus encoder and queue. */
#pragma once

#include <stdbool.h>

#include "esp_err.h"

typedef struct {
    bool running;
    int elapsed_s;
    int total_s;
    char path[64];     /* last or current file */
    esp_err_t last_err; /* result of the last recording */
} recorder_state_t;

/* Called from the recorder task when the file is closed (or on failure). */
typedef void (*recorder_done_cb_t)(esp_err_t result, void *ctx);
/* Called once per second of audio written. */
typedef void (*recorder_tick_cb_t)(int elapsed_s, void *ctx);

esp_err_t recorder_start(int seconds, recorder_done_cb_t done, recorder_tick_cb_t tick, void *ctx);
void recorder_get_state(recorder_state_t *out);
