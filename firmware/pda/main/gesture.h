/* Touch gesture recognition: tap, long press, swipe (docs/t5-pda-plan.md §5). Pure C;
 * host-tested (test/test_gesture.c). Feed it one sample per touch poll; it reports at most
 * one gesture per finger-down. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define GESTURE_LONG_MS 700   /* held this long without moving: long press (fired while held) */
#define GESTURE_TAP_MAX_MS 600
#define GESTURE_MOVE_PX 30    /* more movement than this is not a tap */
#define GESTURE_SWIPE_PX 120  /* at least this far along one axis: a swipe */

typedef enum {
    GESTURE_NONE,
    GESTURE_TAP,
    GESTURE_LONG,
    GESTURE_SWIPE_LEFT,
    GESTURE_SWIPE_RIGHT,
    GESTURE_SWIPE_UP,
    GESTURE_SWIPE_DOWN,
} gesture_kind_t;

typedef struct {
    gesture_kind_t kind;
    int x, y; /* where the finger went down */
} gesture_t;

typedef struct {
    bool down;
    bool reported; /* a long press already fired for this touch */
    int x0, y0, x, y;
    int64_t t0;
} gesture_state_t;

void gesture_reset(gesture_state_t *g);

/* One sample: touching (with x, y) or not, at now_ms. Returns the gesture finished by this
 * sample, or GESTURE_NONE. */
gesture_t gesture_feed(gesture_state_t *g, bool touching, int x, int y, int64_t now_ms);

const char *gesture_name(gesture_kind_t k);

#ifdef __cplusplus
}
#endif
