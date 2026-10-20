/* Swipe navigation (docs/t5-pda-plan.md §5). Pure C; host-tested (test/test_ui_nav.c).
 *
 * Top-level tab screens: left/right = next/previous tab (wrapping), up/down = page.
 * Detail screens (conversation, transcript, event): right = back to the list; left = the
 * next page, then the next transcript part or the next item, nothing at the end; up/down =
 * page. */
#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

#define UI_NAV_TABS 6

typedef enum {
    UI_NAV_LEFT,  /* finger moved to the left: "forward" */
    UI_NAV_RIGHT, /* "back" */
    UI_NAV_UP,    /* next page */
    UI_NAV_DOWN,  /* previous page */
} ui_nav_dir_t;

typedef struct {
    bool detail;     /* a detail screen, not a tab */
    int tab;         /* the tab index (top level) */
    int page, pages; /* the current screen's page, 0-based, and page count */
    bool next_part;  /* a transcript with a next part */
    int sel, n_items; /* detail: the shown item and how many the list has (-1: none) */
} ui_nav_in_t;

typedef enum {
    UI_NAV_NONE,
    UI_NAV_TAB,       /* arg: the tab to show */
    UI_NAV_PAGE,      /* arg: the page to show */
    UI_NAV_BACK,      /* back to the list */
    UI_NAV_NEXT_PART, /* the transcript's next part */
    UI_NAV_ITEM,      /* arg: the item to show in the same detail screen */
} ui_nav_kind_t;

typedef struct {
    ui_nav_kind_t kind;
    int arg;
} ui_nav_t;

ui_nav_t ui_nav_swipe(const ui_nav_in_t *in, ui_nav_dir_t dir);

#ifdef __cplusplus
}
#endif
