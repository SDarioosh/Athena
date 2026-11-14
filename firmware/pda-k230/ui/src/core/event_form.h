/* The new-event form (T5 plan §5 "Adding an event", the T5's start_new_event / A_ADD in
 * ui_pda.c) as plain state: the day (today .. +13), the time (15-minute steps), the
 * duration (15, 30, 60 min or all day) and the title, and the `cal_add` it makes. The
 * action follows the relay's strict checks (T5 plan §3.5): a title of 1 to 200
 * characters, timed: `start` and `end` ISO-8601 with the display offset and end after
 * start; all-day: `start` a date and no `end`. No LVGL; host-tested (test/test_event_form.c). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define EVENT_FORM_DAYS 14         /* the day picker: today .. +13 */
#define EVENT_TITLE_MAX_CHARS 200  /* the relay's limit (§3.5) */
#define EVENT_TITLE_BUF (EVENT_TITLE_MAX_CHARS * 4 + 1)
#define EVENT_STEP_MIN 15
#define EVENT_FIRST_SLOT 8         /* the hour grid: 08:00 .. 19:00, like the T5 */
#define EVENT_SLOTS 12

typedef struct {
    int32_t day;       /* local day number */
    int min;           /* minute of the day, a multiple of 15 */
    int dur;           /* minutes; 0 = all day */
    char title[EVENT_TITLE_BUF];
    int64_t from_item; /* an action item it comes from, -1 none */
} event_form_t;

typedef enum {
    EVENT_FORM_OK = 0,
    EVENT_FORM_NO_TITLE,  /* empty or only spaces */
    EVENT_FORM_TITLE_LONG, /* more than 200 characters */
    EVENT_FORM_BAD_DAY,   /* outside today .. +13 */
    EVENT_FORM_BAD_TIME,  /* not a 15-minute step of the day, or no duration */
} event_form_err_t;

/* T5 defaults: today, the next full hour (08:00 at the earliest, 23:00 at the latest),
 * 30 minutes, no title. */
void event_form_init(event_form_t *f, int32_t today, int64_t now, int tz_min);
/* false (unchanged) outside today .. +13 */
bool event_form_set_day(event_form_t *f, int32_t day, int32_t today);
/* HH:00; an all-day form becomes 30 minutes (T5 A_SLOT) */
void event_form_set_hour(event_form_t *f, int hour);
/* ±15 min, kept within 00:00 .. 23:45; an all-day form becomes 30 minutes */
void event_form_step(event_form_t *f, int delta_min);
void event_form_set_duration(event_form_t *f, int dur_min);
/* Copy the title, cut to 200 characters (UTF-8 code points). */
void event_form_set_title(event_form_t *f, const char *title);

event_form_err_t event_form_validate(const event_form_t *f, int32_t today);
/* What the user sees for an error: "Give it a title", ... ("" for OK). */
const char *event_form_err_text(event_form_err_t e);

/* The cal_add action into out (PDA_ACTION_BUF bytes): its length, 0 if the form isn't
 * valid. The title is trimmed and line breaks and tabs become spaces. */
size_t event_form_cal_add(const event_form_t *f, int32_t today, int tz_min, char *out);

/* "09:00 – 09:30" or "All day"; with the day: "Wed 21 Oct · 09:00 – 09:30" ("Today",
 * "Tomorrow"). */
void event_form_time_text(const event_form_t *f, char *out, size_t cap);
void event_form_when_text(const event_form_t *f, int32_t today, char *out, size_t cap);

/* Characters (UTF-8 code points) of s. */
size_t utf8_chars(const char *s);

#ifdef __cplusplus
}
#endif
