#include "widgets.h"

#include <stdio.h>
#include <string.h>

#include "pda_time.h"
#include "theme.h"
#include "today_plan.h"
#include "ui.h"

/* ---- page scaffolding ---- */

static void plain(lv_obj_t *o)
{
    lv_obj_remove_style_all(o);
    lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE);
}

lv_obj_t *ui_page_scroller(lv_obj_t *page)
{
    lv_obj_t *s = lv_obj_create(page);
    plain(s);
    lv_obj_add_flag(s, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_SCROLL_ELASTIC | LV_OBJ_FLAG_SCROLL_MOMENTUM);
    lv_obj_set_scroll_dir(s, LV_DIR_VER);
    lv_obj_set_scrollbar_mode(s, LV_SCROLLBAR_MODE_ACTIVE);
    lv_obj_set_size(s, UI_W, LV_PCT(100));
    lv_obj_set_flex_flow(s, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_hor(s, UI_MARGIN, 0);
    lv_obj_set_style_pad_top(s, SP_2, 0);
    lv_obj_set_style_pad_bottom(s, SP_10, 0);
    lv_obj_set_style_pad_row(s, UI_CARD_GAP, 0);
    /* a thin, rounded scrollbar that shows only while scrolling */
    lv_obj_set_style_width(s, 4, LV_PART_SCROLLBAR);
    lv_obj_set_style_radius(s, 2, LV_PART_SCROLLBAR);
    lv_obj_set_style_bg_color(s, C(C_TEXT_3), LV_PART_SCROLLBAR);
    lv_obj_set_style_bg_opa(s, LV_OPA_70, LV_PART_SCROLLBAR);
    lv_obj_set_style_pad_right(s, 4, LV_PART_SCROLLBAR);
    return s;
}

lv_obj_t *ui_page_title(lv_obj_t *parent, const char *title, const char *aside)
{
    lv_obj_t *row = lv_obj_create(parent);
    plain(row);
    lv_obj_set_size(row, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_style_pad_hor(row, SP_1, 0);
    lv_obj_set_style_pad_top(row, SP_3, 0);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(row, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_END);
    lv_obj_t *t = lv_label_create(row);
    lv_label_set_text(t, title);
    lv_obj_set_style_text_font(t, F.title, 0);
    lv_obj_set_style_text_color(t, C(C_TEXT), 0);
    if (aside && aside[0]) {
        lv_obj_t *a = lv_label_create(row);
        lv_label_set_text(a, aside);
        lv_obj_set_style_text_font(a, F.small, 0);
        lv_obj_set_style_text_color(a, C(C_TEXT_2), 0);
        lv_obj_set_style_pad_bottom(a, 6, 0); /* on the title's baseline */
    }
    return row;
}

lv_obj_t *ui_card(lv_obj_t *parent)
{
    lv_obj_t *c = lv_obj_create(parent);
    plain(c);
    lv_obj_add_style(c, &ST_CARD, 0);
    lv_obj_set_style_pad_hor(c, 0, 0);
    lv_obj_set_style_pad_ver(c, SP_2, 0);
    lv_obj_set_style_clip_corner(c, true, 0);
    lv_obj_set_size(c, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(c, LV_FLEX_FLOW_COLUMN);
    return c;
}

lv_obj_t *ui_section_head(lv_obj_t *card, const char *icon, const char *label, uint32_t color,
                          const char *action, lv_event_cb_t cb, void *user_data)
{
    lv_obj_t *row = lv_obj_create(card);
    plain(row);
    lv_obj_set_size(row, LV_PCT(100), 60);
    lv_obj_set_style_pad_left(row, UI_CARD_PAD, 0);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(row, SP_2, 0);
    if (icon) {
        lv_obj_t *i = lv_label_create(row);
        lv_label_set_text(i, icon);
        lv_obj_set_style_text_font(i, F.caption, 0);
        lv_obj_set_style_text_color(i, C(color), 0);
    }
    lv_obj_t *l = lv_label_create(row);
    lv_label_set_text(l, label);
    lv_obj_set_style_text_font(l, F.caption, 0);
    lv_obj_set_style_text_color(l, C(color), 0);
    lv_obj_set_style_text_letter_space(l, 2, 0);
    lv_obj_set_flex_grow(l, 1);
    if (action) {
        /* a full-height touch target at the right end */
        lv_obj_t *b = lv_obj_create(row);
        plain(b);
        lv_obj_set_size(b, LV_SIZE_CONTENT, UI_TOUCH_MIN);
        lv_obj_set_style_pad_hor(b, UI_CARD_PAD, 0);
        lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_style(b, &ST_PRESSED, LV_STATE_PRESSED);
        lv_obj_set_style_radius(b, 16, 0);
        lv_obj_t *t = lv_label_create(b);
        lv_label_set_text(t, action);
        lv_obj_set_style_text_font(t, F.small_b, 0);
        lv_obj_set_style_text_color(t, C(C_ACCENT), 0);
        lv_obj_center(t);
        if (cb) {
            lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, user_data);
        }
        ui_focusable(b, false);
    } else {
        lv_obj_set_style_pad_right(row, UI_CARD_PAD, 0);
    }
    return row;
}

lv_obj_t *ui_text(lv_obj_t *parent, const lv_font_t *font, uint32_t color, const char *text,
                  int32_t width, int max_lines)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, C(color), 0);
    if (max_lines > 0) {
        lv_obj_set_width(l, width);
        const int32_t lh = lv_font_get_line_height(font);
        lv_obj_set_style_text_line_space(l, 2, 0);
        lv_obj_set_style_max_height(l, max_lines * lh + (max_lines - 1) * 2, 0);
        lv_label_set_long_mode(l, LV_LABEL_LONG_MODE_DOTS);
    } else {
        if (width > 0) {
            lv_obj_set_width(l, width);
        }
        lv_label_set_long_mode(l, LV_LABEL_LONG_MODE_DOTS);
        lv_obj_set_height(l, lv_font_get_line_height(font));
    }
    lv_label_set_text(l, text ? text : "");
    return l;
}

