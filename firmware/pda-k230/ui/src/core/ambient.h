/* The ambient screen (docs/k230-pda-plan.md "Screens": in place of the T5's Asleep
 * screen), without LVGL. Host-tested (test/test_ambient.c).
 *
 *   AWAKE --no touch or gesture for dim_after_s, fist, presence left--> AMBIENT
 *   AMBIENT --off_after_s--> OFF (backlight 0)
 *   AMBIENT / OFF --touch, palm, presence appeared--> AWAKE
 *
 * While presence wake is on and someone is in front of the camera, AWAKE doesn't time out
 * (the plan: dim after they leave). A timeout of 0 is "never". The ambient picture moves a
 * little every minute (amb_shift) so no pixel stays lit in the same place for long. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "feed.h"

#ifdef __cplusplus
extern "C" {
#endif

#define AMB_SHIFT_PX 18 /* the largest move from the centre, each way */

typedef enum {
    AMB_AWAKE = 0,
    AMB_AMBIENT,
    AMB_OFF,
} amb_stage_t;

typedef struct {
    amb_stage_t stage;
    int64_t since_ms; /* when this stage began */
} amb_t;

typedef struct {
    int dim_after_s; /* AWAKE -> AMBIENT without activity; 0 never */
    int off_after_s; /* AMBIENT -> OFF; 0 never */
    bool hold;       /* someone is present and presence wake is on: AWAKE stays */
} amb_cfg_t;

void amb_reset(amb_t *a, int64_t now_ms);
/* true if the stage changed */
bool amb_wake(amb_t *a, int64_t now_ms);
bool amb_dim(amb_t *a, int64_t now_ms); /* AWAKE -> AMBIENT now (fist, presence left) */
/* Time passing. last_activity_ms: the last touch or hands-free gesture. */
bool amb_tick(amb_t *a, int64_t now_ms, int64_t last_activity_ms, const amb_cfg_t *cfg);

/* The picture's offset from the centre for a minute number (epoch minutes): a slow walk
 * over a 5 x 5 grid within +-AMB_SHIFT_PX that visits every point once per 25 minutes. */
void amb_shift(int64_t minute, int32_t *dx, int32_t *dy);

/* The next event that hasn't ended (T5 Asleep screen): timed events whose end (or start, if
 * none) is after now; all-day events of today or later. Sorted events: the first such.
 * -1 if none. */
int amb_next_event(const feed_t *f, int64_t now, int tz_min);
/* "Tuesday 20 October" in local time. */
void amb_fmt_date_long(int64_t epoch, int tz_min, char *out, size_t cap);

/* Its full date and time: "Tue 20 Oct \xc2\xb7 12:30 – 13:30", "Fri 23 Oct \xc2\xb7 all day". */
void amb_fmt_when(const feed_event_t *e, int tz_min, char *out, size_t cap);

#ifdef __cplusplus
}
#endif
