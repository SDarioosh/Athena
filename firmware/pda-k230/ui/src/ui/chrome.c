/* The status header (clock; camera dot, changes waiting, sync state, battery) and the tab
 * bar (icons and labels, the active one in the accent on a pill). */
#include <stdio.h>
#include <string.h>

#include "app_state.h"
#include "pda_time.h"
#include "theme.h"
#include "ui_internal.h"
#include "widgets.h"

static struct {
    lv_obj_t *bar, *clock, *right;
    lv_obj_t *cam, *pending, *sync, *sync_icon, *sync_text, *batt;
    lv_obj_t *tabbar, *tab[UI_TABS], *pill[UI_TABS], *icon[UI_TABS], *label[UI_TABS];
    ui_tab_t active;
} H;

static void plain(lv_obj_t *o)
{
    lv_obj_remove_style_all(o);
    lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE);
}

static void show(lv_obj_t *o, bool on)
{
    ui_show(o, on);
}

static lv_obj_t *label(lv_obj_t *parent, const lv_font_t *f, uint32_t color)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_obj_set_style_text_font(l, f, 0);
    lv_obj_set_style_text_color(l, C(color), 0);
    lv_label_set_text(l, "");
    return l;
}

static void spin(void *var, int32_t v)
{
    lv_obj_set_style_transform_rotation(var, v, 0);
}

static void tab_clicked(lv_event_t *e)
{
    const ui_tab_t t = (ui_tab_t)(intptr_t)lv_event_get_user_data(e);
    ui_show_tab(t, true);
}

