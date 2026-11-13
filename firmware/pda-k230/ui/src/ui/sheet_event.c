/* An event's details in the bottom sheet: title, when, where, the hub's reason, and a way
 * to its day on the Calendar tab. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app_state.h"
#include "screens.h"
#include "theme.h"
#include "widgets.h"

typedef struct {
    int event;
    char why[200];
} sheet_arg_t;

static lv_obj_t *info_row(lv_obj_t *parent, const char *icon, uint32_t icon_color, const char *text,
                          uint32_t color)
{
    lv_obj_t *row = lv_obj_create(parent);
    lv_obj_remove_style_all(row);
    lv_obj_remove_flag(row, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(row, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_set_style_pad_column(row, SP_4, 0);
    lv_obj_t *i = lv_label_create(row);
    lv_label_set_text(i, icon);
    lv_obj_set_style_text_font(i, F.small, 0);
    lv_obj_set_style_text_color(i, C(icon_color), 0);
    lv_obj_set_style_pad_top(i, 2, 0);
    ui_text(row, F.body, color, text, UI_W - 2 * (UI_MARGIN + SP_2) - 26 - SP_4, 4);
    return row;
}

static void to_calendar(lv_event_t *e)
{
    (void)e;
    ui_show_tab(UI_TAB_CALENDAR, true);
}

static void build(lv_obj_t *s, void *argp)
{
    const sheet_arg_t *arg = argp;
    const feed_t *f = app_feed();
    if (!f || arg->event < 0 || arg->event >= f->n_events) {
        ui_text(s, F.body, C_TEXT_2, "This event is no longer in the feed.", UI_CARD_W, 3);
        return;
    }
    const feed_event_t *e = &f->events[arg->event];
    lv_obj_t *t = ui_text(s, F.title, C_TEXT, e->title, UI_W - 2 * (UI_MARGIN + SP_2), 3);
    lv_obj_set_style_text_line_space(t, 0, 0);
    lv_obj_set_style_margin_bottom(t, SP_2, 0);
    char when[80];
    ui_fmt_event_when(e, app_today(), app_tz_min(), when, sizeof(when));
    info_row(s, ICON_CLOCK, C_TEXT_2, when, C_TEXT);
    if (e->where) {
        info_row(s, ICON_MAP_PIN, C_TEXT_2, e->where, C_TEXT);
    }
    if (arg->why[0]) {
        info_row(s, ICON_SPARKLES, C_ACCENT, arg->why, C_TEXT_2);
    }
    /* to the Calendar tab */
    lv_obj_t *b = lv_obj_create(s);
    lv_obj_remove_style_all(b);
    lv_obj_remove_flag(b, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_size(b, LV_PCT(100), UI_TOUCH_MIN + 8);
    lv_obj_set_style_margin_top(b, SP_4, 0);
    lv_obj_set_style_radius(b, 24, 0);
    lv_obj_set_style_bg_color(b, C(C_SURFACE_2), 0);
    lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(b, C(C_SURFACE_3), LV_STATE_PRESSED);
    lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(b, to_calendar, LV_EVENT_CLICKED, NULL);
    ui_focusable(b, false);
    lv_obj_t *bl = lv_label_create(b);
    lv_label_set_text(bl, ICON_CALENDAR_DAYS "  Open the calendar");
    lv_obj_set_style_text_font(bl, F.body_b, 0);
    lv_obj_set_style_text_color(bl, C(C_ACCENT), 0);
    lv_obj_center(bl);
}

void sheet_event_open(int event_idx, const char *why)
{
    sheet_arg_t *a = calloc(1, sizeof(*a));
    if (!a) {
        return;
    }
    a->event = event_idx;
    snprintf(a->why, sizeof(a->why), "%s", why ? why : "");
    ui_sheet_open(build, a, free);
}
