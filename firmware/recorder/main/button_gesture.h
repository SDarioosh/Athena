/* Button gestures from a polled level: single press, double press, long press. Pure C;
 * host-tested (test/test_button_gesture.c). The board's button task feeds it the raw level
 * every few ms and acts on what comes back.
 *
 * - The level is debounced: a change counts once it has been stable for debounce_ms.
 * - LONG fires while the button is still held, long_ms after the (debounced) press; the
 *   release that follows emits nothing.
 * - DOUBLE fires on the second press when it comes within double_ms of the first release;
 *   that second press emits nothing else (holding it does not make a LONG).
 * - SINGLE fires double_ms after the release of a short press when no second press came. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define BUTTON_GESTURE_DEBOUNCE_MS 30
#define BUTTON_GESTURE_LONG_MS 1000
#define BUTTON_GESTURE_DOUBLE_MS 300

typedef enum {
    BUTTON_GESTURE_NONE,
    BUTTON_GESTURE_SINGLE,
    BUTTON_GESTURE_DOUBLE,
    BUTTON_GESTURE_LONG,
} button_gesture_event_t;

typedef struct {
    /* timings, set by button_gesture_init() */
    uint32_t debounce_ms;
    uint32_t long_ms;
    uint32_t double_ms;
    /* state */
    bool stable;        /* debounced level: true = pressed */
    bool candidate;     /* last raw level seen */
    uint32_t cand_ms;   /* how long the raw level has been `candidate` */
    int phase;          /* internal */
    uint32_t phase_ms;  /* time in the current phase */
} button_gesture_t;

/* Default timings (the BUTTON_GESTURE_*_MS values), button released. */
void button_gesture_init(button_gesture_t *g);

/* Feed the raw level (`down` = pressed) and the ms elapsed since the previous call. Returns
 * at most one event per call. */
button_gesture_event_t button_gesture_update(button_gesture_t *g, bool down, uint32_t elapsed_ms);

const char *button_gesture_name(button_gesture_event_t ev);

#ifdef __cplusplus
}
#endif
