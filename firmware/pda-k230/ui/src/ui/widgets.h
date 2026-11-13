/* Shared building blocks for screens: the page scroller, titles, cards, section headings,
 * rows, the animated round tick, the to-do row (Today and Tasks), chips, empty states.
 * Everything takes its look from theme.h. */
#pragma once

#include <stdbool.h>

#include "app_state.h"
#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

/* A full-page vertical scroller with the screen margins, momentum and elastic edges; its
 * children stack in a column with UI_CARD_GAP between them. */
lv_obj_t *ui_page_scroller(lv_obj_t *page);

/* The big title at the top of a page ("Today"), with an optional line on the right
 * ("Tue 20 Oct"). */
lv_obj_t *ui_page_title(lv_obj_t *parent, const char *title, const char *aside);

/* A card: rounded surface, a column. */
lv_obj_t *ui_card(lv_obj_t *parent);

/* A card's heading: CAPS label (tracked), and an optional action on the right ("+2 more ›")
 * that calls cb with user_data. Returns the heading row. */
lv_obj_t *ui_section_head(lv_obj_t *card, const char *icon, const char *label, uint32_t color,
                          const char *action, lv_event_cb_t cb, void *user_data);

/* A label of a font and colour. max_lines > 0: wrap within width and cut the last line
 * with "…" (the height follows the text up to that many lines); 0: one line, no wrap. */
lv_obj_t *ui_text(lv_obj_t *parent, const lv_font_t *font, uint32_t color, const char *text,
                  int32_t width, int max_lines);
/* Change a ui_text's text keeping its line limit. */
void ui_text_set(lv_obj_t *label, const char *text);

/* Show or hide obj; a content-sized parent is resized (LVGL doesn't do it for a hidden
 * flag). */
void ui_show(lv_obj_t *obj, bool on);

/* A 1 px rule across a card's inner width. */
lv_obj_t *ui_hairline(lv_obj_t *parent);

/* A clickable list row: a left column (UI_LEFT_COL), a middle column, a right slot.
 * The returned struct is owned by the row (freed with it). */
typedef struct {
    lv_obj_t *row, *left, *mid, *right;
} ui_row_t;
ui_row_t *ui_row(lv_obj_t *parent, lv_event_cb_t on_click, void *user_data);

/* The round tick box: a ring, filled with the accent and a check when done. ui_tick_set
 * with animate runs the tick's pop (UI_ANIM). */
lv_obj_t *ui_tick(lv_obj_t *parent);
void ui_tick_set(lv_obj_t *tick, bool done, bool animate);

/* A checklist item row, shared by Today and Tasks: the tick, the text (up to title_lines,
 * struck through when done), a second line (sub, may be NULL), a mark on the right while a
 * change waits for syncd, and "Couldn't sync" when the server refused it. A tap calls
 * app_todo_tap() and toasts its result. */
typedef struct {
    ui_row_t *r;
    lv_obj_t *tick, *title, *sub, *mark;
    int note, item;
    bool done, pending, failed, shown;
    char sub_text[200];
} todo_row_t;
/* left_w: the tick's column, UI_LEFT_COL beside event rows (the tick centred in it), or
 * UI_TICK in a list of to-dos only. */
todo_row_t *todo_row(lv_obj_t *parent, int note_idx, int item_idx, const lv_font_t *title_font,
                     int title_lines, const char *sub, int32_t left_w);
/* Show the item's current state (app_todo_view); animate: the tick pops, the text fades. */
void todo_row_refresh(todo_row_t *t, bool animate);

/* A small rounded chip: icon and text in a colour (on a dark tint of it). */
lv_obj_t *ui_chip(lv_obj_t *parent, const char *icon, const char *text, uint32_t color);

/* A centred empty state: a big icon in a ring, a title and a line under it. */
lv_obj_t *ui_empty_state(lv_obj_t *parent, const char *icon, uint32_t icon_color, const char *title,
                         const char *text);

/* An event's time for humans: "Today 12:30 – 13:30", "Wed 21 Oct, all day". */
void ui_fmt_event_when(const feed_event_t *e, int32_t today, int tz_min, char *out, size_t cap);

#ifdef __cplusplus
}
#endif
