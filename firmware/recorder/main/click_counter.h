/* Click counting for a button that reports only finished presses: a power-chip key that
 * sends SHORT when a short press ends and LONG while it is held (the T-Watch S3's only
 * button, the AXP2101 PWR key). Pure C; host-tested (test/test_click_counter.c). The board's
 * button task feeds it the key events with a millisecond clock and acts on what comes back.
 *
 * - Clicks (SHORT events) less than gap_ms apart add up. gap_ms after the last one the
 *   count is emitted as CLICK_ACTION_CLICKS.
 * - LONG drops any pending clicks and emits CLICK_ACTION_HOLD at once (a click, then a hold,
 *   is a hold).
 * - After an emitted action, clicks are ignored for guard_ms, and every ignored click starts
 *   the guard again: the buzz that confirms an action can shake the key into extra presses
 *   (seen on the T-Watch Ultra's PWR key 0.5-1 s after a start buzz). A LONG is never
 *   ignored. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CLICK_COUNTER_GAP_MS 500
#define CLICK_COUNTER_GUARD_MS 1000

typedef enum {
    CLICK_ACTION_NONE,
    CLICK_ACTION_CLICKS,  /* *clicks says how many */
    CLICK_ACTION_HOLD,
    CLICK_ACTION_IGNORED, /* a click inside the guard time, dropped */
} click_action_t;

typedef struct {
    /* timings, set by click_counter_init() */
    uint32_t gap_ms;
    uint32_t guard_ms;
    /* state */
    int pending;          /* clicks counted so far */
    uint32_t last_ms;     /* time of the last counted click */
    bool guard_on;
    uint32_t guard_from_ms;
} click_counter_t;

/* Default timings (the CLICK_COUNTER_*_MS values), nothing pending. */
void click_counter_init(click_counter_t *c);

/* A SHORT event at now_ms. Returns CLICKS if the previous count was due (its gap had already
 * passed; this click then falls in that action's guard), IGNORED if the click was dropped,
 * else NONE. */
click_action_t click_counter_short(click_counter_t *c, uint32_t now_ms, int *clicks);

/* A LONG event at now_ms: always HOLD. */
click_action_t click_counter_long(click_counter_t *c, uint32_t now_ms);

/* Call when click_counter_wait_ms() has passed (or any time): CLICKS once the gap after the
 * last click has passed, else NONE. */
click_action_t click_counter_poll(click_counter_t *c, uint32_t now_ms, int *clicks);

/* ms until click_counter_poll() can emit something; UINT32_MAX when nothing is pending. */
uint32_t click_counter_wait_ms(const click_counter_t *c, uint32_t now_ms);

#ifdef __cplusplus
}
#endif
