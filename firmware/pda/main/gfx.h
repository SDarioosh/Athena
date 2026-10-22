/* Text and shape helpers on top of epdiy's fonts, for ui_pda.c: measuring UTF-8 strings,
 * ellipsizing, word wrapping, strike-through and simple buttons. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "epdiy.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Text colours (4-bit: 0 black .. 15 white). All black: grey text doesn't show on the
 * owner's panel, so secondary and queued text is told apart by layout, labels ("pending")
 * and marks, not by colour. */
#define GFX_INK 0x0
#define GFX_PENDING 0x0 /* queued but not confirmed by a feed yet */
#define GFX_FAINT 0x0

void gfx_begin(uint8_t *fb);

int gfx_text_width(const EpdFont *f, const char *s);
/* Draw s with its baseline at y; color 0..15; returns the x after it. */
int gfx_text(const EpdFont *f, int x, int y, const char *s, uint8_t color);
/* One line cut to max_w with "…". */
int gfx_text_fit(const EpdFont *f, int x, int y, int max_w, const char *s, uint8_t color);
/* White on black (an inverted button, the toast); returns the x after it. */
int gfx_text_inv(const EpdFont *f, int x, int y, const char *s);
/* Right-aligned at x_right. */
void gfx_text_right(const EpdFont *f, int x_right, int y, const char *s, uint8_t color);
/* Word-wrapped paragraph starting at baseline y, skipping the first skip_lines lines, at most
 * max_lines lines (the last cut with "…" if there is more). Returns the lines it needs in
 * total (drawn or not), so callers can page. */
int gfx_wrap(const EpdFont *f, int x, int y, int w, const char *s, uint8_t color, int skip_lines,
             int max_lines);
/* Pixel widths of the lines the last gfx_wrap() drew, at most max; returns how many
 * (for strike-through). */
int gfx_wrap_widths(int *out, int max);

/* The Regular font that draws what a SemiBold one lacks (fonts.h), or NULL. */
const EpdFont *gfx_fallback(const EpdFont *f);

/* Line height of a font. */
int gfx_line(const EpdFont *f);

void gfx_strike(const EpdFont *f, int x, int y, int w);
/* A round tick box centred on (cx, cy): an open ring, or filled black with a white tick. */
void gfx_check(int cx, int cy, int r, bool done);
/* A filled dot (an event's mark). */
void gfx_dot(int cx, int cy, int r);
/* A battery icon GFX_BATTERY_W wide from x, centred on a FONT_TEXT line with baseline y,
 * filled to percent. */
#define GFX_BATTERY_W 29
void gfx_battery_icon(int x, int y, int percent);
/* The icon ending at x_right with its percentage left of it (FONT_TEXT; "+" when
 * charging). Returns the x where the percentage starts. */
int gfx_battery(int x_right, int y, int percent, bool charging);
/* A framed button with a centred SemiBold label; inverted = filled black. */
void gfx_button(EpdRect r, const char *label, bool inverted);
void gfx_hline(int x, int y, int w, uint8_t color);
void gfx_fill(EpdRect r, uint8_t color);
void gfx_frame(EpdRect r, uint8_t color);

#ifdef __cplusplus
}
#endif
