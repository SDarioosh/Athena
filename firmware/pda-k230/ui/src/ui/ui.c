#include "ui.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "app_state.h"
#include "local_msg.h"
#include "theme.h"
#include "ui_internal.h"

#define DETAIL_DEPTH 4

static struct {
    lv_obj_t *scr, *content;
    lv_obj_t *pages[UI_TABS];
    bool built[UI_TABS];
    ui_tab_t tab;
    struct {
        lv_obj_t *layer, *page;
        const ui_detail_t *d;
        void *arg;
    } detail[DETAIL_DEPTH];
    int depth;
    /* the sheet */
    lv_obj_t *scrim, *sheet;
    void (*sheet_destroy)(void *);
    void *sheet_arg;
    /* the toast */
    lv_obj_t *toast, *toast_icon, *toast_text;
    lv_timer_t *toast_timer;
    /* screen off */
    lv_obj_t *off;
    unsigned dirty;
    int64_t minute;
    ui_hooks_t hooks;
} U;

static void plain(lv_obj_t *o)
{
    lv_obj_remove_style_all(o);
    lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE);
}

/* ---- updates ---- */

static void flush(void)
{
    const unsigned what = U.dirty;
    if (!what) {
        return;
    }
    U.dirty = 0;
    for (int t = 0; t < UI_TABS; t++) {
        if (U.built[t] && UI_SCREENS[t]->update) {
            UI_SCREENS[t]->update(U.pages[t], what);
        }
    }
    for (int i = 0; i < U.depth; i++) {
        if (U.detail[i].d->update) {
            U.detail[i].d->update(U.detail[i].page, U.detail[i].arg, what);
        }
    }
    chrome_update(what);
}

static void flush_timer(lv_timer_t *t)
{
    (void)t;
    const int64_t m = app_now() / 60;
    if (m != U.minute) {
        U.minute = m;
        U.dirty |= UI_DIRTY_CLOCK;
    }
    flush();
}

void ui_refresh_now(void)
{
    U.dirty |= UI_DIRTY_ALL & ~UI_DIRTY_SHOWN;
    flush();
}

static void on_app(void *ctx, app_ev_t ev, int32_t arg)
{
    (void)ctx;
    switch (ev) {
    case APP_EV_FEED: U.dirty |= UI_DIRTY_FEED | UI_DIRTY_OUTBOX; break;
    case APP_EV_OUTBOX: U.dirty |= UI_DIRTY_OUTBOX; break;
    case APP_EV_FAILED: {
        const app_failed_t *f;
        const int n = app_failed(&f);
        if (arg >= 0 && arg < n) {
            ui_toast(UI_TOAST_ERROR, "Couldn't sync \xe2\x80\x9c%s\xe2\x80\x9d", f[arg].label);
        }
        U.dirty |= UI_DIRTY_OUTBOX;
        break;
    }
    case APP_EV_DETAIL: U.dirty |= UI_DIRTY_DETAIL; break;
    case APP_EV_CLOCK: U.dirty |= UI_DIRTY_CLOCK; break;
    case APP_EV_LINK:
    case APP_EV_NET:
    case APP_EV_TAILNET:
    case APP_EV_PAIR_RESULT: U.dirty |= UI_DIRTY_NET; break;
    case APP_EV_VISION: {
        U.dirty |= UI_DIRTY_VISION;
        static bool was_present;
        const app_vision_t *v = app_vision();
        if (app_settings()->presence_wake && v->present != was_present) {
            if (v->present) {
                ui_screen_on();
            } else {
                ui_screen_off();
            }
        }
        was_present = v->present;
        break;
    }
    case APP_EV_SETTINGS:
        U.dirty |= UI_DIRTY_SETTINGS;
        hf_ui_settings_changed();
        if (U.hooks.brightness) {
            U.hooks.brightness(app_settings()->brightness);
        }
        break;
    case APP_EV_POWER: U.dirty |= UI_DIRTY_POWER; break;
    }
}

/* ---- tabs ---- */

static void build_page(ui_tab_t t)
{
    if (!U.built[t]) {
        U.built[t] = true;
        UI_SCREENS[t]->build(U.pages[t]);
        if (UI_SCREENS[t]->update) {
            UI_SCREENS[t]->update(U.pages[t], UI_DIRTY_ALL);
        }
    }
}

static void slide_x(void *var, int32_t v)
{
    lv_obj_set_x(var, v);
}

