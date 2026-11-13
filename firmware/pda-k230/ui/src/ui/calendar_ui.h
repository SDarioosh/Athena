/* Between the Calendar tab (screen_calendar.c) and its new-event flow (event_new.c). */
#pragma once

#include <stdint.h>

/* The new-event flow: when (day, time, duration), then the title; "Add" queues a cal_add
 * and returns to the Calendar. */
void event_new_open(void);
/* The same, filled in (T5 "→ calendar" from a suggested action item): the title, the day
 * (FEED_NO_DAY or a past day: today) and the action item's id (-1 none) as from_item. */
void event_new_open_for(const char *title, int32_t day, int64_t from_item);

/* Calendar: scroll to the queued cal_add action_id and flash it (after "Add"). */
void calendar_show_added(uint32_t action_id);