void ui_text_set(lv_obj_t *label, const char *text)
{
    lv_label_set_text(label, text ? text : "");
}

void ui_show(lv_obj_t *obj, bool on)
{
    if (on == !lv_obj_has_flag(obj, LV_OBJ_FLAG_HIDDEN)) {
        return;
    }
    if (on) {
        lv_obj_remove_flag(obj, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(obj, LV_OBJ_FLAG_HIDDEN);
    }
    for (lv_obj_t *p = lv_obj_get_parent(obj); p; p = lv_obj_get_parent(p)) {
        lv_obj_refresh_self_size(p);
        lv_obj_mark_layout_as_dirty(p);
    }
}

lv_obj_t *ui_hairline(lv_obj_t *parent)
{
    lv_obj_t *h = lv_obj_create(parent);
    plain(h);
    lv_obj_set_size(h, LV_PCT(100), 1);
    lv_obj_set_style_bg_color(h, C(C_HAIRLINE), 0);
    lv_obj_set_style_bg_opa(h, LV_OPA_COVER, 0);
    return h;
}

/* ---- rows ---- */

static void free_user_data(lv_event_t *e)
{
    lv_free(lv_event_get_user_data(e));
}

ui_row_t *ui_row(lv_obj_t *parent, lv_event_cb_t on_click, void *user_data)
{
    ui_row_t *r = lv_malloc_zeroed(sizeof(*r));
    r->row = lv_obj_create(parent);
    plain(r->row);
    lv_obj_add_event_cb(r->row, free_user_data, LV_EVENT_DELETE, r);
    lv_obj_set_size(r->row, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_style_min_height(r->row, UI_ROW_MIN, 0);
    lv_obj_set_style_pad_hor(r->row, UI_CARD_PAD, 0);
    lv_obj_set_style_pad_ver(r->row, SP_5, 0);
    lv_obj_set_style_pad_column(r->row, SP_4, 0);
    lv_obj_set_flex_flow(r->row, LV_FLEX_FLOW_ROW);
    /* the tick / time and the chevron line up with the title's first line */
    lv_obj_set_flex_align(r->row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_add_style(r->row, &ST_PRESSED, LV_STATE_PRESSED);
    if (on_click) {
        lv_obj_add_flag(r->row, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(r->row, on_click, LV_EVENT_CLICKED, user_data);
    }

    r->left = lv_obj_create(r->row);
    plain(r->left);
    lv_obj_set_size(r->left, UI_LEFT_COL, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(r->left, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(r->left, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_remove_flag(r->left, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_flag(r->left, LV_OBJ_FLAG_OVERFLOW_VISIBLE); /* the tick's pop and nudge */

    r->mid = lv_obj_create(r->row);
    plain(r->mid);
    lv_obj_set_flex_grow(r->mid, 1);
    lv_obj_set_height(r->mid, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(r->mid, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(r->mid, SP_1, 0);
    lv_obj_remove_flag(r->mid, LV_OBJ_FLAG_CLICKABLE);

    r->right = lv_obj_create(r->row);
    plain(r->right);
    lv_obj_set_size(r->right, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_style_pad_top(r->right, 6, 0);
    lv_obj_remove_flag(r->right, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_flag(r->right, LV_OBJ_FLAG_HIDDEN);
    return r;
}

/* the width left for the middle column of a row inside a card */
#define ROW_MID_W (UI_CARD_W - 2 * UI_CARD_PAD - UI_LEFT_COL - SP_4)

/* ---- the tick ---- */

static void tick_anim(void *var, int32_t v)
{
    /* v: 0 (open) .. 1024 (done); the pop is in the middle of the way */
    lv_obj_t *t = var;
    lv_obj_t *check = lv_obj_get_child(t, 0);
    const lv_opa_t opa = (lv_opa_t)(v * 255 / 1024);
    lv_obj_set_style_bg_opa(t, opa, 0);
    lv_obj_set_style_border_color(t, lv_color_mix(C(C_ACCENT), C(C_TEXT_3), opa), 0);
    lv_obj_set_style_opa(check, opa, 0);
    const int32_t pop = v < 512 ? v : 1024 - v; /* 0 .. 512 .. 0 */
    const int32_t scale = 256 + pop * 40 / 512;
    lv_obj_set_style_transform_scale_x(t, scale, 0);
    lv_obj_set_style_transform_scale_y(t, scale, 0);
}

lv_obj_t *ui_tick(lv_obj_t *parent)
{
    lv_obj_t *t = lv_obj_create(parent);
    plain(t);
    lv_obj_set_size(t, UI_TICK, UI_TICK);
    lv_obj_set_style_radius(t, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_border_width(t, 3, 0);
    lv_obj_set_style_border_color(t, C(C_TEXT_3), 0);
    lv_obj_set_style_bg_color(t, C(C_ACCENT), 0);
    lv_obj_set_style_bg_opa(t, LV_OPA_TRANSP, 0);
    lv_obj_set_style_transform_pivot_x(t, UI_TICK / 2, 0);
    lv_obj_set_style_transform_pivot_y(t, UI_TICK / 2, 0);
    lv_obj_remove_flag(t, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_t *c = lv_label_create(t);
    lv_label_set_text(c, ICON_CHECK);
    lv_obj_set_style_text_font(c, F.icon_s, 0);
    lv_obj_set_style_text_color(c, C(C_ON_ACCENT), 0);
    lv_obj_set_style_opa(c, LV_OPA_TRANSP, 0);
    lv_obj_center(c);
    return t;
}

void ui_tick_set(lv_obj_t *tick, bool done, bool animate)
{
    lv_anim_delete(tick, tick_anim);
    if (!animate) {
        tick_anim(tick, done ? 1024 : 0);
        lv_obj_set_style_transform_scale_x(tick, 256, 0);
        lv_obj_set_style_transform_scale_y(tick, 256, 0);
        return;
    }
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, tick);
    lv_anim_set_exec_cb(&a, tick_anim);
    lv_anim_set_duration(&a, UI_ANIM);
    lv_anim_set_values(&a, done ? 0 : 1024, done ? 1024 : 0);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
    lv_anim_start(&a);
}

/* ---- the to-do row ---- */

static void todo_clicked(lv_event_t *e)
{
    todo_row_t *t = lv_event_get_user_data(e);
    const app_todo_result_t r = app_todo_tap(t->note, t->item);
    switch (r) {
    case APP_TODO_QUEUED: break; /* the tick says it */
    case APP_TODO_UNDONE: ui_toast(UI_TOAST_INFO, "Undone"); break;
    case APP_TODO_REVERSE: ui_toast(UI_TOAST_INFO, "Changing it back with the next sync"); break;
    case APP_TODO_FULL: ui_toast(UI_TOAST_ERROR, "Outbox full: sync first"); break;
    case APP_TODO_ERROR: ui_toast(UI_TOAST_ERROR, "Couldn't save the change"); break;
    }
}

static void free_todo(lv_event_t *e)
{
    lv_free(lv_event_get_user_data(e));
}

todo_row_t *todo_row(lv_obj_t *parent, int note_idx, int item_idx, const lv_font_t *title_font,
                     int title_lines, const char *sub, int32_t left_w)
{
    const feed_t *f = app_feed();
    todo_row_t *t = lv_malloc_zeroed(sizeof(*t));
    t->note = note_idx;
    t->item = item_idx;
    snprintf(t->sub_text, sizeof(t->sub_text), "%s", sub ? sub : "");
    t->r = ui_row(parent, todo_clicked, t);
    lv_obj_add_event_cb(t->r->row, free_todo, LV_EVENT_DELETE, t);
    ui_focusable(t->r->row, true);
    lv_obj_set_width(t->r->left, left_w);
    lv_obj_set_flex_align(t->r->left, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    t->tick = ui_tick(t->r->left);
    lv_obj_set_style_pad_top(t->r->mid, 4, 0); /* the title's first line on the tick's middle */
    const int32_t w = ROW_MID_W + UI_LEFT_COL - left_w - 40;
    t->title = ui_text(t->r->mid, title_font, C_TEXT, f->todos[note_idx].items[item_idx].text, w, title_lines);
    t->sub = ui_text(t->r->mid, F.small, C_TEXT_2, t->sub_text, w, 2);
    t->mark = lv_label_create(t->r->right);
    lv_obj_set_style_text_font(t->mark, F.icon_s, 0);
    lv_obj_set_style_text_color(t->mark, C(C_TEXT_3), 0);
    lv_label_set_text(t->mark, ICON_UPLOAD);
    todo_row_refresh(t, false);
    return t;
}

void todo_row_refresh(todo_row_t *t, bool animate)
{
    app_todo_view_t v;
    app_todo_view(t->note, t->item, &v);
    if (t->shown && v.done == t->done && v.pending == t->pending && v.failed == t->failed) {
        return;
    }
    const bool anim = animate && t->shown && v.done != t->done;
    t->done = v.done;
    t->pending = v.pending;
    t->failed = v.failed;
    t->shown = true;
    ui_tick_set(t->tick, v.done, anim);
    lv_obj_set_style_text_decor(t->title, v.done ? LV_TEXT_DECOR_STRIKETHROUGH : LV_TEXT_DECOR_NONE, 0);
    lv_obj_set_style_text_color(t->title, C(v.done ? C_TEXT_3 : C_TEXT), 0);
    if (v.failed) {
        ui_text_set(t->sub, ICON_CIRCLE_ALERT " Couldn't sync \xe2\x80\x93 changed on the website?");
        lv_obj_set_style_text_color(t->sub, C(C_DANGER), 0);
    } else {
        ui_text_set(t->sub, t->sub_text);
        lv_obj_set_style_text_color(t->sub, C(v.done ? C_TEXT_3 : C_TEXT_2), 0);
    }
    ui_show(t->sub, t->sub_text[0] || v.failed);
    ui_show(t->r->right, v.pending);
}

/* ---- chips and empty states ---- */

lv_obj_t *ui_chip(lv_obj_t *parent, const char *icon, const char *text, uint32_t color)
{
    lv_obj_t *c = lv_obj_create(parent);
    plain(c);
    lv_obj_set_size(c, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_style_radius(c, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(c, lv_color_mix(C(color), C(C_BG), LV_OPA_20), 0);
    lv_obj_set_style_bg_opa(c, LV_OPA_COVER, 0);
    lv_obj_set_style_pad_hor(c, SP_3, 0);
    lv_obj_set_style_pad_ver(c, SP_1, 0);
    lv_obj_t *l = lv_label_create(c);
    char buf[160];
    snprintf(buf, sizeof(buf), "%s%s%s", icon ? icon : "", icon ? " " : "", text);
    lv_label_set_text(l, buf);
    lv_obj_set_style_text_font(l, F.caption, 0);
    lv_obj_set_style_text_color(l, C(color), 0);
    return c;
}

lv_obj_t *ui_empty_state(lv_obj_t *parent, const char *icon, uint32_t icon_color, const char *title,
                         const char *text)
{
    lv_obj_t *box = lv_obj_create(parent);
    plain(box);
    lv_obj_set_size(box, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_style_pad_top(box, SP_12 * 2, 0);
    lv_obj_set_style_pad_bottom(box, SP_12, 0);
    lv_obj_set_flex_flow(box, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(box, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_row(box, SP_4, 0);

    lv_obj_t *ring = lv_obj_create(box);
    plain(ring);
    lv_obj_set_size(ring, 168, 168);
    lv_obj_set_style_radius(ring, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(ring, lv_color_mix(C(icon_color), C(C_BG), LV_OPA_20), 0);
    lv_obj_set_style_bg_opa(ring, LV_OPA_COVER, 0);
    lv_obj_set_style_margin_bottom(ring, SP_4, 0);
    lv_obj_t *i = lv_label_create(ring);
    lv_label_set_text(i, icon);
    lv_obj_set_style_text_font(i, F.icon_xl, 0);
    lv_obj_set_style_text_color(i, C(icon_color), 0);
    lv_obj_center(i);

    lv_obj_t *t = lv_label_create(box);
    lv_label_set_text(t, title);
    lv_obj_set_style_text_font(t, F.title, 0);
    lv_obj_set_style_text_color(t, C(C_TEXT), 0);
    if (text && text[0]) {
        lv_obj_t *s = lv_label_create(box);
        lv_label_set_text(s, text);
        lv_label_set_long_mode(s, LV_LABEL_LONG_MODE_WRAP);
        lv_obj_set_width(s, UI_CARD_W - 2 * SP_8);
        lv_obj_set_style_text_align(s, LV_TEXT_ALIGN_CENTER, 0);
        lv_obj_set_style_text_font(s, F.small, 0);
        lv_obj_set_style_text_color(s, C(C_TEXT_2), 0);
        lv_obj_set_style_text_line_space(s, 4, 0);
    }
    return box;
}

void ui_fmt_event_when(const feed_event_t *e, int32_t today, int tz_min, char *out, size_t cap)
{
    const int32_t day = today_event_day(e, tz_min);
    char d[24];
    if (day == today) {
        snprintf(d, sizeof(d), "Today");
    } else if (day == today + 1) {
        snprintf(d, sizeof(d), "Tomorrow");
    } else {
        pda_fmt_day(day, d, sizeof(d));
    }
    if (e->all_day) {
        snprintf(out, cap, "%s, all day", d);
        return;
    }
    char a[12], b[12];
    pda_fmt_hm(e->start, tz_min, a, sizeof(a));
    if (e->end > e->start) {
        pda_fmt_hm(e->end, tz_min, b, sizeof(b));
        snprintf(out, cap, "%s %s \xe2\x80\x93 %s", d, a, b);
    } else {
        snprintf(out, cap, "%s %s", d, a);
    }
}
