#include "controls.h"

#include <stdio.h>

#include "theme.h"
#include "ui.h"

#define DIMMED LV_STATE_USER_1

static struct {
    bool init;
    lv_style_t primary, secondary, dimmed, pressed, choice, choice_on, choice_pressed;
    lv_style_transition_dsc_t tr;
} S;

static const lv_style_prop_t TR_PROPS[] = {LV_STYLE_BG_COLOR, LV_STYLE_TEXT_COLOR, LV_STYLE_BORDER_COLOR,
                                           LV_STYLE_TRANSFORM_SCALE_X, LV_STYLE_TRANSFORM_SCALE_Y, 0};

static void styles(void)
{
    if (S.init) {
        return;
    }
    S.init = true;
    lv_style_transition_dsc_init(&S.tr, TR_PROPS, lv_anim_path_ease_out, UI_ANIM_FAST, 0, NULL);

    lv_style_init(&S.primary);
    lv_style_set_bg_color(&S.primary, C(C_ACCENT));
    lv_style_set_bg_opa(&S.primary, LV_OPA_COVER);
    lv_style_set_text_color(&S.primary, C(C_ON_ACCENT));
    lv_style_set_radius(&S.primary, 28);
    lv_style_set_transition(&S.primary, &S.tr);

    lv_style_init(&S.secondary);
    lv_style_set_bg_color(&S.secondary, C(C_SURFACE_2));
    lv_style_set_bg_opa(&S.secondary, LV_OPA_COVER);
    lv_style_set_text_color(&S.secondary, C(C_ACCENT));
    lv_style_set_radius(&S.secondary, 28);
    lv_style_set_transition(&S.secondary, &S.tr);

    lv_style_init(&S.dimmed);
    lv_style_set_bg_color(&S.dimmed, C(C_SURFACE_2));
    lv_style_set_text_color(&S.dimmed, C(C_TEXT_3));

    lv_style_init(&S.pressed);
    lv_style_set_transform_scale_x(&S.pressed, 248);
    lv_style_set_transform_scale_y(&S.pressed, 248);
    lv_style_set_bg_color(&S.pressed, C(C_SURFACE_3));

    lv_style_init(&S.choice);
    lv_style_set_bg_color(&S.choice, C(C_SURFACE_2));
    lv_style_set_bg_opa(&S.choice, LV_OPA_COVER);
    lv_style_set_text_color(&S.choice, C(C_TEXT));
    lv_style_set_border_color(&S.choice, C(C_SURFACE_3));
    lv_style_set_border_width(&S.choice, 1);
    lv_style_set_radius(&S.choice, 20);
    lv_style_set_transition(&S.choice, &S.tr);

    lv_style_init(&S.choice_on);
    lv_style_set_bg_color(&S.choice_on, C(C_ACCENT));
    lv_style_set_border_color(&S.choice_on, C(C_ACCENT));
    lv_style_set_text_color(&S.choice_on, C(C_ON_ACCENT));

    lv_style_init(&S.choice_pressed);
    lv_style_set_transform_scale_x(&S.choice_pressed, 244);
    lv_style_set_transform_scale_y(&S.choice_pressed, 244);
}

static void plain(lv_obj_t *o)
{
    lv_obj_remove_style_all(o);
    lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE);
}

static void pivot_mid(lv_obj_t *o)
{
    lv_obj_set_style_transform_pivot_x(o, LV_PCT(50), 0);
    lv_obj_set_style_transform_pivot_y(o, LV_PCT(50), 0);
}

lv_obj_t *ui_button(lv_obj_t *parent, const char *icon, const char *text, ui_button_kind_t kind,
                    lv_event_cb_t cb, void *user_data)
{
    styles();
    lv_obj_t *b = lv_obj_create(parent);
    plain(b);
    lv_obj_set_size(b, LV_PCT(100), UI_BUTTON_H);
    lv_obj_add_style(b, kind == UI_BUTTON_PRIMARY ? &S.primary : &S.secondary, 0);
    lv_obj_add_style(b, &S.dimmed, DIMMED);
    lv_obj_add_style(b, &S.pressed, LV_STATE_PRESSED);
    if (kind == UI_BUTTON_PRIMARY) {
        lv_obj_set_style_bg_color(b, lv_color_mix(C(C_ACCENT), C(C_BG), LV_OPA_80), LV_STATE_PRESSED);
    }
    pivot_mid(b);
    lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
    if (cb) {
        lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, user_data);
    }
    ui_focusable(b, false);
    lv_obj_t *l = lv_label_create(b);
    lv_obj_set_style_text_font(l, F.body_b, 0);
    lv_label_set_long_mode(l, LV_LABEL_LONG_MODE_DOTS);
    lv_obj_set_style_max_width(l, UI_CARD_W - 2 * SP_6, 0);
    lv_obj_center(l);
    ui_button_set_text(b, icon, text);
    return b;
}