/* a page that slid out is hidden and put back (its coords lag a frame: use the target) */
static void slide_done(lv_anim_t *a)
{
    lv_obj_t *o = a->var;
    if (lv_anim_get_user_data(a)) {
        lv_obj_add_flag(o, LV_OBJ_FLAG_HIDDEN);
    }
    lv_obj_set_x(o, 0);
}

static void slide(lv_obj_t *o, int32_t from, int32_t to, uint32_t ms)
{
    lv_anim_delete(o, slide_x);
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, o);
    lv_anim_set_exec_cb(&a, slide_x);
    lv_anim_set_values(&a, from, to);
    lv_anim_set_duration(&a, ms);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
    lv_anim_set_completed_cb(&a, slide_done);
    lv_anim_set_user_data(&a, (void *)(intptr_t)(to != 0));
    lv_obj_set_x(o, from);
    lv_anim_start(&a);
}

void ui_show_tab(ui_tab_t tab, bool animate)
{
    if ((unsigned)tab >= UI_TABS) {
        return;
    }
    while (U.depth > 0) {
        ui_pop();
    }
    ui_sheet_close();
    build_page(tab);
    const ui_tab_t old = U.tab;
    U.tab = tab;
    chrome_set_tab(tab, animate);
    hf_ui_reset_focus();
    if (old == tab) {
        lv_obj_remove_flag(U.pages[tab], LV_OBJ_FLAG_HIDDEN);
        return;
    }
    lv_obj_t *in = U.pages[tab], *out = U.pages[old];
    lv_obj_remove_flag(in, LV_OBJ_FLAG_HIDDEN);
    if (UI_SCREENS[tab]->update) {
        UI_SCREENS[tab]->update(in, UI_DIRTY_SHOWN);
    }
    if (!animate) {
        lv_anim_delete(in, slide_x);
        lv_anim_delete(out, slide_x);
        lv_obj_set_x(in, 0);
        lv_obj_add_flag(out, LV_OBJ_FLAG_HIDDEN);
        lv_obj_set_x(out, 0);
        return;
    }
    const int dir = tab > old ? 1 : -1; /* a tab to the right comes in from the right */
    slide(in, dir * UI_W, 0, UI_ANIM);
    slide(out, 0, -dir * UI_W, UI_ANIM);
}

ui_tab_t ui_current_tab(void)
{
    return U.tab;
}

bool ui_tab_visible(ui_tab_t tab)
{
    return tab == U.tab && U.depth == 0;
}

lv_obj_t *ui_tab_page(ui_tab_t tab)
{
    build_page(tab);
    return U.pages[tab];
}

void ui_nav_next(void)
{
    if (U.sheet) {
        return;
    }
    if (U.depth > 0) {
        const int i = U.depth - 1;
        if (U.detail[i].d->next) {
            U.detail[i].d->next(U.detail[i].page, U.detail[i].arg);
        }
        return;
    }
    ui_show_tab((ui_tab_t)((U.tab + 1) % UI_TABS), true);
}

void ui_nav_prev(void)
{
    if (U.sheet) {
        ui_sheet_close();
        return;
    }
    if (U.depth > 0) {
        ui_pop();
        return;
    }
    ui_show_tab((ui_tab_t)((U.tab + UI_TABS - 1) % UI_TABS), true);
}

static void on_gesture(lv_event_t *e)
{
    (void)e;
    const lv_dir_t d = lv_indev_get_gesture_dir(lv_indev_active());
    if (d == LV_DIR_LEFT) {
        ui_nav_next();
    } else if (d == LV_DIR_RIGHT) {
        ui_nav_prev();
    }
}

lv_obj_t *ui_active_scroller(void)
{
    if (U.sheet) {
        return U.sheet;
    }
    if (U.depth > 0) {
        const int i = U.depth - 1;
        return U.detail[i].d->scroller ? U.detail[i].d->scroller(U.detail[i].page) : NULL;
    }
    return UI_SCREENS[U.tab]->scroller ? UI_SCREENS[U.tab]->scroller(U.pages[U.tab]) : NULL;
}

/* ---- detail pages ---- */

static void back_clicked(lv_event_t *e)
{
    (void)e;
    ui_pop();
}

