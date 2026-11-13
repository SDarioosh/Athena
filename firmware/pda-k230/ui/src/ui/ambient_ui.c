/* The ambient screen, in place of the plain black screen-off overlay (docs/k230-pda-plan.md
 * "Screens"; the T5's Asleep screen): mostly black for the AMOLED, a large clock, the date,
 * the next event that hasn't ended (title and full date and time), small sync and pending
 * marks; the picture moves a little every minute against burn-in. The backlight goes down
 * while it shows and to 0 once it turns fully off. Timing: core/ambient.c, the timeouts from
 * ui.json (dim_after_s, off_after_s). Woken by a touch, `palm` and presence (ui.c, the
 * hands-free and presence paths call ui_screen_on()). */
#include <stdio.h>
#include <string.h>

#include "ambient.h"
#include "app_state.h"
#include "icons_extra.h"
#include "pda_time.h"
#include "theme.h"
#include "ui_internal.h"
#include "widgets.h"

#define AMB_BRIGHTNESS 20 /* % at most while ambient */
#define AMB_TICK_MS 500

static struct {
    amb_t st;
    lv_obj_t *layer, *group, *clock, *date, *next_cap, *next_title, *next_when, *foot;
    int64_t last_gesture_ms, minute;
} A;

static void plain(lv_obj_t *o)
{
    lv_obj_remove_style_all(o);
    lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
}

static void power(bool on)
{
    const ui_hooks_t *h = ui_hooks();
    if (h->screen_power) {
        h->screen_power(on);
    }
}

static void brightness(int pct)
{
    const ui_hooks_t *h = ui_hooks();
    if (h->brightness) {
        h->brightness(pct);
    }
}

static void layer_pressed(lv_event_t *e)
{
    (void)e;
    ui_screen_on();
}

static lv_obj_t *centered_label(lv_obj_t *parent, const lv_font_t *f, uint32_t color)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_obj_set_style_text_font(l, f, 0);
    lv_obj_set_style_text_color(l, C(color), 0);
    lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_long_mode(l, LV_LABEL_LONG_MODE_DOTS);
    lv_obj_set_width(l, UI_CARD_W - 2 * SP_8);
    lv_label_set_text(l, "");
    return l;
}