void ui_button_set_text(lv_obj_t *button, const char *icon, const char *text)
{
    char buf[160];
    snprintf(buf, sizeof(buf), "%s%s%s", icon ? icon : "", icon ? "  " : "", text ? text : "");
    lv_label_set_text(lv_obj_get_child(button, 0), buf);
}

void ui_button_set_enabled(lv_obj_t *button, bool on)
{
    if (on) {
        lv_obj_remove_state(button, DIMMED);
    } else {
        lv_obj_add_state(button, DIMMED);
    }
}

bool ui_button_enabled(lv_obj_t *button)
{
    return !lv_obj_has_state(button, DIMMED);
}

lv_obj_t *ui_choice(lv_obj_t *parent, const char *text, int32_t w, int32_t h, lv_event_cb_t cb,
                    void *user_data)
{
    styles();
    lv_obj_t *t = lv_obj_create(parent);
    plain(t);
    lv_obj_set_size(t, w, h);
    lv_obj_set_style_min_height(t, UI_TOUCH_MIN, 0);
    lv_obj_add_style(t, &S.choice, 0);
    lv_obj_add_style(t, &S.choice_on, LV_STATE_CHECKED);
    lv_obj_add_style(t, &S.choice_pressed, LV_STATE_PRESSED);
    pivot_mid(t);
    lv_obj_set_style_pad_hor(t, LV_COORD_IS_PX(w) ? SP_2 : SP_5, 0);
    lv_obj_set_flex_flow(t, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(t, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_row(t, 0, 0);
    lv_obj_add_flag(t, LV_OBJ_FLAG_CLICKABLE);
    if (cb) {
        lv_obj_add_event_cb(t, cb, LV_EVENT_CLICKED, user_data);
    }
    ui_focusable(t, false);
    if (text) {
        lv_obj_t *l = lv_label_create(t);
        lv_obj_set_style_text_font(l, F.small_b, 0);
        if (LV_COORD_IS_PX(w)) {
            lv_label_set_long_mode(l, LV_LABEL_LONG_MODE_DOTS);
            lv_obj_set_width(l, LV_SIZE_CONTENT);
            lv_obj_set_style_max_width(l, w - 2 * SP_2, 0);
        } else {
            lv_obj_set_style_max_width(l, UI_CARD_INNER_W - 2 * SP_5, 0);
        }
        lv_label_set_text(l, text);
    }
    return t;
}

void ui_choice_set(lv_obj_t *tile, bool selected)
{
    if (selected) {
        lv_obj_add_state(tile, LV_STATE_CHECKED);
    } else {
        lv_obj_remove_state(tile, LV_STATE_CHECKED);
    }
}

lv_obj_t *ui_fab(lv_obj_t *page, lv_event_cb_t cb, void *user_data)
{
    styles();
    lv_obj_t *b = lv_obj_create(page);
    plain(b);
    lv_obj_set_size(b, UI_FAB_D, UI_FAB_D);
    lv_obj_add_style(b, &S.primary, 0);
    lv_obj_set_style_radius(b, LV_RADIUS_CIRCLE, 0);
    lv_obj_add_style(b, &S.pressed, LV_STATE_PRESSED);
    lv_obj_set_style_bg_color(b, lv_color_mix(C(C_ACCENT), C(C_BG), LV_OPA_80), LV_STATE_PRESSED);
    lv_obj_set_style_shadow_width(b, 48, 0);
    lv_obj_set_style_shadow_offset_y(b, 8, 0);
    lv_obj_set_style_shadow_color(b, C(C_BG), 0);
    lv_obj_set_style_shadow_opa(b, LV_OPA_COVER, 0);
    /* a black ring cuts it out of the cards scrolling under it */
    lv_obj_set_style_outline_width(b, 6, 0);
    lv_obj_set_style_outline_color(b, C(C_BG), 0);
    lv_obj_set_style_outline_opa(b, LV_OPA_COVER, 0);
    pivot_mid(b);
    lv_obj_align(b, LV_ALIGN_BOTTOM_RIGHT, -UI_MARGIN - SP_1, -UI_MARGIN - SP_1);
    lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
    if (cb) {
        lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, user_data);
    }
    ui_focusable(b, false);
    /* the plus: two rounded bars, crisper than a glyph */
    for (int i = 0; i < 2; i++) {
        lv_obj_t *bar = lv_obj_create(b);
        plain(bar);
        lv_obj_remove_flag(bar, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_size(bar, i ? 6 : 40, i ? 40 : 6);
        lv_obj_set_style_radius(bar, 3, 0);
        lv_obj_set_style_bg_color(bar, C(C_ON_ACCENT), 0);
        lv_obj_set_style_bg_opa(bar, LV_OPA_COVER, 0);
        lv_obj_center(bar);
    }
    return b;
}
