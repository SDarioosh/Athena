/* Low-battery guard (pure, host-tested): decides from fuel-gauge readings when the battery
 * is too low to record and when it has recovered. The limits come from the board
 * (board_battery_limits()); the app stops recording and refuses new sessions while low. */
#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int low_percent;    /* at or below this: low */
    int low_mv;         /* below this: low, whatever the gauge says */
    int resume_percent; /* low ends at or above this (and at or above low_mv), or on USB */
} bg_limits_t;

typedef struct {
    bool present; /* a battery is connected */
    int millivolts;
    int percent;  /* -1 if unknown */
    bool vbus;    /* USB power: never low */
} bg_reading_t;

typedef enum {
    BG_NONE,
    BG_ENTER_LOW,
    BG_LEAVE_LOW,
} bg_event_t;

/* Readings in a row below the limit before the guard says low (one bad read never stops a
 * recording). */
#define BG_LOW_READINGS 2
/* Without a gauge percent, low ends this far above low_mv. */
#define BG_MV_HYSTERESIS 200

typedef struct {
    bg_limits_t lim;
    bool low;
    int below; /* consecutive low readings while not low */
} battery_guard_t;

void battery_guard_init(battery_guard_t *g, const bg_limits_t *lim);
/* Feed one reading; returns the transition it caused, if any. */
bg_event_t battery_guard_update(battery_guard_t *g, const bg_reading_t *r);

#ifdef __cplusplus
}
#endif
