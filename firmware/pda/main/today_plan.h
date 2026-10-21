/* The Today tab's plan (docs/t5-pda-plan.md §5): three sections, DO FIRST, COMING UP and
 * ALSO WORTH A LOOK, each a short list of to-do items and events with a reason. It comes
 * from the hub's "Today insight" (feed `insight`, §4.1) when the feed has one for today,
 * and from fixed rules over the feed's `todos` and `calendar` when it hasn't. Both fill the
 * same struct, so the screen has one way to draw it. Pure C; host-tested
 * (test/test_today_plan.c).
 *
 * Rules (no insight for today):
 * - DO FIRST: open items of overdue checklists (oldest due date first), then of the ones
 *   due today, then today's events that aren't over (all-day first, then by time);
 * - COMING UP: events and items of checklists due on the next 7 days, by date and time;
 * - ALSO WORTH A LOOK: items of pinned checklists not listed above, then of undated ones;
 * - done items and events that are over are left out, and so are shopping lists (a title
 *   of Shopping, Groceries or Grocery, with or without " list", any case) without a due
 *   date; at most TODAY_PLAN_CAP items per section, the rest counted in `more`.
 *
 * With the insight: its sections in the hub's order, without items that are done, events
 * that are over and repeats. Today's timed events that are still to come all go to the end
 * of DO FIRST in time order (with the hub's reason when it named them), wherever the hub
 * put them. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "feed.h"

#ifdef __cplusplus
extern "C" {
#endif

#define TODAY_PLAN_CAP 5    /* rule-based items per section */
#define TODAY_PLAN_MAX 8    /* room per section: the insight's 5 plus today's events */
#define TODAY_REASON_MAX 40

typedef enum {
    TODAY_DO_FIRST = 0,
    TODAY_COMING_UP,
    TODAY_ALSO,
    TODAY_SECTIONS,
} today_section_id_t;

typedef struct {
    feed_insight_item_t ref;        /* what it is (todo item or event) and the hub's `why` */
    char reason[TODAY_REASON_MAX];  /* rule-made: "overdue since 2026-10-19", "due today",
                                       "today 14:00", "Fri 23 Oct 14:00", "pinned", "no date";
                                       with the insight: an event's time, else "" */
} today_item_t;

typedef struct {
    today_item_t items[TODAY_PLAN_MAX];
    int n;
    int more; /* left out by the cap */
} today_section_t;

typedef struct {
    bool from_insight;
    const char *headline; /* the insight's; NULL without one */
    today_section_t sec[TODAY_SECTIONS];
} today_plan_t;

/* today: the local day number (pda_local_day(now, tz_min)); now: epoch seconds; tz_min:
 * the display offset, for events' local day and time. */
void today_plan_build(const feed_t *f, int32_t today, int64_t now, int tz_min, today_plan_t *out);

/* From f->insight if the feed has one dated today; false (out untouched) if not. */
bool today_plan_from_insight(const feed_t *f, int32_t today, int64_t now, int tz_min,
                             today_plan_t *out);

/* The insight if there is one for today, else the rules. */
void today_plan_for_today(const feed_t *f, int32_t today, int64_t now, int tz_min, today_plan_t *out);

/* "Shopping", "groceries", "Grocery List", ... (trimmed, any case). */
bool today_is_shopping_list(const char *title);

/* An event's time as the plan shows it: "today 14:00", "today, all day",
 * "Wed 21 Oct 09:00", "Sat 25 Oct". */
void today_event_when(const feed_event_t *e, int32_t today, int tz_min, char *out, size_t cap);

/* The local day of an event (all-day: its date). */
int32_t today_event_day(const feed_event_t *e, int tz_min);

/* Over: all-day before today, timed with its end (or start, without an end) passed. */
bool today_event_over(const feed_event_t *e, int32_t today, int64_t now);

#ifdef __cplusplus
}
#endif
