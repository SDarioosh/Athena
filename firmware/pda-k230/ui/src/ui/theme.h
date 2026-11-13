/* Design tokens: every colour, size, space, radius, duration and font of the UI. Screens use
 * these names only (no literal colours or pixel sizes in screen files), so the look stays
 * one. Portrait 568 x 1232 AMOLED (~330 ppi): true black background, one accent. */
#pragma once

#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- screen ---- */
#define UI_W 568
#define UI_H 1232

/* ---- colours (0xRRGGBB) ---- */
#define C_BG 0x000000        /* true black: AMOLED pixels off */
#define C_SURFACE 0x111114   /* cards */
#define C_SURFACE_2 0x1C1C21 /* pressed rows, chips, the toast */
#define C_SURFACE_3 0x2A2A31 /* borders of controls */
#define C_HAIRLINE 0x232329  /* rules between rows */
#define C_TEXT 0xF3F3F6      /* primary text */
#define C_TEXT_2 0xA4A4AF    /* secondary text */
#define C_TEXT_3 0x6C6C76    /* done items, hints */
#define C_ACCENT 0xF5B53F    /* the one accent: Athena amber */
#define C_ACCENT_TINT 0x1E170A /* the accent at ~12 % on black: the insight card */
#define C_ON_ACCENT 0x1A1204 /* text and ticks on an accent fill */
#define C_DANGER 0xFF6B5E    /* "couldn't sync", overdue */
#define C_CAMERA 0x34D15A    /* the camera-on dot (the convention: green) */

/* ---- spacing (a 4 px grid) ---- */
#define SP_1 4
#define SP_2 8
#define SP_3 12
#define SP_4 16
#define SP_5 20
#define SP_6 24
#define SP_8 32
#define SP_10 40
#define SP_12 48

#define UI_MARGIN 20     /* screen edge to cards */
#define UI_CARD_PAD 24   /* inside a card */
#define UI_CARD_GAP 20   /* between cards */
#define UI_CARD_RADIUS 28
#define UI_ROW_MIN 88    /* a list row */
#define UI_TOUCH_MIN 72  /* the smallest touch target */
#define UI_LEFT_COL 64   /* a row's tick box or time column */
#define UI_TICK 44       /* the round tick box */

#define UI_STATUS_H 64   /* the status header */
#define UI_TABBAR_H 112  /* the tab bar */
#define UI_CONTENT_H (UI_H - UI_STATUS_H - UI_TABBAR_H)
#define UI_CARD_W (UI_W - 2 * UI_MARGIN)
#define UI_CARD_INNER_W (UI_CARD_W - 2 * UI_CARD_PAD)

/* ---- motion ---- */
#define UI_ANIM_FAST 150  /* presses, small state changes */
#define UI_ANIM 220       /* tab slides, the tick, the focus ring */
#define UI_ANIM_SLOW 250  /* sheets */
#define UI_TOAST_MS 2600

/* ---- type scale (px; Inter via tiny_ttf). Body text is never under 26 px. ---- */
#define FS_CAPTION 22  /* section labels (caps, tracked), tab labels, chips */
#define FS_SMALL 26    /* secondary lines */
#define FS_BODY 30     /* item titles, body */
#define FS_LEAD 28     /* the insight headline */
#define FS_TITLE 44    /* screen titles */
#define FS_DISPLAY 64  /* big numbers */
#define FS_HERO 96     /* the empty state's tick */

typedef struct {
    const lv_font_t *caption;   /* 22 SemiBold */
    const lv_font_t *small;     /* 26 Regular */
    const lv_font_t *small_b;   /* 26 SemiBold */
    const lv_font_t *body;      /* 30 Regular */
    const lv_font_t *body_b;    /* 30 SemiBold */
    const lv_font_t *lead;      /* 28 Regular */
    const lv_font_t *title;     /* 44 Bold */
    const lv_font_t *display;   /* 64 Bold */
    const lv_font_t *icon_s;    /* Lucide 24 */
    const lv_font_t *icon;      /* Lucide 32 */
    const lv_font_t *icon_l;    /* Lucide 48 */
    const lv_font_t *icon_xl;   /* Lucide 96 */
} ui_fonts_t;

extern ui_fonts_t F;

/* Load the fonts and set LVGL's default theme to ours. Call once after lv_init() and the
 * display. Every text font falls back to the Lucide font of its size, so a label can mix
 * text and ICON_* glyphs. */
void theme_init(lv_display_t *disp);

/* Shared styles */
extern lv_style_t ST_CARD;       /* surface, radius, padding */
extern lv_style_t ST_PRESSED;    /* a row's pressed look */
extern lv_style_t ST_FOCUS;      /* keyboard/hands-free focus outline */

static inline lv_color_t C(uint32_t hex)
{
    return lv_color_hex(hex);
}

/* ---- icons: Lucide (ISC), the glyphs in fonts/lucide.ttf (fonts/make_fonts.py) ---- */
#define ICON_SUN "\xEE\x85\xB8"
#define ICON_NEWSPAPER "\xEE\x8D\x88"
#define ICON_CALENDAR_DAYS "\xEE\x8A\xB9"
#define ICON_LIST_CHECKS "\xEE\x87\x90"
#define ICON_MESSAGES_SQUARE "\xEE\x90\x8D"
#define ICON_ACTIVITY "\xEE\x80\xB8"
#define ICON_CHECK "\xEE\x81\xAC"
#define ICON_REFRESH_CW "\xEE\x85\x85"
#define ICON_WIFI "\xEE\x86\xAE"
#define ICON_WIFI_OFF "\xEE\x86\xAF"
#define ICON_BATTERY "\xEE\x81\x93"
#define ICON_BATTERY_LOW "\xEE\x81\x96"
#define ICON_BATTERY_MEDIUM "\xEE\x81\x97"
#define ICON_BATTERY_FULL "\xEE\x81\x95"
#define ICON_BATTERY_CHARGING "\xEE\x81\x94"
#define ICON_CAMERA "\xEE\x81\xA4"
#define ICON_CLOUD_OFF "\xEE\x82\x8D"
#define ICON_HAND "\xEE\x87\x97"
#define ICON_STAR "\xEE\x85\xB6"
#define ICON_MAP_PIN "\xEE\x84\x91"
#define ICON_CHEVRON_RIGHT "\xEE\x81\xAF"
#define ICON_CHEVRON_LEFT "\xEE\x81\xAE"
#define ICON_X "\xEE\x86\xB2"
#define ICON_ARROW_DOWN "\xEE\x81\x82"
#define ICON_CLOCK "\xEE\x82\x87"
#define ICON_UPLOAD "\xEE\x86\x9E"
#define ICON_CIRCLE_ALERT "\xEE\x81\xB7"
#define ICON_SPARKLES "\xEE\x90\x92"
#define ICON_UNDO "\xEE\x8A\xA1"
#define ICON_INBOX "\xEE\x83\xB7"
#define ICON_LOADER "\xEE\x84\x8A"

#ifdef __cplusplus
}
#endif
