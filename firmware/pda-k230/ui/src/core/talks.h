/* Talks (T5 plan §4.1 `conversations`, `actions`, §4.3 details, §5 Conversations): the
 * words and rules of the Talks tab, its conversation detail and the transcript, without
 * LVGL. Host-tested (test/test_talks.c).
 *
 * Suggested action items (feed `actions` with a `conv`) behave as on the T5 (ui_pda.c
 * on_item): a tap queues item_status "done", a long press "dismissed"; a tap on an item
 * with a change waiting takes the change back while syncd hasn't listed it, else queues
 * "open" (the reverse). The newest queued item_status is what the item shows. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "feed.h"

#ifdef __cplusplus
extern "C" {
#endif

#define TALKS_TRANSCRIPT_PAGE_S 600 /* §4.3: 10 minutes per transcript page (as the T5) */

/* "Just now", "12 min ago", "3 h ago" (under 6 h), "Today 09:30", "Yesterday 18:00",
 * "Fri 18:00" (this week), "Wed 14 Oct" (older), "Tomorrow 09:00"/"Wed 21 Oct" ahead.
 * at 0: "". tz_min: the display offset. */
void talks_rel_time(int64_t at, int64_t now, int tz_min, char *out, size_t cap);

/* The people of a conversation: "Alice", "Alice and Ben", "Alice, Ben and 2 more" (at most
 * `show` names). "" for none. */
void talks_people(const char *const *people, int n, int show, char *out, size_t cap);

/* Up to two capital initials of a name for its round avatar: "Maria" -> "M",
 * "Aunt Rosa" -> "AR", "" -> "?". UTF-8 aware (the first code point of each word). */
void talks_initials(const char *name, char *out, size_t cap);

/* The feed's suggested action items of conversation `conv_id`, in feed order (indexes
 * into f->actions). Returns the count (<= max). */
int talks_conv_actions(const feed_t *f, const char *conv_id, int *idx, int max);

/* ---- an action item's state ---- */

typedef enum {
    TALKS_ITEM_OPEN = 0,
    TALKS_ITEM_DONE,
    TALKS_ITEM_DISMISSED,
} talks_item_state_t;

typedef struct {
    talks_item_state_t state; /* the newest queued change wins over the feed (always open) */
    bool pending;             /* a change is queued */
    bool listed;              /* syncd has it: a tap now queues the reverse */
    bool failed;              /* the relay refused the last change */
} talks_item_view_t;

void talks_item_view(const feed_action_t *a, talks_item_view_t *out);

typedef enum {
    TALKS_TAP_QUEUED,  /* done / dismissed queued */
    TALKS_TAP_UNDONE,  /* the queued change was taken back */
    TALKS_TAP_REOPEN,  /* syncd had it: "open" queued */
    TALKS_TAP_FULL,    /* outbox full */
    TALKS_TAP_ERROR,
} talks_tap_t;

/* A tap (long_press: dismiss) on f->actions[idx] of the current feed. */
talks_tap_t talks_item_tap(int idx, bool long_press);

/* The second line of an action item row: the change waiting ("Done · waiting to sync, tap
 * to undo", "Dismissed · sent"), "Couldn't sync", or due date and owner ("Due Fri 23 Oct ·
 * Me", "Overdue since Wed 7 Oct"). */
void talks_item_line(const feed_action_t *a, const talks_item_view_t *v, int32_t today, char *out,
                     size_t cap);

/* ---- transcripts ---- */

/* The first page's ref of a conversation's transcript: transcript:<id>@0-600. */
size_t talks_transcript_ref(const char *conv_id, char *out, size_t cap);

/* "0–10 min", "10–20 min" from a page's from/to (seconds); "" if to <= from. */
void talks_part_label(double from, double to, char *out, size_t cap);

/* "02:16" (or "1:02:16" past an hour) of an offset in seconds. */
void talks_offset(double o, char *out, size_t cap);

#ifdef __cplusplus
}
#endif
