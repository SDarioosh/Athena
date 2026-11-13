/* Tasks (T5 plan §4.4, §5): the owner's Odysseus checklists (feed `todos`), a card per
 * note in the hub's order (pinned first, ★), its due date, its items with round ticks;
 * done items struck through; a tap toggles (todo_toggle), a second tap before syncd has it
 * undoes; pending and "couldn't sync" as on Today. */
#include <stdio.h>
#include <string.h>

#include "app_state.h"
#include "pda_time.h"
#include "screens.h"
#include "theme.h"
#include "widgets.h"

#define MAX_ROWS (FEED_MAX_TODO_NOTES * FEED_MAX_TODO_ITEMS)

static struct {
    lv_obj_t *scroll, *aside;
    todo_row_t *rows[MAX_ROWS];
    int n_rows;
    const feed_t *feed;
    int32_t day;
} K;

static int open_count(void)
{
    const feed_t *f = app_feed();
    int n = 0;
    for (int ni = 0; f && ni < f->n_todos; ni++) {
        for (int k = 0; k < f->todos[ni].n_items; k++) {
            app_todo_view_t v;
            app_todo_view(ni, k, &v);
            n += !v.done;
        }
    }
    return n;
}

static void set_aside(void)
{
    if (!K.aside) {
        return;
    }
    const int n = open_count();
    if (n == 0) {
        lv_label_set_text(K.aside, "all done");
    } else {
        lv_label_set_text_fmt(K.aside, "%d open", n);
    }
}

static void note_head(lv_obj_t *card, const feed_todo_t *note, int32_t today)
{
    lv_obj_t *row = lv_obj_create(card);
    lv_obj_remove_style_all(row);
    lv_obj_remove_flag(row, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(row, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_style_pad_hor(row, UI_CARD_PAD, 0);
    lv_obj_set_style_pad_top(row, SP_4, 0);
    lv_obj_set_style_pad_bottom(row, SP_1, 0);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_set_style_pad_column(row, SP_3, 0);

    char due[48] = "";
    uint32_t due_color = C_TEXT_2;
    if (note->due_day != FEED_NO_DAY) {
        char d[24];
        pda_fmt_day(note->due_day, d, sizeof(d));
        if (note->due_day < today) {
            snprintf(due, sizeof(due), "Overdue %s", d);
            due_color = C_DANGER;
        } else if (note->due_day == today) {
            snprintf(due, sizeof(due), "Due today");
            due_color = C_ACCENT;
        } else {
            snprintf(due, sizeof(due), "Due %s", d);
        }
    }
    int32_t w = UI_CARD_INNER_W;
    if (note->pinned) {
        lv_obj_t *star = lv_label_create(row);
        lv_label_set_text(star, ICON_STAR);
        lv_obj_set_style_text_font(star, F.small, 0);
        lv_obj_set_style_text_color(star, C(C_ACCENT), 0);
        lv_obj_set_style_pad_top(star, 4, 0);
        w -= 26 + SP_3;
    }
    lv_obj_t *chip = NULL;
    if (due[0]) {
        chip = ui_chip(row, NULL, due, due_color);
        lv_obj_set_style_margin_top(chip, 2, 0);
        lv_obj_update_layout(chip);
        w -= lv_obj_get_width(chip) + SP_3;
    }
    lv_obj_t *t = ui_text(row, F.body_b, C_TEXT, note->title ? note->title : "Untitled checklist", w, 2);
    if (chip) {
        lv_obj_move_to_index(t, note->pinned ? 1 : 0); /* the chip goes last */
    }
    lv_obj_set_flex_grow(t, 1);
}

static void rebuild(void)
{
    const feed_t *f = app_feed();
    const int32_t y = lv_obj_get_scroll_y(K.scroll);
    lv_obj_clean(K.scroll);
    K.n_rows = 0;
    K.aside = NULL;
    lv_obj_t *title = ui_page_title(K.scroll, "Tasks", f && f->n_todos ? " " : NULL);
    if (f && f->n_todos) {
        K.aside = lv_obj_get_child(title, 1);
        set_aside();
    }
    if (!f) {
        ui_no_feed(K.scroll);
        return;
    }
    if (!f->has_todos || f->n_todos == 0) {
        ui_empty_state(K.scroll, ICON_LIST_CHECKS, C_TEXT_2, "No checklists",
                       f->has_todos ? "Odysseus has no checklists with items. Add one on the Odysseus "
                                      "website; it shows up here after the next sync."
                                    : "Pair this PDA with Odysseus on the Status tab to see your "
                                      "checklists here.");
        return;
    }
    const int32_t today = app_today();
    for (int ni = 0; ni < f->n_todos; ni++) {
        const feed_todo_t *note = &f->todos[ni];
        lv_obj_t *card = ui_card(K.scroll);
        note_head(card, note, today);
        for (int k = 0; k < note->n_items; k++) {
            if (k > 0) {
                lv_obj_t *h = ui_hairline(card);
                lv_obj_set_style_margin_left(h, UI_CARD_PAD + UI_TICK + SP_4, 0);
                lv_obj_set_width(h, UI_CARD_W - UI_CARD_PAD - UI_TICK - SP_4);
            }
            todo_row_t *t = todo_row(card, ni, k, F.body, 3, NULL, UI_TICK);
            if (K.n_rows < MAX_ROWS) {
                K.rows[K.n_rows++] = t;
            }
        }
    }
    lv_obj_update_layout(K.scroll);
    lv_obj_scroll_to_y(K.scroll, y > 0 ? y : 0, LV_ANIM_OFF);
}

static void build(lv_obj_t *page)
{
    K.scroll = ui_page_scroller(page);
}

static void update(lv_obj_t *page, unsigned what)
{
    (void)page;
    const int32_t day = app_today();
    if ((what & UI_DIRTY_FEED) || app_feed() != K.feed || ((what & UI_DIRTY_CLOCK) && day != K.day)) {
        K.feed = app_feed();
        K.day = day;
        rebuild();
        return;
    }
    if (what & UI_DIRTY_OUTBOX) {
        for (int i = 0; i < K.n_rows; i++) {
            todo_row_refresh(K.rows[i], true);
        }
        set_aside();
    }
}

static lv_obj_t *scroller(lv_obj_t *page)
{
    (void)page;
    return K.scroll;
}

const ui_screen_t screen_tasks = {
    .label = "Tasks",
    .icon = ICON_LIST_CHECKS,
    .build = build,
    .update = update,
    .scroller = scroller,
};
