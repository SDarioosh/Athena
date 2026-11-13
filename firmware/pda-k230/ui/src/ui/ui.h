/* The UI frame: the status header, the tab bar, one page per tab, detail pages pushed over
 * them, the bottom sheet, toasts and the hands-free overlay. Screens are files of their own
 * (screen_*.c) that fill a page; they register in ui_screens.c. README.md has the recipe.
 *
 * Data comes from app_state (app_feed() etc.); the UI listens to its events and calls each
 * visible screen's update() with what changed. Nothing here talks to a socket: syncd is
 * reached through the hooks main.c sets (ui_set_hooks). */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "lvgl.h"
#include "vision_msg.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    UI_TAB_TODAY = 0,
    UI_TAB_BRIEF,
    UI_TAB_CALENDAR,
    UI_TAB_TASKS,
    UI_TAB_TALKS,
    UI_TAB_STATUS,
    UI_TABS,
} ui_tab_t;

/* What changed, for update() */
typedef enum {
    UI_DIRTY_FEED = 1u << 0,     /* a new feed (rebuild) */
    UI_DIRTY_OUTBOX = 1u << 1,   /* pending / failed marks changed (update rows in place) */
    UI_DIRTY_CLOCK = 1u << 2,    /* the minute or the display offset changed */
    UI_DIRTY_NET = 1u << 3,      /* syncd: link, net, tailnet, pair_result */
    UI_DIRTY_VISION = 1u << 4,   /* camera state, presence */
    UI_DIRTY_SETTINGS = 1u << 5, /* ui.json */
    UI_DIRTY_DETAIL = 1u << 6,   /* a detail blob arrived */
    UI_DIRTY_POWER = 1u << 7,
    UI_DIRTY_SHOWN = 1u << 8,    /* the page just became visible */
    UI_DIRTY_ALL = 0x1ffu,
} ui_dirty_t;

/* A tab's screen. build() runs once with an empty page (the content area between the
 * header and the tab bar, UI_W x UI_CONTENT_H, black, not scrollable: make your own
 * scroller, normally with ui_page_scroller()). update() runs whenever something in `what`
 * changed while the page exists (also when hidden: keep it cheap, or check
 * ui_tab_visible()). scroller() returns what hands-free page up/down scrolls (NULL: none). */
typedef struct {
    const char *label; /* the tab bar's label and the page title */
    const char *icon;  /* ICON_* */
    void (*build)(lv_obj_t *page);
    void (*update)(lv_obj_t *page, unsigned what);
    lv_obj_t *(*scroller)(lv_obj_t *page);
} ui_screen_t;

/* A detail page pushed over the tabs (a conversation, a transcript). build() gets an empty
 * page under a back bar with the title; arg is the caller's. destroy() frees arg. */
typedef struct {
    void (*build)(lv_obj_t *page, void *arg);
    void (*update)(lv_obj_t *page, void *arg, unsigned what);
    void (*destroy)(void *arg);
    lv_obj_t *(*scroller)(lv_obj_t *page);
    /* swipe left / hands-free next in this detail: true if handled (next page, next item) */
    bool (*next)(lv_obj_t *page, void *arg);
} ui_detail_t;

/* Hooks into the rest of the program (main.c); NULL members do nothing. */
typedef struct {
    int (*send_local)(const char *json, size_t len); /* a LOCAL message to syncd: 0 sent */
    void (*screen_power)(bool on);                   /* display on/off (fist, presence) */
    void (*brightness)(int percent);
} ui_hooks_t;

void ui_init(lv_display_t *disp);
void ui_set_hooks(const ui_hooks_t *h);

/* ---- navigation ---- */
void ui_show_tab(ui_tab_t tab, bool animate);
ui_tab_t ui_current_tab(void);
bool ui_tab_visible(ui_tab_t tab); /* the tab's page is what the user sees (no detail over it) */
void ui_push(const char *title, const ui_detail_t *d, void *arg);
void ui_pop(void);
bool ui_in_detail(void);

/* The bottom sheet (an event's details): build() fills it; closed by the X, a tap on the
 * scrim, swipe down or ui_sheet_close(). One at a time. */
void ui_sheet_open(void (*build)(lv_obj_t *sheet, void *arg), void *arg, void (*destroy)(void *arg));
void ui_sheet_close(void);
bool ui_sheet_is_open(void);

typedef enum {
    UI_TOAST_INFO,
    UI_TOAST_OK,
    UI_TOAST_ERROR,
} ui_toast_kind_t;
void ui_toast(ui_toast_kind_t kind, const char *fmt, ...) __attribute__((format(printf, 2, 3)));

/* LOCAL sync_now; toasts when syncd isn't there. */
void ui_request_sync(void);
int ui_send_local(const char *json, size_t len);

/* ---- hands-free focus ---- */
/* Mark obj as a focus stop (point moves the ring there). tick: it is a to-do, and a held
 * thumbs-up "clicks" it (LV_EVENT_CLICKED is sent to it). */
void ui_focusable(lv_obj_t *obj, bool tick);
/* A vision event (vision_server's callback). Ignored unless Hands-free is on. */
void ui_vision_event(const vision_event_t *ev);

/* ---- screen off ---- */
void ui_screen_off(void);
void ui_screen_on(void);
bool ui_screen_is_off(void);

/* ---- for the screenshot tool and tests ---- */
lv_obj_t *ui_tab_page(ui_tab_t tab);
/* Mark everything dirty and let update() run now (normally on the next LVGL timer). */
void ui_refresh_now(void);

#ifdef __cplusplus
}
#endif
