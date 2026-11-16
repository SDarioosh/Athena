/* Between the UI frame's own files (ui.c, chrome.c, handsfree_ui.c, ui_screens.c); screens
 * don't include this. */
#pragma once

#include "lvgl.h"
#include "ui.h"

/* ui_screens.c: the tab table */
extern const ui_screen_t *const UI_SCREENS[UI_TABS];

/* chrome.c: the status header and the tab bar */
void chrome_create(lv_obj_t *scr);
void chrome_update(unsigned what);
void chrome_set_tab(ui_tab_t tab, bool animate);

/* handsfree_ui.c: the focus ring, the hand cursor and the hold ring */
void hf_ui_create(void);
void hf_ui_settings_changed(void);
/* The page changed (tab, detail, sheet): drop the focus. */
void hf_ui_reset_focus(void);

/* ambient_ui.c: the ambient screen behind ui_screen_off() / ui_screen_on() */
void ambient_ui_init(void);
void ambient_ui_enter(void);
void ambient_ui_wake(void);
bool ambient_ui_active(void);
void ambient_ui_poke(void); /* a hands-free gesture: activity, like a touch */

/* ui.c: the hooks main.c set (never NULL; members may be) */
const ui_hooks_t *ui_hooks(void);

/* ui.c, for handsfree_ui.c */
lv_obj_t *ui_active_scroller(void);
void ui_nav_next(void);
void ui_nav_prev(void);
