/* Hands-free on screen: the focus ring, the hand cursor and the hold ring, driven by
 * core/handsfree.c's commands (the plan's gesture table). */
#include <math.h>
#include <string.h>

#include "app_state.h"
#include "handsfree.h"
#include "screens.h"
#include "theme.h"
#include "ui_internal.h"

#define FOCUSABLE LV_OBJ_FLAG_USER_1
#define TICKABLE LV_OBJ_FLAG_USER_2
#define RING_OUT 6     /* the ring sits this far outside the focused object */
#define CURSOR_D 76
#define HOLD_D (UI_TICK + 28) /* round the tick */
#define HOLD_W 6

static struct {
    hf_state_t st;
    lv_obj_t *ring, *cursor, *cursor_icon, *hold;
    lv_obj_t *focused;
    lv_area_t ring_at; /* where the ring is drawn now (it glides to the focused object) */
    bool ring_shown;
} HF;

static void plain(lv_obj_t *o)
{
    lv_obj_remove_style_all(o);
    lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
}

static bool enabled(void)
{
    return app_settings()->hands_free;
}

void ui_focusable(lv_obj_t *obj, bool tick)
{
    lv_obj_add_flag(obj, FOCUSABLE);
    if (tick) {
        lv_obj_add_flag(obj, TICKABLE);
    }
}

static void cursor_dim(bool dim);

static void focused_deleted(lv_event_t *e)
{
    if (lv_event_get_target(e) == HF.focused) {
        HF.focused = NULL;
    }
}

static void set_focus(lv_obj_t *o)
{
    if (o == HF.focused) {
        return;
    }
    if (HF.focused) {
        lv_obj_remove_event_cb_with_user_data(HF.focused, focused_deleted, NULL);
    }
    HF.focused = o;
    hf_cmd_t c;
    if (hf_focus_changed(&HF.st, &c)) {
        lv_obj_add_flag(HF.hold, LV_OBJ_FLAG_HIDDEN);
        cursor_dim(false);
    }
    if (o) {
        lv_obj_add_event_cb(o, focused_deleted, LV_EVENT_DELETE, NULL);
    }
}

void hf_ui_reset_focus(void)
{
    set_focus(NULL);
}

/* The hold ring goes round the focused to-do's tick (its left column's first child, as
 * widgets.c todo_row builds it), so the item's text stays readable while it fills. */
static void place_hold(void)
{
    if (!HF.focused) {
        return;
    }
    lv_area_t a;
    lv_obj_t *left = lv_obj_get_child(HF.focused, 0);
    lv_obj_t *tick = left ? lv_obj_get_child(left, 0) : NULL;
    if (tick) {
        lv_obj_get_coords(tick, &a);
    } else {
        lv_obj_get_coords(HF.focused, &a);
        a.x1 += UI_CARD_PAD;
        a.x2 = a.x1 + UI_TICK;
    }
    const int32_t cx = (a.x1 + a.x2) / 2, cy = (a.y1 + a.y2) / 2;
    lv_obj_set_pos(HF.hold, cx - HOLD_D / 2, cy - HOLD_D / 2);
}

/* the hand cursor steps back while a hold fills (it sits on the text) */
static void cursor_dim(bool dim)
{
    lv_obj_set_style_opa(HF.cursor, dim ? LV_OPA_40 : LV_OPA_COVER, 0);
}

/* the ring glides to its target: a third of the way per frame (~150 ms to settle) */
static int32_t glide(int32_t from, int32_t to)
{
    const int32_t d = to - from;
    return d > -2 && d < 2 ? to : from + d / 3;
}

