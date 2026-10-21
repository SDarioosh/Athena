/* The screens' geometry (portrait, docs/t5-pda-plan.md §5), shared by ui_pda.c and
 * ui_today.c: a header, the content, a bottom row for buttons and the pager, the tab bar. */
#pragma once

#include "fonts.h"

#define W 540  /* PDA_SCR_W */
#define H 960  /* PDA_SCR_H */
#define HEADER_H 100
#define TAB_H 76
#define TAB_Y (H - TAB_H)
#define TOP (HEADER_H + 8)
#define BOTTOM (TAB_Y - 6)
#define MARGIN 16
#define ROW_H 52                 /* the bottom row: the screen's buttons and the pager */
#define ROW_Y (BOTTOM - ROW_H)
#define LIST_BOTTOM (ROW_Y - 10) /* lists and text end here */
#define LIST_W (W - 2 * MARGIN)
#define PAGER_BTN 64
#define TEXT (&FONT_TEXT)
#define BIG (&FONT_BIG)
#define LH 30                    /* FONT_TEXT line advance */
