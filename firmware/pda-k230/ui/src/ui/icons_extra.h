/* The icons the Talks, Status, pairing and ambient screens use beyond theme.h's ICON_*:
 * fonts/lucide-extra.ttf (Lucide, ISC; fonts/make_icons_extra.py), for icon-only labels
 * (the Inter files don't carry them, so they can't be mixed into a text label). Also the
 * ambient screen's big clock face (Inter Bold, FS_CLOCK). The fonts are made on first use. */
#pragma once

#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

#define FS_CLOCK 168 /* the ambient clock */

typedef struct {
    const lv_font_t *icon_s; /* 24 */
    const lv_font_t *icon;   /* 32 */
    const lv_font_t *icon_l; /* 48 */
    const lv_font_t *icon_xl; /* FS_HERO */
    const lv_font_t *clock;  /* Inter Bold FS_CLOCK */
} ui_fonts_extra_t;

const ui_fonts_extra_t *fx(void);

/* Lucide 1.x codepoints, as make_icons_extra.py prints them */
#define ICONX_QR_CODE "\xEE\x87\x9F"
#define ICONX_SCAN_QR_CODE "\xEE\x97\xB6"
#define ICONX_KEYBOARD "\xEE\x8A\x84"
#define ICONX_LINK "\xEE\x84\x82"
#define ICONX_UNLINK "\xEE\x86\x9C"
#define ICONX_TRASH_2 "\xEE\x86\x8E"
#define ICONX_POWER "\xEE\x85\x80"
#define ICONX_SUN_DIM "\xEE\x8A\x99"
#define ICONX_TIMER "\xEE\x87\xA0"
#define ICONX_APP_WINDOW "\xEE\x90\xA6"
#define ICONX_THUMBS_UP "\xEE\x86\x8A"
#define ICONX_POINTER "\xEE\x87\xA8"
#define ICONX_ARROW_LEFT "\xEE\x81\x88"
#define ICONX_ARROW_RIGHT "\xEE\x81\x89"
#define ICONX_ARROW_UP "\xEE\x81\x8A"
#define ICONX_GLOBE "\xEE\x83\xA8"
#define ICONX_INFO "\xEE\x83\xB9"
#define ICONX_SIGNAL "\xEE\x89\x9F"
#define ICONX_GRAB "\xEE\x87\xA6"
#define ICONX_CIRCLE_CHECK "\xEE\x88\xA6"
#define ICONX_CIRCLE_X "\xEE\x82\x84"
#define ICONX_MOON "\xEE\x84\x9E"
#define ICONX_SHIELD_CHECK "\xEE\x87\xBF"
#define ICONX_NETWORK "\xEE\x84\xA5"
#define ICONX_USER_ROUND "\xEE\x91\xA8"

#ifdef __cplusplus
}
#endif
