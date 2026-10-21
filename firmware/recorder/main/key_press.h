/* Short and long presses from a polled GPIO button, in the form click_counter.h takes: SHORT
 * when a press shorter than long_ms ends, LONG while a press is still held at long_ms (its
 * release then emits nothing). Pure C; host-tested (test/test_key_press.c). Used by the
 * T4-S3, whose only button is BOOT (GPIO0): the board feeds it the raw level every few ms
 * and passes its events to the click counter, so it gets the same clicks and hold as the
 * T-Watch S3's power-chip key.
 *
 * The level is debounced: a change counts once it has been stable for debounce_ms. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define KEY_PRESS_DEBOUNCE_MS 20
#define KEY_PRESS_LONG_MS 1000

typedef enum {
    KEY_PRESS_NONE,
    KEY_PRESS_SHORT,
    KEY_PRESS_LONG,
} key_press_event_t;

typedef struct {
    uint32_t debounce_ms;
    uint32_t long_ms;
    bool stable;       /* debounced level: true = pressed */
    bool candidate;    /* last raw level seen */
    uint32_t cand_ms;  /* how long the raw level has been `candidate` */
    uint32_t held_ms;  /* time held since the debounced press */
    bool long_sent;
} key_press_t;

/* Default timings (KEY_PRESS_*_MS), button released. */
void key_press_init(key_press_t *k);

/* Feed the raw level (`down` = pressed) and the ms since the previous call. At most one
 * event per call. */
key_press_event_t key_press_update(key_press_t *k, bool down, uint32_t elapsed_ms);

/* Released, raw and debounced: the caller may stop polling until the button goes down. */
bool key_press_idle(const key_press_t *k);

#ifdef __cplusplus
}
#endif