static void create(void)
{
    A.layer = lv_obj_create(lv_layer_sys());
    plain(A.layer);
    lv_obj_set_size(A.layer, UI_W, UI_H);
    lv_obj_set_style_bg_color(A.layer, C(C_BG), 0);
    lv_obj_set_style_bg_opa(A.layer, LV_OPA_COVER, 0);
    lv_obj_add_flag(A.layer, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(A.layer, layer_pressed, LV_EVENT_PRESSED, NULL);

    /* clock, date and the next event: a column a little above the middle */
    A.group = lv_obj_create(A.layer);
    plain(A.group);
    lv_obj_set_size(A.group, UI_W, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(A.group, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(A.group, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    A.clock = lv_label_create(A.group);
    lv_obj_set_style_text_font(A.clock, fx()->clock, 0);
    lv_obj_set_style_text_color(A.clock, C(C_TEXT_2), 0);
    lv_obj_set_style_text_letter_space(A.clock, -4, 0);
    A.date = centered_label(A.group, F.body, C_TEXT_3);
    lv_obj_set_style_margin_top(A.date, -SP_2, 0);
    A.next_cap = centered_label(A.group, F.caption, C_TEXT_3);
    lv_obj_set_style_text_letter_space(A.next_cap, 3, 0);
    lv_obj_set_style_margin_top(A.next_cap, SP_12 + SP_8, 0);
    A.next_title = centered_label(A.group, F.body_b, C_TEXT_2);
    lv_label_set_long_mode(A.next_title, LV_LABEL_LONG_MODE_DOTS);
    lv_obj_set_style_max_height(A.next_title, 2 * lv_font_get_line_height(F.body_b) + 4, 0);
    lv_obj_set_style_margin_top(A.next_title, SP_3, 0);
    A.next_when = centered_label(A.group, F.small, C_TEXT_3);
    lv_obj_set_style_margin_top(A.next_when, SP_1, 0);

    /* the marks at the foot */
    A.foot = centered_label(A.layer, F.small, C_TEXT_3);
    lv_obj_set_width(A.foot, UI_W - 2 * UI_MARGIN);
    lv_obj_add_flag(A.layer, LV_OBJ_FLAG_HIDDEN);
}

static void refresh(void)
{
    if (!A.layer) {
        return;
    }
    const bool off = A.st.stage == AMB_OFF;
    ui_show(A.group, !off);
    ui_show(A.foot, !off);
    if (off) {
        return;
    }
    const int64_t now = app_now();
    const int tz = app_tz_min();
    char b[160];
    pda_fmt_hm(now, tz, b, sizeof(b));
    lv_label_set_text(A.clock, app_time_valid() ? b : "--:--");
    amb_fmt_date_long(now, tz, b, sizeof(b));
    lv_label_set_text(A.date, app_time_valid() ? b : "");

    const feed_t *f = app_feed();
    const int i = app_time_valid() ? amb_next_event(f, now, tz) : -1;
    if (i >= 0) {
        const feed_event_t *e = &f->events[i];
        const bool now_on = !e->all_day && e->start <= now;
        lv_label_set_text(A.next_cap, now_on ? "NOW" : "NEXT");
        lv_obj_set_style_text_color(A.next_cap, lv_color_mix(C(C_ACCENT), C(C_BG), LV_OPA_70), 0);
        lv_label_set_text(A.next_title, e->title);
        amb_fmt_when(e, tz, b, sizeof(b));
        lv_label_set_text(A.next_when, b);
        ui_show(A.next_title, true);
        ui_show(A.next_when, true);
    } else {
        lv_label_set_text(A.next_cap, f ? "NOTHING ON THE CALENDAR" : "");
        lv_obj_set_style_text_color(A.next_cap, C(C_TEXT_3), 0);
        ui_show(A.next_title, false);
        ui_show(A.next_when, false);
    }

    /* sync, waiting changes, battery */
    const app_net_t *n = app_net();
    char sync[64] = "";
    if (!app_link_up()) {
        snprintf(sync, sizeof(sync), ICON_CLOUD_OFF " Not syncing");
    } else if (n->state == APP_NET_UNPAIRED) {
        snprintf(sync, sizeof(sync), ICON_CLOUD_OFF " Not paired");
    } else if (n->state == APP_NET_OFFLINE) {
        snprintf(sync, sizeof(sync), ICON_WIFI_OFF " Offline");
    } else if (n->state == APP_NET_AUTH_FAILED) {
        snprintf(sync, sizeof(sync), ICON_CIRCLE_ALERT " Pair again");
    } else if (n->last_ok > 0) {
        char hm[12];
        pda_fmt_hm(n->last_ok, tz, hm, sizeof(hm));
        snprintf(sync, sizeof(sync), ICON_CHECK " Synced %s", hm);
    }
    char line[160];
    size_t m = (size_t)snprintf(line, sizeof(line), "%s", sync);
    const int ob = app_outbox_count();
    if (ob > 0 && m < sizeof(line)) {
        m += (size_t)snprintf(line + m, sizeof(line) - m, "%s" ICON_UPLOAD " %d waiting", m ? "     " : "", ob);
    }
    const int pct = app_battery_pct();
    if (pct >= 0 && m < sizeof(line)) {
        snprintf(line + m, sizeof(line) - m, "%s%s %d%%", m ? "     " : "",
                 app_charging() ? ICON_BATTERY_CHARGING : pct < 15 ? ICON_BATTERY_LOW : ICON_BATTERY_MEDIUM, pct);
    }
    lv_label_set_text(A.foot, line);

    /* the burn-in walk */
    int32_t dx, dy;
    amb_shift(now / 60, &dx, &dy);
    lv_obj_align(A.group, LV_ALIGN_TOP_MID, dx, 300 + dy);
    lv_obj_align(A.foot, LV_ALIGN_BOTTOM_MID, dx, -(SP_12 + SP_4) + dy);
}

static void apply(void)
{
    switch (A.st.stage) {
    case AMB_AWAKE:
        lv_obj_add_flag(A.layer, LV_OBJ_FLAG_HIDDEN);
        power(true);
        brightness(app_settings()->brightness);
        break;
    case AMB_AMBIENT: {
        lv_obj_remove_flag(A.layer, LV_OBJ_FLAG_HIDDEN);
        lv_obj_move_foreground(A.layer);
        power(true);
        const int b = app_settings()->brightness;
        brightness(b < AMB_BRIGHTNESS ? b : AMB_BRIGHTNESS);
        A.minute = app_now() / 60;
        refresh();
        break;
    }
    case AMB_OFF:
        lv_obj_remove_flag(A.layer, LV_OBJ_FLAG_HIDDEN);
        refresh();
        power(false);
        break;
    }
}

static void tick(lv_timer_t *t)
{
    (void)t;
    const app_settings_t *s = app_settings();
    const app_vision_t *v = app_vision();
    const amb_cfg_t cfg = {
        .dim_after_s = s->dim_after_s,
        .off_after_s = s->off_after_s,
        .hold = s->presence_wake && v->connected && v->present,
    };
    int64_t last = app_last_activity_ms();
    if (A.last_gesture_ms > last) {
        last = A.last_gesture_ms;
    }
    if (amb_tick(&A.st, app_mono_ms(), last, &cfg)) {
        apply();
        return;
    }
    if (A.st.stage == AMB_AMBIENT && app_now() / 60 != A.minute) {
        A.minute = app_now() / 60;
        refresh();
    }
}

void ambient_ui_init(void)
{
    create();
    amb_reset(&A.st, app_mono_ms());
    lv_timer_create(tick, AMB_TICK_MS, NULL);
}

void ambient_ui_enter(void)
{
    if (!A.layer) {
        return;
    }
    if (amb_dim(&A.st, app_mono_ms())) {
        apply();
    }
}

void ambient_ui_wake(void)
{
    if (!A.layer) {
        return;
    }
    A.last_gesture_ms = app_mono_ms(); /* the timeout counts from the wake */
    if (amb_wake(&A.st, app_mono_ms())) {
        apply();
    }
}

bool ambient_ui_active(void)
{
    return A.layer && A.st.stage != AMB_AWAKE;
}

void ambient_ui_poke(void)
{
    A.last_gesture_ms = app_mono_ms();
}
