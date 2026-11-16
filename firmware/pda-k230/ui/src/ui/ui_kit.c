#include "ui_kit.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "talks.h"
#include "theme.h"
#include "ui.h"
#include "widgets.h"

#define BTN_H (UI_TOUCH_MIN + 8)
#define BTN_RADIUS 24

static void plain(lv_obj_t *o)
{
    lv_obj_remove_style_all(o);
    lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
}

lv_obj_t *kit_icon(lv_obj_t *parent, const lv_font_t *font, const char *glyph, uint32_t color)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_label_set_text(l, glyph);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, C(color), 0);
    return l;
}

static lv_obj_t *box(lv_obj_t *parent, lv_flex_flow_t flow, int32_t gap)
{
    lv_obj_t *o = lv_obj_create(parent);
    plain(o);
    lv_obj_set_size(o, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(o, flow);
    lv_obj_set_style_pad_row(o, gap, 0);
    lv_obj_set_style_pad_column(o, gap, 0);
    return o;
}

lv_obj_t *kit_col(lv_obj_t *parent, int32_t gap)
{
    return box(parent, LV_FLEX_FLOW_COLUMN, gap);
}

lv_obj_t *kit_row(lv_obj_t *parent, int32_t gap)
{
    lv_obj_t *r = box(parent, LV_FLEX_FLOW_ROW, gap);
    lv_obj_set_flex_align(r, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    return r;
}

/* ---- buttons ---- */

static void kind_colors(kit_kind_t k, uint32_t *bg, uint32_t *bg_pressed, uint32_t *fg)
{
    *bg = C_SURFACE_2;
    *bg_pressed = C_SURFACE_3;
    *fg = C_TEXT;
    switch (k) {
    case KIT_PRIMARY: *bg = C_ACCENT; *bg_pressed = C_TEXT_2; *fg = C_ON_ACCENT; break;
    case KIT_SECONDARY: *bg = C_SURFACE_2; *bg_pressed = C_SURFACE_3; *fg = C_ACCENT; break;
    case KIT_QUIET: *bg = C_SURFACE_2; *bg_pressed = C_SURFACE_3; *fg = C_TEXT; break;
    case KIT_DANGER: *bg = C_SURFACE_2; *bg_pressed = C_SURFACE_3; *fg = C_DANGER; break;
    }
}

lv_obj_t *kit_button(lv_obj_t *parent, kit_kind_t kind, const lv_font_t *icon_font, const char *icon,
                     const char *text, lv_event_cb_t cb, void *ud)
{
    uint32_t bg, bgp, fg;
    kind_colors(kind, &bg, &bgp, &fg);
    lv_obj_t *b = lv_obj_create(parent);
    plain(b);
    lv_obj_set_size(b, LV_PCT(100), BTN_H);
    lv_obj_set_style_radius(b, BTN_RADIUS, 0);
    lv_obj_set_style_bg_color(b, C(bg), 0);
    lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(b, C(bgp), LV_STATE_PRESSED);
    lv_obj_set_style_opa(b, LV_OPA_40, LV_STATE_DISABLED);
    lv_obj_set_style_pad_hor(b, SP_6, 0);
    lv_obj_set_flex_flow(b, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(b, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(b, SP_3, 0);
    lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
    if (cb) {
        lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, ud);
    }
    ui_focusable(b, false);
    lv_obj_t *i = kit_icon(b, icon_font ? icon_font : F.icon_s, icon ? icon : "", fg);
    if (!icon) {
        lv_obj_add_flag(i, LV_OBJ_FLAG_HIDDEN);
    }
    lv_obj_t *l = lv_label_create(b);
    lv_label_set_text(l, text);
    lv_label_set_long_mode(l, LV_LABEL_LONG_MODE_DOTS);
    lv_obj_set_style_max_width(l, UI_CARD_INNER_W - 60, 0);
    lv_obj_set_style_text_font(l, F.body_b, 0);
    lv_obj_set_style_text_color(l, C(fg), 0);
    return b;
}

void kit_button_set_text(lv_obj_t *btn, const char *text)
{
    lv_label_set_text(lv_obj_get_child(btn, 1), text);
}

void kit_button_set_enabled(lv_obj_t *btn, bool on)
{
    if (on) {
        lv_obj_remove_state(btn, LV_STATE_DISABLED);
    } else {
        lv_obj_add_state(btn, LV_STATE_DISABLED);
    }
}

lv_obj_t *kit_card_block(lv_obj_t *card, int32_t gap)
{
    lv_obj_t *b = kit_col(card, gap);
    lv_obj_set_width(b, LV_PCT(100));
    lv_obj_set_style_pad_hor(b, UI_CARD_PAD, 0);
    lv_obj_set_style_pad_ver(b, SP_3, 0);
    return b;
}

void kit_rule(lv_obj_t *card)
{
    lv_obj_t *h = ui_hairline(card);
    lv_obj_set_style_margin_left(h, UI_CARD_PAD, 0);
    lv_obj_set_width(h, UI_CARD_W - UI_CARD_PAD);
}

/* ---- rows ---- */

static lv_obj_t *settings_row(lv_obj_t *card)
{
    lv_obj_t *r = lv_obj_create(card);
    plain(r);
    lv_obj_set_size(r, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_style_min_height(r, UI_ROW_MIN, 0);
    lv_obj_set_style_pad_hor(r, UI_CARD_PAD, 0);
    lv_obj_set_style_pad_ver(r, SP_3, 0);
    lv_obj_set_style_pad_column(r, SP_4, 0);
    lv_obj_set_flex_flow(r, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(r, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    return r;
}

lv_obj_t *kit_value_row(lv_obj_t *card, const lv_font_t *icon_font, const char *icon, const char *label,
                        const char *value)
{
    lv_obj_t *r = settings_row(card);
    if (icon) {
        lv_obj_t *i = kit_icon(r, icon_font, icon, C_TEXT_2);
        lv_obj_set_width(i, 32);
        lv_obj_set_style_text_align(i, LV_TEXT_ALIGN_CENTER, 0);
    }
    lv_obj_t *l = ui_text(r, F.body, C_TEXT, label, 0, 0);
    lv_obj_set_width(l, LV_SIZE_CONTENT);
    lv_obj_t *v = lv_label_create(r);
    lv_label_set_long_mode(v, LV_LABEL_LONG_MODE_DOTS);
    lv_obj_set_flex_grow(v, 1);
    lv_obj_set_height(v, lv_font_get_line_height(F.body));
    lv_obj_set_style_text_align(v, LV_TEXT_ALIGN_RIGHT, 0);
    lv_obj_set_style_text_font(v, F.body, 0);
    lv_obj_set_style_text_color(v, C(C_TEXT_2), 0);
    lv_label_set_text(v, value ? value : "");
    return v;
}

static void switch_row_clicked(lv_event_t *e)
{
    lv_obj_t *sw = lv_event_get_user_data(e);
    if (lv_obj_has_state(sw, LV_STATE_DISABLED)) {
        return;
    }
    if (lv_obj_has_state(sw, LV_STATE_CHECKED)) {
        lv_obj_remove_state(sw, LV_STATE_CHECKED);
    } else {
        lv_obj_add_state(sw, LV_STATE_CHECKED);
    }
    lv_obj_send_event(sw, LV_EVENT_VALUE_CHANGED, NULL);
}

lv_obj_t *kit_switch_row(lv_obj_t *card, const char *title, const char *sub, bool on, lv_event_cb_t cb,
                         void *ud)
{
    lv_obj_t *r = settings_row(card);
    lv_obj_set_style_pad_ver(r, SP_4, 0);
    lv_obj_add_flag(r, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_style(r, &ST_PRESSED, LV_STATE_PRESSED);
    ui_focusable(r, false);
    const int32_t sw_w = 76, tw = UI_CARD_INNER_W - sw_w - SP_4;
    lv_obj_t *col = kit_col(r, SP_1);
    lv_obj_set_width(col, tw);
    ui_text(col, F.body, C_TEXT, title, tw, 2);
    if (sub && sub[0]) {
        ui_text(col, F.small, C_TEXT_2, sub, tw, 4);
    }
    lv_obj_t *sw = lv_switch_create(r);
    lv_obj_remove_flag(sw, LV_OBJ_FLAG_CLICKABLE); /* the row toggles it */
    lv_obj_set_size(sw, sw_w, 44);
    lv_obj_set_style_bg_color(sw, C(C_SURFACE_3), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(sw, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_bg_color(sw, C(C_ACCENT), LV_PART_INDICATOR | LV_STATE_CHECKED);
    lv_obj_set_style_bg_color(sw, C(C_TEXT), LV_PART_KNOB);
    lv_obj_set_style_pad_all(sw, -4, LV_PART_KNOB);
    lv_obj_set_style_anim_duration(sw, UI_ANIM_FAST, 0);
    if (on) {
        lv_obj_add_state(sw, LV_STATE_CHECKED);
    }
    if (cb) {
        lv_obj_add_event_cb(sw, cb, LV_EVENT_VALUE_CHANGED, ud);
    }
    lv_obj_add_event_cb(r, switch_row_clicked, LV_EVENT_CLICKED, sw);
    return sw;
}

int kit_choice_index(lv_obj_t *chip)
{
    return (int)lv_obj_get_index(chip);
}

lv_obj_t *kit_choice_row(lv_obj_t *card, const char *title, const char *const *labels, int n, int selected,
                         lv_event_cb_t cb, void *ud)
{
    lv_obj_t *wrap = lv_obj_create(card);
    plain(wrap);
    lv_obj_set_size(wrap, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_style_pad_hor(wrap, UI_CARD_PAD, 0);
    lv_obj_set_style_pad_top(wrap, SP_4, 0);
    lv_obj_set_style_pad_bottom(wrap, SP_4, 0);
    lv_obj_set_flex_flow(wrap, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(wrap, SP_3, 0);
    ui_text(wrap, F.body, C_TEXT, title, UI_CARD_INNER_W, 2);
    /* a segmented control: one rounded track, the chosen segment filled */
    lv_obj_t *track = lv_obj_create(wrap);
    plain(track);
    lv_obj_set_size(track, LV_PCT(100), UI_TOUCH_MIN + 8); /* segments of UI_TOUCH_MIN */
    lv_obj_set_style_radius(track, 22, 0);
    lv_obj_set_style_bg_color(track, C(C_SURFACE_2), 0);
    lv_obj_set_style_bg_opa(track, LV_OPA_COVER, 0);
    lv_obj_set_style_pad_all(track, 4, 0);
    lv_obj_set_style_pad_column(track, 4, 0);
    lv_obj_set_flex_flow(track, LV_FLEX_FLOW_ROW);
    for (int i = 0; i < n; i++) {
        lv_obj_t *c = lv_obj_create(track);
        plain(c);
        lv_obj_set_flex_grow(c, 1);
        lv_obj_set_height(c, LV_PCT(100));
        lv_obj_set_style_radius(c, 18, 0);
        const bool on = i == selected;
        lv_obj_set_style_bg_color(c, C(on ? C_ACCENT : C_SURFACE_2), 0);
        lv_obj_set_style_bg_opa(c, LV_OPA_COVER, 0);
        lv_obj_set_style_bg_color(c, C(on ? C_ACCENT : C_SURFACE_3), LV_STATE_PRESSED);
        lv_obj_add_flag(c, LV_OBJ_FLAG_CLICKABLE);
        ui_focusable(c, false);
        if (cb) {
            lv_obj_add_event_cb(c, cb, LV_EVENT_CLICKED, ud);
        }
        lv_obj_t *l = lv_label_create(c);
        lv_label_set_text(l, labels[i]);
        lv_obj_set_style_text_font(l, on ? F.small_b : F.small, 0);
        lv_obj_set_style_text_color(l, C(on ? C_ON_ACCENT : C_TEXT_2), 0);
        lv_obj_center(l);
    }
    return wrap;
}

/* ---- the state line ---- */

void kit_state_line(lv_obj_t *card, kit_state_t *s)
{
    s->box = lv_obj_create(card);
    plain(s->box);
    lv_obj_set_size(s->box, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_style_pad_hor(s->box, UI_CARD_PAD, 0);
    lv_obj_set_style_pad_ver(s->box, SP_4, 0);
    lv_obj_set_style_pad_column(s->box, SP_4, 0);
    lv_obj_set_flex_flow(s->box, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(s->box, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    s->ring = lv_obj_create(s->box);
    plain(s->ring);
    lv_obj_set_size(s->ring, 64, 64);
    lv_obj_set_style_radius(s->ring, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_opa(s->ring, LV_OPA_COVER, 0);
    s->icon = lv_label_create(s->ring);
    lv_label_set_text(s->icon, "");
    lv_obj_center(s->icon);
    const int32_t w = UI_CARD_INNER_W - 64 - SP_4;
    lv_obj_t *col = kit_col(s->box, 2);
    lv_obj_set_width(col, w);
    s->title = ui_text(col, F.body_b, C_TEXT, "", w, 2);
    s->sub = ui_text(col, F.small, C_TEXT_2, "", w, 3);
}

void kit_state_set(kit_state_t *s, const lv_font_t *icon_font, const char *icon, uint32_t color,
                   const char *title, const char *sub)
{
    lv_obj_set_style_bg_color(s->ring, lv_color_mix(C(color), C(C_BG), LV_OPA_20), 0);
    lv_label_set_text(s->icon, icon);
    lv_obj_set_style_text_font(s->icon, icon_font, 0);
    lv_obj_set_style_text_color(s->icon, C(color), 0);
    ui_text_set(s->title, title);
    ui_text_set(s->sub, sub);
    ui_show(s->sub, sub && sub[0]);
}

/* ---- the confirm sheet ---- */

typedef struct {
    char title[96], text[240], yes[48];
    const lv_font_t *icon_font;
    const char *icon;
    bool danger;
    void (*on_yes)(void);
} confirm_t;

static void confirm_yes(lv_event_t *e)
{
    confirm_t *c = lv_event_get_user_data(e);
    void (*fn)(void) = c->on_yes;
    ui_sheet_close(); /* frees c */
    if (fn) {
        fn();
    }
}

static void confirm_no(lv_event_t *e)
{
    (void)e;
    ui_sheet_close();
}

static void confirm_build(lv_obj_t *s, void *arg)
{
    confirm_t *c = arg;
    const int32_t w = UI_W - 2 * (UI_MARGIN + SP_2);
    if (c->icon) {
        lv_obj_t *ring = lv_obj_create(s);
        plain(ring);
        lv_obj_set_size(ring, 88, 88);
        lv_obj_set_style_radius(ring, LV_RADIUS_CIRCLE, 0);
        const uint32_t col = c->danger ? C_DANGER : C_ACCENT;
        lv_obj_set_style_bg_color(ring, lv_color_mix(C(col), C(C_BG), LV_OPA_20), 0);
        lv_obj_set_style_bg_opa(ring, LV_OPA_COVER, 0);
        lv_obj_center(kit_icon(ring, c->icon_font, c->icon, col));
    }
    lv_obj_t *t = ui_text(s, F.title, C_TEXT, c->title, w, 3);
    lv_obj_set_style_text_line_space(t, 0, 0);
    ui_text(s, F.body, C_TEXT_2, c->text, w, 6);
    lv_obj_t *col = kit_col(s, SP_3);
    lv_obj_set_width(col, LV_PCT(100));
    lv_obj_set_style_margin_top(col, SP_4, 0);
    lv_obj_t *yes = kit_button(col, c->danger ? KIT_DANGER : KIT_PRIMARY, NULL, NULL, c->yes, confirm_yes, c);
    if (c->danger) {
        lv_obj_set_style_bg_color(yes, lv_color_mix(C(C_DANGER), C(C_BG), LV_OPA_20), 0);
    }
    kit_button(col, KIT_QUIET, NULL, NULL, "Cancel", confirm_no, NULL);
}

void kit_confirm(const char *title, const char *text, const lv_font_t *icon_font, const char *icon,
                 const char *yes, bool danger, void (*on_yes)(void))
{
    confirm_t *c = calloc(1, sizeof(*c));
    if (!c) {
        return;
    }
    snprintf(c->title, sizeof(c->title), "%s", title);
    snprintf(c->text, sizeof(c->text), "%s", text);
    snprintf(c->yes, sizeof(c->yes), "%s", yes);
    c->icon_font = icon_font;
    c->icon = icon;
    c->danger = danger;
    c->on_yes = on_yes;
    ui_sheet_open(confirm_build, c, free);
}

/* ---- hold to confirm ---- */

typedef struct {
    lv_obj_t *btn, *fill;
    uint32_t hold_ms;
    void (*on_done)(void);
    bool done;
} hold_t;

static void fill_w(void *var, int32_t v)
{
    lv_obj_set_width(var, v);
}

static void hold_complete(lv_anim_t *a)
{
    hold_t *h = lv_anim_get_user_data(a);
    if (h->done) {
        return;
    }
    h->done = true;
    if (h->on_done) {
        h->on_done();
    }
}

static void hold_event(lv_event_t *e)
{
    hold_t *h = lv_event_get_user_data(e);
    const lv_event_code_t code = lv_event_get_code(e);
    const int32_t full = lv_obj_get_width(h->btn);
    lv_anim_delete(h->fill, fill_w);
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, h->fill);
    lv_anim_set_exec_cb(&a, fill_w);
    lv_anim_set_user_data(&a, h);
    if (code == LV_EVENT_PRESSED) {
        h->done = false;
        lv_anim_set_values(&a, lv_obj_get_width(h->fill), full);
        lv_anim_set_duration(&a, h->hold_ms);
        lv_anim_set_path_cb(&a, lv_anim_path_linear);
        lv_anim_set_completed_cb(&a, hold_complete);
        lv_anim_start(&a);
    } else if (code == LV_EVENT_RELEASED || code == LV_EVENT_PRESS_LOST || code == LV_EVENT_INDEV_RESET) {
        if (!h->done && code != LV_EVENT_INDEV_RESET) {
            ui_toast(UI_TOAST_INFO, "Keep holding to wipe the cache");
        }
        lv_anim_set_values(&a, lv_obj_get_width(h->fill), 0);
        lv_anim_set_duration(&a, UI_ANIM);
        lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
        lv_anim_start(&a);
    } else if (code == LV_EVENT_DELETE) {
        lv_anim_delete(h->fill, fill_w);
        free(h);
    }
}

lv_obj_t *kit_hold_button(lv_obj_t *parent, const lv_font_t *icon_font, const char *icon, const char *text,
                          uint32_t hold_ms, void (*on_done)(void))
{
    hold_t *h = calloc(1, sizeof(*h));
    lv_obj_t *b = kit_button(parent, KIT_DANGER, icon_font, icon, text, NULL, NULL);
    if (!h) {
        return b;
    }
    h->btn = b;
    h->hold_ms = hold_ms;
    h->on_done = on_done;
    lv_obj_set_style_clip_corner(b, true, 0);
    /* the fill: behind the label, out of the flex layout */
    h->fill = lv_obj_create(b);
    plain(h->fill);
    lv_obj_add_flag(h->fill, LV_OBJ_FLAG_FLOATING);
    lv_obj_set_pos(h->fill, -SP_6, 0); /* from the button's edge, not its padding */
    lv_obj_set_size(h->fill, 0, LV_PCT(100));
    lv_obj_set_style_bg_color(h->fill, lv_color_mix(C(C_DANGER), C(C_BG), LV_OPA_40), 0);
    lv_obj_set_style_bg_opa(h->fill, LV_OPA_COVER, 0);
    lv_obj_move_to_index(h->fill, 0);
    lv_obj_add_event_cb(b, hold_event, LV_EVENT_PRESSED, h);
    lv_obj_add_event_cb(b, hold_event, LV_EVENT_RELEASED, h);
    lv_obj_add_event_cb(b, hold_event, LV_EVENT_PRESS_LOST, h);
    lv_obj_add_event_cb(b, hold_event, LV_EVENT_INDEV_RESET, h); /* its page left (ui_leaving) */
    lv_obj_add_event_cb(b, hold_event, LV_EVENT_DELETE, h);
    return b;
}

/* ---- initials ---- */

lv_obj_t *kit_avatar(lv_obj_t *parent, const char *name, int32_t size)
{
    lv_obj_t *a = lv_obj_create(parent);
    plain(a);
    lv_obj_set_size(a, size, size);
    lv_obj_set_style_radius(a, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(a, C(C_SURFACE_3), 0);
    lv_obj_set_style_bg_opa(a, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(a, C(C_SURFACE), 0);
    lv_obj_set_style_border_width(a, 3, 0);
    char ini[12];
    talks_initials(name, ini, sizeof(ini));
    lv_obj_t *l = lv_label_create(a);
    lv_label_set_text(l, ini);
    lv_obj_set_style_text_font(l, size >= 48 ? F.small_b : F.caption, 0);
    lv_obj_set_style_text_color(l, C(C_TEXT), 0);
    lv_obj_center(l);
    return a;
}