void chrome_create(lv_obj_t *scr)
{
    /* ---- the header ---- */
    H.bar = lv_obj_create(scr);
    plain(H.bar);
    lv_obj_set_size(H.bar, UI_W, UI_STATUS_H);
    lv_obj_set_style_pad_hor(H.bar, UI_MARGIN + SP_2, 0);
    lv_obj_set_style_bg_color(H.bar, C(C_BG), 0);
    lv_obj_set_style_bg_opa(H.bar, LV_OPA_COVER, 0);
    H.clock = label(H.bar, F.body_b, C_TEXT);
    lv_obj_align(H.clock, LV_ALIGN_LEFT_MID, 0, 0);

    H.right = lv_obj_create(H.bar);
    plain(H.right);
    /* a fixed width (not content-sized): items come and go, the row stays right-aligned */
    lv_obj_set_size(H.right, UI_W - 2 * (UI_MARGIN + SP_2) - 110, UI_STATUS_H);
    lv_obj_align(H.right, LV_ALIGN_RIGHT_MID, 0, 0);
    lv_obj_set_flex_flow(H.right, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(H.right, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(H.right, SP_4, 0);

    /* the camera dot: shown whenever vision reports the camera on (plan "Decisions") */
    H.cam = lv_obj_create(H.right);
    plain(H.cam);
    lv_obj_set_size(H.cam, 14, 14);
    lv_obj_set_style_radius(H.cam, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(H.cam, C(C_CAMERA), 0);
    lv_obj_set_style_bg_opa(H.cam, LV_OPA_COVER, 0);
    lv_obj_set_style_shadow_color(H.cam, C(C_CAMERA), 0);
    lv_obj_set_style_shadow_width(H.cam, 16, 0);
    lv_obj_set_style_shadow_opa(H.cam, LV_OPA_60, 0);

    H.pending = label(H.right, F.small_b, C_ACCENT);

    H.sync = lv_obj_create(H.right);
    plain(H.sync);
    lv_obj_set_size(H.sync, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(H.sync, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(H.sync, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(H.sync, SP_2, 0);
    H.sync_icon = label(H.sync, F.icon_s, C_TEXT_2);
    lv_obj_set_style_transform_pivot_x(H.sync_icon, 12, 0);
    lv_obj_set_style_transform_pivot_y(H.sync_icon, 12, 0);
    H.sync_text = label(H.sync, F.small, C_TEXT_2);

    H.batt = label(H.right, F.small, C_TEXT_2);

    /* ---- the tab bar ---- */
    H.tabbar = lv_obj_create(scr);
    plain(H.tabbar);
    lv_obj_set_size(H.tabbar, UI_W, UI_TABBAR_H);
    lv_obj_align(H.tabbar, LV_ALIGN_BOTTOM_MID, 0, 0);
    lv_obj_set_style_bg_color(H.tabbar, C(C_BG), 0);
    lv_obj_set_style_bg_opa(H.tabbar, LV_OPA_COVER, 0);
    lv_obj_set_style_border_side(H.tabbar, LV_BORDER_SIDE_TOP, 0);
    lv_obj_set_style_border_width(H.tabbar, 1, 0);
    lv_obj_set_style_border_color(H.tabbar, C(C_HAIRLINE), 0);
    lv_obj_set_style_pad_hor(H.tabbar, SP_1, 0);
    lv_obj_set_style_pad_bottom(H.tabbar, SP_2, 0);
    lv_obj_set_flex_flow(H.tabbar, LV_FLEX_FLOW_ROW);
    for (int t = 0; t < UI_TABS; t++) {
        lv_obj_t *b = lv_obj_create(H.tabbar);
        plain(b);
        H.tab[t] = b;
        lv_obj_set_flex_grow(b, 1);
        lv_obj_set_height(b, LV_PCT(100));
        lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(b, tab_clicked, LV_EVENT_CLICKED, (void *)(intptr_t)t);
        lv_obj_set_flex_flow(b, LV_FLEX_FLOW_COLUMN);
        lv_obj_set_flex_align(b, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        lv_obj_set_style_pad_row(b, SP_1, 0);
        ui_focusable(b, false);
        H.pill[t] = lv_obj_create(b);
        plain(H.pill[t]);
        lv_obj_set_size(H.pill[t], 68, 44);
        lv_obj_set_style_radius(H.pill[t], 22, 0);
        lv_obj_set_style_bg_color(H.pill[t], lv_color_mix(C(C_ACCENT), C(C_BG), LV_OPA_20), 0);
        lv_obj_set_style_bg_opa(H.pill[t], LV_OPA_TRANSP, 0);
        lv_obj_set_style_bg_color(H.pill[t], C(C_SURFACE_2), LV_STATE_PRESSED);
        lv_obj_add_flag(H.pill[t], LV_OBJ_FLAG_EVENT_BUBBLE);
        H.icon[t] = label(H.pill[t], F.icon, C_TEXT_3);
        lv_label_set_text(H.icon[t], UI_SCREENS[t]->icon);
        lv_obj_center(H.icon[t]);
        H.label[t] = label(b, F.tab, C_TEXT_3);
        lv_label_set_text(H.label[t], UI_SCREENS[t]->label);
    }
    H.active = UI_TABS;
}

static void pill_opa(void *var, int32_t v)
{
    lv_obj_set_style_bg_opa(var, (lv_opa_t)v, 0);
}

void chrome_set_tab(ui_tab_t tab, bool animate)
{
    for (int t = 0; t < UI_TABS; t++) {
        const bool on = t == (int)tab;
        lv_obj_set_style_text_color(H.icon[t], C(on ? C_ACCENT : C_TEXT_3), 0);
        lv_obj_set_style_text_color(H.label[t], C(on ? C_ACCENT : C_TEXT_3), 0);
        lv_anim_delete(H.pill[t], pill_opa);
        if (on && animate && H.active != tab) {
            lv_anim_t a;
            lv_anim_init(&a);
            lv_anim_set_var(&a, H.pill[t]);
            lv_anim_set_exec_cb(&a, pill_opa);
            lv_anim_set_values(&a, 0, 255);
            lv_anim_set_duration(&a, UI_ANIM);
            lv_anim_start(&a);
        } else {
            lv_obj_set_style_bg_opa(H.pill[t], on ? LV_OPA_COVER : LV_OPA_TRANSP, 0);
        }
    }
    H.active = tab;
}

static void update_sync(void)
{
    const app_net_t *n = app_net();
    const char *icon = NULL, *text = "";
    uint32_t color = C_TEXT_2;
    char age[48] = "";
    bool spinning = false;
    if (!app_link_up()) {
        icon = ICON_CLOUD_OFF;
        color = C_TEXT_3;
    } else {
        switch (n->state) {
        case APP_NET_UNPAIRED: icon = ICON_CLOUD_OFF; text = "Not paired"; break;
        case APP_NET_OFFLINE: icon = ICON_WIFI_OFF; text = "Offline"; break;
        case APP_NET_SYNCING: icon = ICON_REFRESH_CW; color = C_ACCENT; spinning = true; break;
        case APP_NET_AUTH_FAILED: icon = ICON_CIRCLE_ALERT; text = "Sign in again"; color = C_DANGER; break;
        case APP_NET_OK:
        case APP_NET_UNKNOWN:
            /* like the T5: a note only when worth a look */
            if (n->last_ok > 0 && app_now() - n->last_ok >= 3600) {
                char a[24];
                pda_fmt_age(app_now() - n->last_ok, a, sizeof(a));
                snprintf(age, sizeof(age), "%s", a); /* "2 h ago" by the clock icon */
                icon = ICON_CLOCK;
                text = age;
            }
            break;
        }
    }
    if (!app_feed() && !text[0] && n->state != APP_NET_SYNCING) {
        text = "No feed";
    }
    show(H.sync, icon || text[0]);
    show(H.sync_icon, icon != NULL);
    show(H.sync_text, text[0] != 0);
    if (icon) {
        lv_label_set_text(H.sync_icon, icon);
        lv_obj_set_style_text_color(H.sync_icon, C(color), 0);
    }
    lv_label_set_text(H.sync_text, text);
    lv_obj_set_style_text_color(H.sync_text, C(color == C_TEXT_3 ? C_TEXT_2 : color), 0);
    const bool running = lv_anim_get(H.sync_icon, spin) != NULL;
    if (spinning && !running) {
        lv_anim_t a;
        lv_anim_init(&a);
        lv_anim_set_var(&a, H.sync_icon);
        lv_anim_set_exec_cb(&a, spin);
        lv_anim_set_values(&a, 0, 3600);
        lv_anim_set_duration(&a, 1000);
        lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
        lv_anim_start(&a);
    } else if (!spinning && running) {
        lv_anim_delete(H.sync_icon, spin);
        lv_obj_set_style_transform_rotation(H.sync_icon, 0, 0);
    }
}

void chrome_update(unsigned what)
{
    if (what & UI_DIRTY_CLOCK) {
        char hm[16];
        pda_fmt_hm(app_now(), app_tz_min(), hm, sizeof(hm));
        lv_label_set_text(H.clock, app_time_valid() ? hm : "--:--");
    }
    if (what & UI_DIRTY_VISION) {
        show(H.cam, app_vision()->camera == APP_CAMERA_ON);
    }
    if (what & (UI_DIRTY_OUTBOX | UI_DIRTY_FEED)) {
        const int n = app_outbox_count();
        show(H.pending, n > 0);
        lv_label_set_text_fmt(H.pending, ICON_UPLOAD " %d", n);
    }
    if (what & (UI_DIRTY_NET | UI_DIRTY_CLOCK | UI_DIRTY_FEED)) {
        update_sync();
    }
    if (what & UI_DIRTY_POWER) {
        const int pct = app_battery_pct();
        show(H.batt, pct >= 0);
        const char *icon = app_charging() ? ICON_BATTERY_CHARGING
                           : pct >= 70    ? ICON_BATTERY_FULL
                           : pct >= 35    ? ICON_BATTERY_MEDIUM
                           : pct >= 15    ? ICON_BATTERY_LOW
                                          : ICON_BATTERY;
        lv_label_set_text_fmt(H.batt, "%s %d%%", icon, pct);
        lv_obj_set_style_text_color(H.batt, C(pct >= 0 && pct < 15 && !app_charging() ? C_DANGER : C_TEXT_2), 0);
    }
}
