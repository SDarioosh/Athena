/* The Calendar tab's agenda (T5 plan §5 "Calendar", the T5's build_cal_rows in ui_pda.c):
 * the feed's events from today to +7 days and the queued cal_adds (pending, shown until
 * the next feed has them), grouped by local day. Within a day: all-day first, then by
 * start time; an event before a pending add at the same time. Events that are over are
 * left out; one that started before today and is still on shows under today. No LVGL;
 * host-tested (test/test_agenda.c). */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "app_state.h"
#include "feed.h"

#ifdef __cplusplus
extern "C" {
#endif

#define AGENDA_DAYS 8          /* today .. +7: what the feed's calendar covers */
#define AGENDA_PENDING_DAYS 31 /* a queued add further out still shows */
#define AGENDA_MAX (2 * (FEED_MAX_EVENTS + OUTBOX_MAX) + 8)

typedef enum {
    AGENDA_DAY,     /* a day heading */
    AGENDA_EVENT,   /* idx: the feed event */
    AGENDA_PENDING, /* idx: the index in the pending list given */
} agenda_kind_t;

typedef struct {
    agenda_kind_t kind;
    int32_t day;   /* local day number */
    int idx;
    bool all_day;
    int64_t start; /* epoch s (all-day: midnight UTC of the date) */
    int64_t end;   /* epoch s; = start when the event has no end (pending: its end) */
    bool now;      /* a timed event that is on at `now` */
} agenda_row_t;

/* Fill rows (at most max) and return how many. f may be NULL (pending adds only); pend
 * may hold any actions, only cal_adds are used. */
int agenda_build(const feed_t *f, const app_pending_t *pend, int n_pend, int32_t today, int64_t now,
                 int tz_min, agenda_row_t *rows, int max);

/* "Today", "Tomorrow", else "Thu 22 Oct". */
void agenda_day_name(int32_t day, int32_t today, char *out, size_t cap);

/* The rows' events (event or pending, no headings). */
int agenda_count_items(const agenda_row_t *rows, int n);

#ifdef __cplusplus
}
#endif
