/* The UI fonts (plan §2), all Fira Sans:
 * - FONT_TEXT: Regular 12 pt, body text;
 * - FONT_TEXT_B: SemiBold 12 pt, item titles, labels, buttons and the active tab;
 * - FONT_BIG: SemiBold 20 pt, screen titles, the clock and headings;
 * - FONT_HUGE: SemiBold 32 pt, ASCII only: the sleep screen and the empty Today.
 * The SemiBold fonts hold Latin-1 and punctuation only; gfx.c draws any other character
 * (arrows, ✓, ★, ▲) from the Regular font of the nearest size (gfx_fallback()). */
#pragma once

#include "epdiy.h"

extern const EpdFont FiraSans_12;
extern const EpdFont FiraSans_20;
extern const EpdFont FiraSansSB_12;
extern const EpdFont FiraSansSB_20;
extern const EpdFont FiraSansSB_32;

#define FONT_TEXT FiraSans_12
#define FONT_TEXT_B FiraSansSB_12
#define FONT_BIG FiraSansSB_20
#define FONT_HUGE FiraSansSB_32
/* Regular 20 pt: the fallback of FONT_BIG and FONT_HUGE */
#define FONT_BIG_REGULAR FiraSans_20