void ui_push(const char *title, const ui_detail_t *d, void *arg)
{
    if (U.depth == DETAIL_DEPTH) {
        if (d->destroy) {
            d->destroy(arg);
        }
        return;
    }
    ui_sheet_close();
    lv_obj_t *layer = lv_obj_create(U.scr);
    plain(layer);
    lv_obj_set_pos(layer, 0, UI_STATUS_H);
    lv_obj_set_size(layer, UI_W, UI_H - UI_STATUS_H);
    lv_obj_set_style_bg_color(layer, C(C_BG), 0);
    lv_obj_set_style_bg_opa(layer, LV_OPA_COVER, 0);
    lv_obj_add_flag(layer, LV_OBJ_FLAG_CLICKABLE); /* nothing under it gets touches */
    lv_obj_add_event_cb(layer, on_gesture, LV_EVENT_GESTURE, NULL);

    lv_obj_t *bar = lv_obj_create(layer);
    plain(bar);
    lv_obj_set_size(bar, UI_W, 80);
    lv_obj_set_flex_flow(bar, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(bar, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_t *back = lv_obj_create(bar);
    plain(back);
    lv_obj_set_size(back, LV_SIZE_CONTENT, UI_TOUCH_MIN + 8);
    lv_obj_set_style_pad_hor(back, UI_MARGIN, 0);
    lv_obj_add_flag(back, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_style(back, &ST_PRESSED, LV_STATE_PRESSED);
    lv_obj_set_style_radius(back, 20, 0);
    lv_obj_add_event_cb(back, back_clicked, LV_EVENT_CLICKED, NULL);
    ui_focusable(back, false);
    lv_obj_t *bl = lv_label_create(back);
    lv_label_set_text(bl, ICON_CHEVRON_LEFT);
    lv_obj_set_style_text_font(bl, F.icon, 0);
    lv_obj_set_style_text_color(bl, C(C_ACCENT), 0);
    lv_obj_center(bl);
    lv_obj_t *tl = lv_label_create(bar);
    lv_label_set_text(tl, title ? title : "");
    lv_label_set_long_mode(tl, LV_LABEL_LONG_MODE_DOTS);
    lv_obj_set_width(tl, UI_W - 140);
    lv_obj_set_style_text_font(tl, F.body_b, 0);
    lv_obj_set_style_text_color(tl, C(C_TEXT), 0);

    lv_obj_t *page = lv_obj_create(layer);
    plain(page);
    lv_obj_set_pos(page, 0, 80);
    lv_obj_set_size(page, UI_W, UI_H - UI_STATUS_H - 80);

    U.detail[U.depth].layer = layer;
    U.detail[U.depth].page = page;
    U.detail[U.depth].d = d;
    U.detail[U.depth].arg = arg;
    U.depth++;
    d->build(page, arg);
    hf_ui_reset_focus();
    slide(layer, UI_W, 0, UI_ANIM);
}

static void detail_gone(lv_anim_t *a)
{
    lv_obj_delete(a->var);
}

void ui_pop(void)
{
    if (U.depth == 0) {
        return;
    }
    U.depth--;
    lv_obj_t *layer = U.detail[U.depth].layer;
    if (U.detail[U.depth].d->destroy) {
        U.detail[U.depth].d->destroy(U.detail[U.depth].arg);
    }
    lv_obj_remove_flag(layer, LV_OBJ_FLAG_CLICKABLE);
    lv_anim_delete(layer, slide_x);
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, layer);
    lv_anim_set_exec_cb(&a, slide_x);
    lv_anim_set_values(&a, lv_obj_get_x(layer), UI_W);
    lv_anim_set_duration(&a, UI_ANIM);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_in);
    lv_anim_set_completed_cb(&a, detail_gone);
    lv_anim_start(&a);
    hf_ui_reset_focus();
    U.dirty |= UI_DIRTY_SHOWN;
}

bool ui_in_detail(void)
{
    return U.depth > 0;
}

/* ---- the sheet ---- */

static void fade(void *var, int32_t v)
{
    lv_obj_set_style_opa(var, (lv_opa_t)v, 0);
}

static void slide_y(void *var, int32_t v)
{
    lv_obj_set_y(var, v);
}

static void sheet_gone(lv_anim_t *a)
{
    lv_obj_delete(a->var);
}

static void sheet_close_clicked(lv_event_t *e)
{
    (void)e;
    ui_sheet_close();
}

static void sheet_gesture(lv_event_t *e)
{
    (void)e;
    const lv_dir_t d = lv_indev_get_gesture_dir(lv_indev_active());
    if (d == LV_DIR_BOTTOM || d == LV_DIR_RIGHT) {
        ui_sheet_close();
    }
}

void ui_sheet_open(void (*build)(lv_obj_t *sheet, void *arg), void *arg, void (*destroy)(void *arg))
{
    ui_sheet_close();
    U.scrim = lv_obj_create(U.scr);
    plain(U.scrim);
    lv_obj_set_size(U.scrim, UI_W, UI_H);
    lv_obj_set_style_bg_color(U.scrim, C(C_BG), 0);
    lv_obj_set_style_bg_opa(U.scrim, LV_OPA_70, 0);
    lv_obj_add_flag(U.scrim, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(U.scrim, sheet_close_clicked, LV_EVENT_CLICKED, NULL);

    lv_obj_t *s = lv_obj_create(U.scrim);
    plain(s);
    U.sheet = s;
    lv_obj_set_width(s, UI_W);
    lv_obj_set_height(s, LV_SIZE_CONTENT);
    lv_obj_set_style_max_height(s, UI_H - UI_STATUS_H - SP_10, 0);
    lv_obj_set_style_bg_color(s, C(C_SURFACE), 0);
    lv_obj_set_style_bg_opa(s, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(s, 36, 0);
    lv_obj_set_style_pad_hor(s, UI_MARGIN + SP_2, 0);
    lv_obj_set_style_pad_top(s, SP_4, 0);
    lv_obj_set_style_pad_bottom(s, UI_MARGIN + 36, 0); /* the bottom corners are off screen */
    lv_obj_set_flex_flow(s, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(s, SP_4, 0);
    lv_obj_add_flag(s, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_scroll_dir(s, LV_DIR_VER);
    lv_obj_add_event_cb(s, sheet_gesture, LV_EVENT_GESTURE, NULL);
    lv_obj_remove_flag(s, LV_OBJ_FLAG_GESTURE_BUBBLE);

    /* the handle and the close button */
    lv_obj_t *top = lv_obj_create(s);
    plain(top);
    lv_obj_set_size(top, LV_PCT(100), UI_TOUCH_MIN);
    lv_obj_t *handle = lv_obj_create(top);
    plain(handle);
    lv_obj_set_size(handle, 56, 6);
    lv_obj_set_style_radius(handle, 3, 0);
    lv_obj_set_style_bg_color(handle, C(C_SURFACE_3), 0);
    lv_obj_set_style_bg_opa(handle, LV_OPA_COVER, 0);
    lv_obj_align(handle, LV_ALIGN_TOP_MID, 0, -SP_1);
    lv_obj_t *x = lv_obj_create(top);
    plain(x);
    lv_obj_set_size(x, UI_TOUCH_MIN, UI_TOUCH_MIN);
    lv_obj_set_style_radius(x, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(x, C(C_SURFACE_2), 0);
    lv_obj_set_style_bg_opa(x, LV_OPA_COVER, 0);
    lv_obj_add_style(x, &ST_PRESSED, LV_STATE_PRESSED);
    lv_obj_set_style_bg_color(x, C(C_SURFACE_3), LV_STATE_PRESSED);
    lv_obj_align(x, LV_ALIGN_TOP_RIGHT, 0, 0);
    lv_obj_add_flag(x, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(x, sheet_close_clicked, LV_EVENT_CLICKED, NULL);
    ui_focusable(x, false);
    lv_obj_t *xl = lv_label_create(x);
    lv_label_set_text(xl, ICON_X);
    lv_obj_set_style_text_font(xl, F.icon, 0);
    lv_obj_set_style_text_color(xl, C(C_TEXT_2), 0);
    lv_obj_center(xl);

    U.sheet_destroy = destroy;
    U.sheet_arg = arg;
    build(s, arg);
    lv_obj_update_layout(s);
    const int32_t h = lv_obj_get_height(s);
    const int32_t y = UI_H - h + 36;

    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, s);
    lv_anim_set_exec_cb(&a, slide_y);
    lv_anim_set_values(&a, UI_H, y);
    lv_anim_set_duration(&a, UI_ANIM_SLOW);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
    lv_obj_set_y(s, UI_H);
    lv_anim_start(&a);
    lv_anim_init(&a);
    lv_anim_set_var(&a, U.scrim);
    lv_anim_set_exec_cb(&a, fade);
    lv_anim_set_values(&a, 0, 255);
    lv_anim_set_duration(&a, UI_ANIM_SLOW);
    lv_obj_set_style_opa(U.scrim, 0, 0);
    lv_anim_start(&a);
    hf_ui_reset_focus();
}

void ui_sheet_close(void)
{
    if (!U.scrim) {
        return;
    }
    lv_obj_t *scrim = U.scrim;
    if (U.sheet_destroy) {
        U.sheet_destroy(U.sheet_arg);
    }
    U.scrim = U.sheet = NULL;
    U.sheet_destroy = NULL;
    U.sheet_arg = NULL;
    lv_obj_remove_flag(scrim, LV_OBJ_FLAG_CLICKABLE);
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, scrim);
    lv_anim_set_exec_cb(&a, fade);
    lv_anim_set_values(&a, lv_obj_get_style_opa(scrim, 0), 0);
    lv_anim_set_duration(&a, UI_ANIM);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_in);
    lv_anim_set_completed_cb(&a, sheet_gone);
    lv_anim_start(&a);
    hf_ui_reset_focus();
}

bool ui_sheet_is_open(void)
{
    return U.scrim != NULL;
}

/* ---- toasts ---- */

static void toast_hide(lv_timer_t *t)
{
    (void)t;
    U.toast_timer = NULL;
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, U.toast);
    lv_anim_set_exec_cb(&a, fade);
    lv_anim_set_values(&a, lv_obj_get_style_opa(U.toast, 0), 0);
    lv_anim_set_duration(&a, UI_ANIM);
    lv_anim_start(&a);
}

void ui_toast(ui_toast_kind_t kind, const char *fmt, ...)
{
    char msg[200];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);
    if (!U.toast) {
        return;
    }
    static const char *const ICONS[] = {ICON_CHECK, ICON_CHECK, ICON_CIRCLE_ALERT};
    static const uint32_t COLORS[] = {C_TEXT_2, C_ACCENT, C_DANGER};
    lv_label_set_text(U.toast_icon, kind == UI_TOAST_INFO ? ICON_SPARKLES : ICONS[kind]);
    lv_obj_set_style_text_color(U.toast_icon, C(COLORS[kind]), 0);
    lv_label_set_text(U.toast_text, msg);
    lv_obj_remove_flag(U.toast, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(U.toast);
    lv_anim_delete(U.toast, fade);
    lv_anim_delete(U.toast, slide_y);
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, U.toast);
    lv_anim_set_exec_cb(&a, fade);
    lv_anim_set_values(&a, lv_obj_get_style_opa(U.toast, 0), 255);
    lv_anim_set_duration(&a, UI_ANIM_FAST);
    lv_anim_start(&a);
    if (U.toast_timer) {
        lv_timer_reset(U.toast_timer);
    } else {
        U.toast_timer = lv_timer_create(toast_hide, UI_TOAST_MS, NULL);
        lv_timer_set_repeat_count(U.toast_timer, 1);
    }
}

static void toast_create(void)
{
    U.toast = lv_obj_create(lv_layer_top());
    plain(U.toast);
    lv_obj_set_size(U.toast, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_style_max_width(U.toast, UI_W - 2 * UI_MARGIN, 0);
    lv_obj_set_style_bg_color(U.toast, C(C_SURFACE_2), 0);
    lv_obj_set_style_bg_opa(U.toast, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(U.toast, C(C_SURFACE_3), 0);
    lv_obj_set_style_border_width(U.toast, 1, 0);
    lv_obj_set_style_radius(U.toast, 32, 0);
    lv_obj_set_style_pad_hor(U.toast, SP_6, 0);
    lv_obj_set_style_pad_ver(U.toast, SP_4, 0);
    lv_obj_set_style_pad_column(U.toast, SP_3, 0);
    lv_obj_set_style_shadow_width(U.toast, 40, 0);
    lv_obj_set_style_shadow_color(U.toast, C(C_BG), 0);
    lv_obj_set_style_shadow_opa(U.toast, LV_OPA_80, 0);
    lv_obj_set_flex_flow(U.toast, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(U.toast, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_align(U.toast, LV_ALIGN_BOTTOM_MID, 0, -(UI_TABBAR_H + SP_6));
    U.toast_icon = lv_label_create(U.toast);
    lv_label_set_text(U.toast_icon, "");
    lv_obj_set_style_text_font(U.toast_icon, F.icon_s, 0);
    U.toast_text = lv_label_create(U.toast);
    lv_obj_set_style_text_font(U.toast_text, F.small, 0);
    lv_obj_set_style_text_color(U.toast_text, C(C_TEXT), 0);
    lv_label_set_long_mode(U.toast_text, LV_LABEL_LONG_MODE_DOTS);
    lv_obj_set_style_max_width(U.toast_text, UI_W - 2 * UI_MARGIN - 2 * SP_6 - 40, 0);
    lv_obj_set_style_opa(U.toast, 0, 0);
    lv_obj_add_flag(U.toast, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(U.toast, LV_OBJ_FLAG_CLICKABLE);
}

/* ---- sync ---- */

int ui_send_local(const char *json, size_t len)
{
    return U.hooks.send_local ? U.hooks.send_local(json, len) : -1;
}

void ui_request_sync(void)
{
    char buf[64];
    const size_t n = local_sync_now(buf, sizeof(buf));
    if (!app_link_up() || ui_send_local(buf, n) != 0) {
        ui_toast(UI_TOAST_ERROR, "The sync service isn't running");
        return;
    }
    const app_net_state_t s = app_net()->state;
    if (s == APP_NET_UNPAIRED) {
        ui_toast(UI_TOAST_ERROR, "Not paired with Odysseus yet");
    } else {
        ui_toast(UI_TOAST_INFO, "Syncing\xe2\x80\xa6");
    }
}

void ui_start_launcher(void)
{
    if (!U.hooks.start_launcher || U.hooks.start_launcher() != 0) {
        ui_toast(UI_TOAST_ERROR, "Couldn't start the launcher");
    }
}

/* ---- screen off ---- */

static void off_clicked(lv_event_t *e)
{
    (void)e;
    ui_screen_on();
}

void ui_screen_off(void)
{
    if (!U.off) {
        U.off = lv_obj_create(lv_layer_sys());
        plain(U.off);
        lv_obj_set_size(U.off, UI_W, UI_H);
        lv_obj_set_style_bg_color(U.off, C(C_BG), 0);
        lv_obj_set_style_bg_opa(U.off, LV_OPA_COVER, 0);
        lv_obj_add_flag(U.off, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(U.off, off_clicked, LV_EVENT_CLICKED, NULL);
    }
    lv_obj_remove_flag(U.off, LV_OBJ_FLAG_HIDDEN);
    if (U.hooks.screen_power) {
        U.hooks.screen_power(false);
    }
}

void ui_screen_on(void)
{
    if (U.off && !lv_obj_has_flag(U.off, LV_OBJ_FLAG_HIDDEN)) {
        lv_obj_add_flag(U.off, LV_OBJ_FLAG_HIDDEN);
        if (U.hooks.screen_power) {
            U.hooks.screen_power(true);
        }
    }
}

bool ui_screen_is_off(void)
{
    return U.off && !lv_obj_has_flag(U.off, LV_OBJ_FLAG_HIDDEN);
}

/* ---- init ---- */

void ui_set_hooks(const ui_hooks_t *h)
{
    U.hooks = *h;
}

void ui_init(lv_display_t *disp)
{
    U.scr = lv_display_get_screen_active(disp);
    lv_obj_remove_flag(U.scr, LV_OBJ_FLAG_SCROLLABLE);

    U.content = lv_obj_create(U.scr);
    plain(U.content);
    lv_obj_set_pos(U.content, 0, UI_STATUS_H);
    lv_obj_set_size(U.content, UI_W, UI_CONTENT_H);
    lv_obj_add_event_cb(U.content, on_gesture, LV_EVENT_GESTURE, NULL);
    for (int t = 0; t < UI_TABS; t++) {
        U.pages[t] = lv_obj_create(U.content);
        plain(U.pages[t]);
        lv_obj_set_size(U.pages[t], UI_W, UI_CONTENT_H);
        lv_obj_add_flag(U.pages[t], LV_OBJ_FLAG_HIDDEN);
    }
    chrome_create(U.scr);
    toast_create();
    hf_ui_create();
    app_add_listener(on_app, NULL);
    U.minute = app_now() / 60;
    lv_timer_create(flush_timer, 40, NULL);
    U.tab = UI_TAB_TODAY;
    build_page(UI_TAB_TODAY);
    lv_obj_remove_flag(U.pages[UI_TAB_TODAY], LV_OBJ_FLAG_HIDDEN);
    chrome_set_tab(UI_TAB_TODAY, false);
    chrome_update(UI_DIRTY_ALL);
}
