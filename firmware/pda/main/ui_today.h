/* The Today tab's content (docs/t5-pda-plan.md §5): INSIGHT, DO FIRST, COMING UP and ALSO
 * WORTH A LOOK from a today_plan_t ("All caught up!" when there is nothing), and the
 * "Digest ready on Brief" line. ui_pda.c draws
 * the header, the pager and the tabs around it and turns the hit regions into actions.
 * Needs only gfx and the fonts, so the host tests can render it (test/test_ui_today.c). */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "epdiy.h"
#include "feed.h"
#include "today_plan.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    UI_TODAY_HIT_TODO = 1, /* arg: note * 64 + item (array indexes): todo_toggle */
    UI_TODAY_HIT_EVENT,    /* arg: event index: the event's detail */
    UI_TODAY_HIT_BRIEF,    /* the digest line */
    UI_TODAY_HIT_TASKS,    /* "+n more" when most of the rest are to-dos */
    UI_TODAY_HIT_CALENDAR, /* "+n more" when most of the rest are events */
} ui_today_hit_t;

/* A to-do item as the T5 shows it right now: a queued todo_toggle wins over the feed. */
typedef struct {
    bool done;
    bool failed; /* the relay couldn't apply the last toggle */
} ui_today_todo_t;

typedef struct {
    const feed_t *f;
    const today_plan_t *plan;
    int32_t today; /* local day (pda_local_day) and time zone, for the events' time column */
    int tz_min;
    void (*todo_state)(void *ctx, int note, int item, ui_today_todo_t *out); /* NULL: the feed's */
    void (*hit)(void *ctx, EpdRect r, ui_today_hit_t kind, int32_t arg);
    void *ctx;
} ui_today_in_t;

/* Lays the content out over pages (between TOP and LIST_BOTTOM; the digest line in the
 * bottom row, left of the pager), clamps *page and draws that page. Returns the page count. */
int ui_today_draw(const ui_today_in_t *in, int *page);

#ifdef __cplusplus
}
#endif
