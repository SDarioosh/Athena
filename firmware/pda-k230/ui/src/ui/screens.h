/* The screens (one file each) and what they share. A new tab screen defines a
 * `const ui_screen_t screen_<name>` in screen_<name>.c, declares it here and replaces its
 * stub in ui_screens.c (README.md "Adding a screen"). */
#pragma once

#include "ui.h"

extern const ui_screen_t screen_today;    /* screen_today.c */
extern const ui_screen_t screen_tasks;    /* screen_tasks.c */
extern const ui_screen_t screen_brief;    /* screen_stub.c until Brief is built */
extern const ui_screen_t screen_calendar; /* screen_stub.c until Calendar is built */
extern const ui_screen_t screen_talks;    /* screen_stub.c until Talks is built */
extern const ui_screen_t screen_status;   /* screen_stub.c until Status is built */

/* sheet_event.c: an event's details in the bottom sheet (Today, Calendar). why: the hub's
 * reason to show, or NULL. */
void sheet_event_open(int event_idx, const char *why);

/* "No feed yet" with what the sync is doing, for any screen without a feed. */
lv_obj_t *ui_no_feed(lv_obj_t *parent);
