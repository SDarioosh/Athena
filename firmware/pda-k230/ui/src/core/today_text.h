/* The Today tab's words for one item (T5 plan §5 "Today as built"): an item's second line
 * (the reason, the hub's `why`, the list, the place or "until 16:00", or nothing when it
 * would only repeat the title) and an event's time column. The rules of the T5's
 * ui_today.c (second_line, restates, repeats), which can't be linked here because it draws
 * with epdiy; kept identical so both PDAs show the same words. Pure C; host-tested
 * (test/test_today_text.c). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "feed.h"
#include "today_plan.h"

#ifdef __cplusplus
extern "C" {
#endif

#define TODAY_LINE_MAX 400 /* the hub's why is up to 80 characters of UTF-8, plus a place */

/* failed: the relay couldn't apply the last toggle of this to-do (its line then says so). */
void today_second_line(const feed_t *f, const today_item_t *it, bool failed, int tz_min, char *out,
                       size_t cap);

/* An event's left column: "09:30" or "all day"; *weekday "Wed" when it isn't today, else "". */
void today_event_column(const feed_event_t *e, int32_t today, int tz_min, char *hm, size_t hm_cap,
                        char *weekday, size_t wd_cap);

/* The hub's reason only restates the title (exported for the tests). */
bool today_restates(const char *why, const char *title);
/* A line that only repeats the first one, or the first one cut short. */
bool today_repeats(const char *line, const char *first);

#ifdef __cplusplus
}
#endif
