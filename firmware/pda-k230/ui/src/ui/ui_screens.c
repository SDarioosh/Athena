/* The tab table: one screen per tab, in tab-bar order (ui_tab_t). A new screen replaces
 * its stub here and in screen_stub.c. */
#include "screens.h"
#include "ui_internal.h"

const ui_screen_t *const UI_SCREENS[UI_TABS] = {
    [UI_TAB_TODAY] = &screen_today,
    [UI_TAB_BRIEF] = &screen_brief,
    [UI_TAB_CALENDAR] = &screen_calendar,
    [UI_TAB_TASKS] = &screen_tasks,
    [UI_TAB_TALKS] = &screen_talks,
    [UI_TAB_STATUS] = &screen_status,
};
