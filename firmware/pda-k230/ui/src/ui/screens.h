/* The screens (one file each) and what they share. A new tab screen defines a
 * `const ui_screen_t screen_<name>` in screen_<name>.c, declares it here and replaces its
 * stub in ui_screens.c (README.md "Adding a screen"). */
#pragma once

#include "ui.h"

extern const ui_screen_t screen_today;    /* screen_today.c */
extern const ui_screen_t screen_tasks;    /* screen_tasks.c */
extern const ui_screen_t screen_brief;    /* screen_brief.c */
extern const ui_screen_t screen_calendar; /* screen_calendar.c */
extern const ui_screen_t screen_talks;    /* screen_talks.c */
extern const ui_screen_t screen_status;   /* screen_status.c */

/* sheet_event.c: an event's details in the bottom sheet (Today, Calendar). why: the hub's
 * reason to show, or NULL. */
void sheet_event_open(int event_idx, const char *why);

/* "No feed yet" with what the sync is doing, for any screen without a feed. */
lv_obj_t *ui_no_feed(lv_obj_t *parent);

/* talk_detail.c: a conversation (by feed id) and a transcript page (by ref), pushed. */
void talk_detail_open(const char *conv_id);
void talk_transcript_open(const char *ref, const char *title);

/* status_pairing.c: the pairing screen, pushed (Status). */
void pairing_open(void);
/* screen_status.c, status_pairing.c: vision events for the gesture tester and the pairing
 * scan, before hands-free sees them (handsfree_ui.c). true: taken, hands-free ignores it. */
bool status_vision_event(const vision_event_t *ev);
bool pairing_vision_event(const vision_event_t *ev);
