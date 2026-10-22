#include "ui_chrome.h"

#include <stdio.h>

#include "gfx.h"
#include "ui_layout.h"

static const char *const TAB_LABELS[UI_TABS] = {"Today", "Brief", "Cal", "Tasks", "Talks", "Status"};

#define TITLE_Y 52 /* the title's and the clock's baseline */
#define SUB_Y 88   /* the second line's */

void ui_chrome_header(const ui_header_t *h)
{
    gfx_fill((EpdRect){0, 0, W, HEADER_H}, 0xFF);
    const int cw = gfx_text_width(BIG, h->clock);
    gfx_text_fit(BIG, MARGIN, TITLE_Y, W - 2 * MARGIN - cw - 20, h->title, GFX_INK);
    gfx_text(BIG, W - MARGIN - cw, TITLE_Y, h->clock, GFX_INK);
    /* right to left: battery, ↑n, note */
    int x = W - MARGIN;
    if (h->battery >= 0) {
        x = gfx_battery(x, SUB_Y, h->battery, h->charging) - 16;
    }
    if (h->outbox > 0) {
        char ob[16];
        snprintf(ob, sizeof(ob), "\xe2\x86\x91%d", h->outbox); /* ↑n */
        x -= gfx_text_width(TEXT_B, ob);
        gfx_text(TEXT_B, x, SUB_Y, ob, GFX_INK);
        x -= 16;
    }
    const int dw = h->date && h->date[0] ? gfx_text_width(TEXT, h->date) : 0;
    if (h->note && h->note[0] && x - MARGIN - dw - 16 > 40) {
        const int nw = gfx_text_width(TEXT, h->note);
        const int room = x - MARGIN - dw - 16;
        gfx_text_fit(TEXT, nw <= room ? x - nw : x - room, SUB_Y, room, h->note, GFX_INK);
    }
    if (dw) {
        gfx_text(TEXT, MARGIN, SUB_Y, h->date, GFX_INK);
    }
    gfx_hline(0, HEADER_H - 2, W, 0x00);
    gfx_hline(0, HEADER_H - 1, W, 0x00);
}

EpdRect ui_chrome_header_area(void)
{
    return (EpdRect){0, 0, W, HEADER_H};
}

void ui_chrome_tabs(int active, void (*hit)(void *ctx, EpdRect r, int tab), void *ctx)
{
    const int tw = W / UI_TABS;
    gfx_hline(0, TAB_Y, W, 0x00);
    gfx_hline(0, TAB_Y + 1, W, 0x00);
    for (int i = 0; i < UI_TABS; i++) {
        const EpdFont *f = i == active ? TEXT_B : TEXT;
        const int lw = gfx_text_width(f, TAB_LABELS[i]);
        gfx_text(f, i * tw + (tw - lw) / 2, TAB_Y + 46, TAB_LABELS[i], GFX_INK);
        if (i == active) {
            gfx_fill((EpdRect){i * tw + 10, TAB_Y + 2, tw - 20, 6}, 0x00);
        }
        if (hit) {
            hit(ctx, (EpdRect){i * tw, TAB_Y, tw, TAB_H}, i);
        }
    }
}

void ui_chrome_heading(int top, const char *label, const char *right)
{
    const int y = top + 27;
    const int x = gfx_text(TEXT_B, MARGIN, y, label, GFX_INK) + 12;
    int x_end = W - MARGIN;
    if (right && *right) {
        const int rw = gfx_text_width(TEXT_B, right);
        gfx_text(TEXT_B, x_end - rw, y, right, GFX_INK);
        x_end -= rw + 12;
    }
    if (x_end > x) {
        gfx_hline(x, y - 9, x_end - x, 0x00);
        gfx_hline(x, y - 8, x_end - x, 0x00);
    }
}

#define ASLEEP_FOOT_Y (TAB_Y + 40) /* the battery and the synced line */

static void centred(const EpdFont *f, int y, const char *s)
{
    gfx_text(f, (W - gfx_text_width(f, s)) / 2, y, s, GFX_INK);
}

void ui_chrome_asleep(const ui_asleep_t *a)
{
    centred(HUGE, 300, "Asleep");
    if (a->since[0]) {
        centred(TEXT_B, 356, a->since);
    }
    centred(TEXT, 396, "Press BOOT to wake");
    int top = 480;
    ui_chrome_heading(top, "NEXT UP", NULL);
    int y = top + UI_HEADING_H + 28;
    if (a->next_when[0]) {
        gfx_wrap(BIG, MARGIN, y + 12, LIST_W, a->next_title, GFX_INK, 0, 2);
        const int lines = gfx_wrap(BIG, 0, -1000, LIST_W, a->next_title, GFX_INK, 1 << 20, 0);
        y += 12 + (lines > 1 ? 2 : 1) * gfx_line(BIG);
        gfx_text(TEXT_B, MARGIN, y - 8, a->next_when, GFX_INK);
    } else {
        gfx_text(TEXT, MARGIN, y, "Nothing on the calendar this week", GFX_INK);
    }
    /* the foot: battery at sleep on the left, the synced line on the right */
    gfx_hline(0, TAB_Y, W, 0x00);
    gfx_hline(0, TAB_Y + 1, W, 0x00);
    if (a->battery >= 0) {
        char t[16];
        snprintf(t, sizeof(t), "%d%%", a->battery);
        gfx_battery_icon(MARGIN, ASLEEP_FOOT_Y, a->battery);
        gfx_text(TEXT, MARGIN + GFX_BATTERY_W + 10, ASLEEP_FOOT_Y, t, GFX_INK);
    }
    if (a->synced[0]) {
        gfx_text_right(TEXT, W - MARGIN, ASLEEP_FOOT_Y, a->synced, GFX_INK);
    }
}

EpdRect ui_chrome_asleep_synced_area(void)
{
    /* FONT_TEXT: ascender 24, descender 7; the right half, clear of the battery */
    return (EpdRect){W / 2, ASLEEP_FOOT_Y - 26, W / 2, 36};
}
