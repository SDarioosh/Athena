/* The frame around every screen (docs/t5-pda-plan.md §5): the header and the tab bar.
 * Pure drawing from plain values, so the host tests render the real thing
 * (test/test_ui_today.c); ui_pda.c gathers the values. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "epdiy.h"

#ifdef __cplusplus
extern "C" {
#endif

#define UI_TABS 6

typedef struct {
    const char *title;
    const char *clock; /* "09:00", or "--:--" before the clock is set */
    const char *date;  /* "Tue 20 Oct", or "" */
    int battery;       /* percent; < 0: unknown (no icon) */
    bool charging;
    const char *note;  /* only when worth a look: "syncing", "synced 3 h ago", "no feed"; or "" */
    int outbox;        /* actions waiting for the phone (shown as ↑n when > 0) */
} ui_header_t;

/* The header: the title and the clock (SemiBold 20 pt), then the date on the left and the
 * note, ↑n and the battery on the right, over a 2 px rule. Fills its area white first. */
void ui_chrome_header(const ui_header_t *h);
/* Its area, for the minute clock's small update. */
EpdRect ui_chrome_header_area(void);

/* The tab bar: plain labels; the active one SemiBold, with a bar under the top rule.
 * active < 0: none. hit (may be NULL) gets each tab's region and index. */
void ui_chrome_tabs(int active, void (*hit)(void *ctx, EpdRect r, int tab), void *ctx);

/* The Asleep screen (deep sleep, pda_sleep.c). Plain values that live in RTC memory, so a
 * sync wake draws exactly what the panel shows and changes only the synced line. Every
 * line is fixed when the T5 goes to sleep, so each names its date: nothing goes stale
 * overnight. */
#define UI_ASLEEP_LINE 48
#define UI_ASLEEP_TITLE 96
typedef struct {
    char since[UI_ASLEEP_LINE];      /* "since Tue 20 Oct 21:40", or "" */
    char synced[UI_ASLEEP_LINE];     /* "synced 21:35", or "" */
    char next_when[UI_ASLEEP_LINE];  /* the next event: "Thu 22 Oct 09:00", or "" for none */
    char next_title[UI_ASLEEP_TITLE];
    int8_t battery;                  /* percent at sleep; < 0 unknown */
} ui_asleep_t;

/* The whole Asleep screen into the framebuffer (no update). */
void ui_chrome_asleep(const ui_asleep_t *a);
/* Where the synced line is, for a sync wake's small update. */
EpdRect ui_chrome_asleep_synced_area(void);

/* A section heading 40 px tall from top: a SemiBold caps label (baseline 27 px down) and a
 * 2 px rule to the right edge, or to a SemiBold note at the right end (all black: the
 * panel doesn't show grey). */
#define UI_HEADING_H 40
void ui_chrome_heading(int top, const char *label, const char *right);

#ifdef __cplusplus
}
#endif