static void frame(lv_timer_t *t)
{
    (void)t;
    hf_cmd_t c;
    if (hf_tick(&HF.st, app_mono_ms(), &c)) {
        lv_obj_add_flag(HF.hold, LV_OBJ_FLAG_HIDDEN);
        cursor_dim(false);
        if (HF.focused && lv_obj_has_flag(HF.focused, TICKABLE)) {
            lv_obj_send_event(HF.focused, LV_EVENT_CLICKED, NULL);
        }
    }
    if (HF.st.holding) {
        lv_arc_set_value(HF.hold, (int32_t)(hf_hold_progress(&HF.st, app_mono_ms()) * 100));
        place_hold();
    }
    const bool want = enabled() && HF.focused && lv_obj_is_visible(HF.focused);
    if (!want) {
        lv_obj_add_flag(HF.ring, LV_OBJ_FLAG_HIDDEN);
        HF.ring_shown = false;
        return;
    }
    lv_area_t a;
    lv_obj_get_coords(HF.focused, &a);
    a.x1 -= RING_OUT;
    a.y1 -= RING_OUT;
    a.x2 += RING_OUT;
    a.y2 += RING_OUT;
    if (!HF.ring_shown) {
        HF.ring_at = a;
        HF.ring_shown = true;
        lv_obj_remove_flag(HF.ring, LV_OBJ_FLAG_HIDDEN);
    } else {
        HF.ring_at.x1 = glide(HF.ring_at.x1, a.x1);
        HF.ring_at.y1 = glide(HF.ring_at.y1, a.y1);
        HF.ring_at.x2 = glide(HF.ring_at.x2, a.x2);
        HF.ring_at.y2 = glide(HF.ring_at.y2, a.y2);
    }
    lv_obj_set_pos(HF.ring, HF.ring_at.x1, HF.ring_at.y1);
    lv_obj_set_size(HF.ring, lv_area_get_width(&HF.ring_at), lv_area_get_height(&HF.ring_at));
}

static void cursor_x(void *v, int32_t x)
{
    lv_obj_set_x(v, x);
}

static void cursor_y(void *v, int32_t y)
{
    lv_obj_set_y(v, y);
}

static void move_cursor(double x, double y, vision_shape_t shape)
{
    const int32_t px = (int32_t)lround(x * UI_W) - CURSOR_D / 2;
    const int32_t py = (int32_t)lround(y * UI_H) - CURSOR_D / 2;
    const bool was_hidden = lv_obj_has_flag(HF.cursor, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(HF.cursor, LV_OBJ_FLAG_HIDDEN);
    const bool thumb = shape == VIS_SHAPE_THUMBS_UP;
    lv_obj_set_style_border_color(HF.cursor, C(thumb ? C_ACCENT : C_TEXT), 0);
    lv_obj_set_style_text_color(HF.cursor_icon, C(thumb ? C_ACCENT : C_TEXT), 0);
    lv_label_set_text(HF.cursor_icon, shape == VIS_SHAPE_THUMBS_UP ? ICON_CHECK : ICON_HAND);
    lv_anim_delete(HF.cursor, cursor_x);
    lv_anim_delete(HF.cursor, cursor_y);
    if (was_hidden) {
        lv_obj_set_pos(HF.cursor, px, py);
        return;
    }
    /* hand updates come at ~10 Hz: glide between them */
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, HF.cursor);
    lv_anim_set_duration(&a, 100);
    lv_anim_set_exec_cb(&a, cursor_x);
    lv_anim_set_values(&a, lv_obj_get_x(HF.cursor), px);
    lv_anim_start(&a);
    lv_anim_set_exec_cb(&a, cursor_y);
    lv_anim_set_values(&a, lv_obj_get_y(HF.cursor), py);
    lv_anim_start(&a);
}

static void focus_at(double x, double y)
{
    lv_point_t p = {(int32_t)lround(x * UI_W), (int32_t)lround(y * UI_H)};
    lv_obj_t *o = lv_indev_search_obj(lv_screen_active(), &p);
    while (o && !lv_obj_has_flag(o, FOCUSABLE)) {
        o = lv_obj_get_parent(o);
    }
    set_focus(o);
}

static void page(int dir)
{
    lv_obj_t *s = ui_active_scroller();
    if (s) {
        lv_obj_scroll_by_bounded(s, 0, -dir * lv_obj_get_height(s) * 3 / 4, LV_ANIM_ON);
    }
}

static void run(const hf_cmd_t *c)
{
    switch (c->kind) {
    case HF_NONE: break;
    case HF_NEXT: ui_nav_next(); break;
    case HF_PREV: ui_nav_prev(); break;
    case HF_PAGE_DOWN: page(1); break;
    case HF_PAGE_UP: page(-1); break;
    case HF_FOCUS_AT: focus_at(c->x, c->y); break;
    case HF_HOLD_START:
        lv_arc_set_value(HF.hold, 0);
        lv_obj_remove_flag(HF.hold, LV_OBJ_FLAG_HIDDEN);
        place_hold();
        cursor_dim(true);
        break;
    case HF_HOLD_CANCEL:
        lv_obj_add_flag(HF.hold, LV_OBJ_FLAG_HIDDEN);
        cursor_dim(false);
        break;
    case HF_HOLD_COMMIT: break; /* frame() commits */
    case HF_HOME:
        ui_screen_on();
        ui_show_tab(UI_TAB_TODAY, true);
        break;
    case HF_SCREEN_OFF: ui_screen_off(); break;
    case HF_CURSOR: move_cursor(c->x, c->y, c->shape); break;
    case HF_CURSOR_HIDE: lv_obj_add_flag(HF.cursor, LV_OBJ_FLAG_HIDDEN); break;
    }
}

void ui_vision_event(const vision_event_t *ev)
{
    if (enabled() && (ev->type == VIS_GESTURE || ev->type == VIS_HAND)) {
        ambient_ui_poke(); /* a gesture is activity, like a touch */
    }
    /* the pairing scan's QR codes; the gesture tester (screen_status.c) */
    if (pairing_vision_event(ev) || status_vision_event(ev)) {
        return;
    }
    if (!enabled()) {
        return;
    }
    if (ui_screen_is_off() && !(ev->type == VIS_GESTURE && ev->g == VIS_G_PALM)) {
        return; /* only an open palm wakes it (or a touch, or presence) */
    }
    const bool todo = HF.focused && lv_obj_has_flag(HF.focused, TICKABLE);
    hf_cmd_t cmds[HF_CMDS_MAX];
    const int n = hf_on_event(&HF.st, ev, todo, app_mono_ms(), cmds);
    for (int i = 0; i < n; i++) {
        run(&cmds[i]);
    }
}

void hf_ui_settings_changed(void)
{
    if (!enabled()) {
        hf_reset(&HF.st);
        lv_obj_add_flag(HF.cursor, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(HF.hold, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(HF.ring, LV_OBJ_FLAG_HIDDEN);
        cursor_dim(false);
        set_focus(NULL);
    }
}

void hf_ui_create(void)
{
    hf_reset(&HF.st);
    lv_obj_t *top = lv_layer_top();

    HF.ring = lv_obj_create(top);
    plain(HF.ring);
    lv_obj_set_style_radius(HF.ring, 22, 0);
    lv_obj_set_style_border_color(HF.ring, C(C_ACCENT), 0);
    lv_obj_set_style_border_width(HF.ring, 4, 0);
    /* a soft second ring outside the first */
    lv_obj_set_style_outline_color(HF.ring, C(C_ACCENT), 0);
    lv_obj_set_style_outline_width(HF.ring, 6, 0);
    lv_obj_set_style_outline_opa(HF.ring, LV_OPA_20, 0);
    lv_obj_add_flag(HF.ring, LV_OBJ_FLAG_HIDDEN);

    HF.hold = lv_arc_create(top);
    lv_obj_remove_flag(HF.hold, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(HF.hold, HOLD_D, HOLD_D);
    lv_arc_set_rotation(HF.hold, 270);
    lv_arc_set_bg_angles(HF.hold, 0, 360);
    lv_arc_set_range(HF.hold, 0, 100);
    lv_arc_set_value(HF.hold, 0);
    lv_obj_set_style_arc_width(HF.hold, HOLD_W, LV_PART_MAIN);
    lv_obj_set_style_arc_color(HF.hold, C(C_SURFACE_3), LV_PART_MAIN);
    lv_obj_set_style_arc_width(HF.hold, HOLD_W, LV_PART_INDICATOR);
    lv_obj_set_style_arc_color(HF.hold, C(C_ACCENT), LV_PART_INDICATOR);
    lv_obj_set_style_arc_rounded(HF.hold, true, LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(HF.hold, LV_OPA_TRANSP, LV_PART_KNOB);
    lv_obj_set_style_pad_all(HF.hold, 0, LV_PART_KNOB);
    lv_obj_add_flag(HF.hold, LV_OBJ_FLAG_HIDDEN);

    HF.cursor = lv_obj_create(top);
    plain(HF.cursor);
    lv_obj_set_size(HF.cursor, CURSOR_D, CURSOR_D);
    lv_obj_set_style_radius(HF.cursor, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(HF.cursor, C(C_TEXT), 0);
    lv_obj_set_style_bg_opa(HF.cursor, LV_OPA_20, 0);
    lv_obj_set_style_border_width(HF.cursor, 3, 0);
    lv_obj_set_style_border_color(HF.cursor, C(C_TEXT), 0);
    lv_obj_set_style_border_opa(HF.cursor, LV_OPA_80, 0);
    HF.cursor_icon = lv_label_create(HF.cursor);
    lv_label_set_text(HF.cursor_icon, ICON_HAND);
    lv_obj_set_style_text_font(HF.cursor_icon, F.icon, 0);
    lv_obj_center(HF.cursor_icon);
    lv_obj_add_flag(HF.cursor, LV_OBJ_FLAG_HIDDEN);

    lv_timer_create(frame, 33, NULL);
}
