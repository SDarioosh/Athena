/* The new-event flow (T5 plan §5 "Adding an event", with the K230's keyboard): two pushed
 * pages. "When": the next 14 days as tiles, the time (an hour grid 08:00-19:00 and ±15 min,
 * like the T5), the duration (15, 30, 60 min, all day). "What": the title, typed on the
 * on-screen keyboard (200 characters) or one tap on the feed's templates (Call, Meeting,
 * Reminder without any). "Add to calendar" queues the cal_add (event_form.c) and returns to
 * the Calendar, where it shows as pending. */
#include <stdio.h>
#include <string.h>

#include "agenda.h"
#include "app_state.h"
#include "calendar_ui.h"
#include "controls.h"
#include "event_form.h"
#include "pda_time.h"
#include "screens.h"
#include "theme.h"
#include "ui_keyboard.h"
#include "widgets.h"

#define GRID_COLS 4
#define TILE_GAP SP_3
#define GRID_W ((UI_CARD_INNER_W - (GRID_COLS - 1) * TILE_GAP) / GRID_COLS)
#define DAY_TILE_W 96
#define DAY_TILE_H 120
#define STEP_W 120

static const char *const DEFAULT_TEMPLATES[] = {"Call", "Meeting", "Reminder"};
static const int DURATIONS[] = {15, 30, 60, 0};
static const char *const DURATION_LABELS[] = {"15 min", "30 min", "1 hour", "All day"};

static struct {
    event_form_t form;
    int32_t today;
    /* When */
    lv_obj_t *when_scroll, *day_strip, *day_aside, *time_label;
    lv_obj_t *days[EVENT_FORM_DAYS], *hours[EVENT_SLOTS], *durs[4];
    /* What */
    lv_obj_t *what_scroll, *summary, *field, *counter, *error, *add;
    lv_obj_t *chips[FEED_MAX_TEMPLATES];
    int n_chips;
} N;

/* ---- shared ---- */

static lv_obj_t *card_body(lv_obj_t *card, lv_flex_flow_t flow)
{
    lv_obj_t *b = lv_obj_create(card);
    lv_obj_remove_style_all(b);
    lv_obj_remove_flag(b, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(b, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_style_pad_hor(b, UI_CARD_PAD, 0);
    lv_obj_set_style_pad_bottom(b, UI_CARD_PAD - SP_2, 0);
    lv_obj_set_flex_flow(b, flow);
    lv_obj_set_style_pad_row(b, TILE_GAP, 0);
    lv_obj_set_style_pad_column(b, TILE_GAP, 0);
    return b;
}

/* a section head with a value on the right ("DAY ... Wed 21 Oct") */
static lv_obj_t *head_with_value(lv_obj_t *card, const char *label)
{
    lv_obj_t *row = ui_section_head(card, NULL, label, C_TEXT_2, NULL, NULL, NULL);
    lv_obj_t *v = lv_label_create(row);
    lv_obj_set_style_text_font(v, F.small_b, 0);
    lv_obj_set_style_text_color(v, C(C_TEXT), 0);
    lv_label_set_text(v, "");
    return v;
}

/* ---- When ---- */

static void show_when(bool animate)
{
    (void)animate;
    const event_form_t *f = &N.form;
    for (int i = 0; i < EVENT_FORM_DAYS; i++) {
        ui_choice_set(N.days[i], N.today + i == f->day);
    }
    for (int i = 0; i < EVENT_SLOTS; i++) {
        ui_choice_set(N.hours[i], f->dur != 0 && f->min / 60 == EVENT_FIRST_SLOT + i);
    }
    for (int i = 0; i < 4; i++) {
        ui_choice_set(N.durs[i], f->dur == DURATIONS[i]);
    }
    char d[24], t[32];
    agenda_day_name(f->day, N.today, d, sizeof(d));
    lv_label_set_text(N.day_aside, d);
    if (f->dur == 0) {
        snprintf(t, sizeof(t), "All day");
    } else {
        snprintf(t, sizeof(t), "%02d:%02d", f->min / 60, f->min % 60);
    }
    lv_label_set_text(N.time_label, t);
    lv_obj_set_style_text_color(N.time_label, C(f->dur == 0 ? C_TEXT_2 : C_TEXT), 0);
    lv_obj_set_style_text_font(N.time_label, f->dur == 0 ? F.title : F.display, 0);
}

static void day_clicked(lv_event_t *e)
{
    const int i = (int)(intptr_t)lv_event_get_user_data(e);
    event_form_set_day(&N.form, N.today + i, N.today);
    lv_obj_scroll_to_view(N.days[i], LV_ANIM_ON);
    show_when(true);
}

static void hour_clicked(lv_event_t *e)
{
    event_form_set_hour(&N.form, EVENT_FIRST_SLOT + (int)(intptr_t)lv_event_get_user_data(e));
    show_when(true);
}

static void step_clicked(lv_event_t *e)
{
    event_form_step(&N.form, (int)(intptr_t)lv_event_get_user_data(e));
    show_when(true);
}

static void dur_clicked(lv_event_t *e)
{
    event_form_set_duration(&N.form, DURATIONS[(int)(intptr_t)lv_event_get_user_data(e)]);
    show_when(true);
}

static const ui_detail_t WHAT;

static void next_clicked(lv_event_t *e)
{
    (void)e;
    ui_push("New event", &WHAT, NULL);
}

static void day_tile(lv_obj_t *strip, int i)
{
    const int32_t day = N.today + i;
    lv_obj_t *t = ui_choice(strip, NULL, DAY_TILE_W, DAY_TILE_H, day_clicked, (void *)(intptr_t)i);
    N.days[i] = t;
    char d[24]; /* "Wed 21 Oct" */
    pda_fmt_day(day, d, sizeof(d));
    char wd[8] = "", num[4] = "", mon[8] = "";
    sscanf(d, "%7s %3s %7s", wd, num, mon);
    lv_obj_t *a = lv_label_create(t);
    lv_label_set_text(a, i == 0 ? "Today" : wd);
    lv_obj_set_style_text_font(a, F.caption, 0);
    lv_obj_t *b = lv_label_create(t);
    lv_label_set_text(b, num);
    lv_obj_set_style_text_font(b, F.title, 0);
    lv_obj_t *c = lv_label_create(t);
    lv_label_set_text(c, mon);
    lv_obj_set_style_text_font(c, F.caption, 0);
    lv_obj_set_style_text_opa(c, LV_OPA_70, 0);
    lv_obj_set_style_text_opa(a, LV_OPA_80, 0);
}

static void when_build(lv_obj_t *page, void *arg)
{
    (void)arg;
    N.when_scroll = ui_page_scroller(page);
    ui_page_title(N.when_scroll, "When?", "Step 1 of 2");

    /* the day */
    lv_obj_t *card = ui_card(N.when_scroll);
    N.day_aside = head_with_value(card, "DAY");
    lv_obj_t *strip = lv_obj_create(card);
    lv_obj_remove_style_all(strip);
    N.day_strip = strip;
    lv_obj_set_size(strip, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(strip, LV_FLEX_FLOW_ROW);
    lv_obj_set_style_pad_hor(strip, UI_CARD_PAD, 0);
    lv_obj_set_style_pad_bottom(strip, UI_CARD_PAD - SP_2, 0);
    lv_obj_set_style_pad_top(strip, SP_1, 0);
    lv_obj_set_style_pad_column(strip, TILE_GAP, 0);
    lv_obj_add_flag(strip, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_SCROLL_MOMENTUM | LV_OBJ_FLAG_SCROLL_ELASTIC);
    lv_obj_set_scroll_dir(strip, LV_DIR_HOR);
    lv_obj_set_scrollbar_mode(strip, LV_SCROLLBAR_MODE_OFF);
    lv_obj_remove_flag(strip, LV_OBJ_FLAG_GESTURE_BUBBLE); /* a swipe here scrolls the days, not the tabs */
    for (int i = 0; i < EVENT_FORM_DAYS; i++) {
        day_tile(strip, i);
    }

    /* the time */
    card = ui_card(N.when_scroll);
    ui_section_head(card, NULL, "TIME", C_TEXT_2, NULL, NULL, NULL);
    lv_obj_t *row = card_body(card, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(row, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_bottom(row, SP_5, 0);
    lv_obj_t *minus = ui_choice(row, "\xe2\x88\x92" "15 min", STEP_W, 88, step_clicked, (void *)(intptr_t)-EVENT_STEP_MIN);
    N.time_label = lv_label_create(row);
    lv_obj_set_style_text_font(N.time_label, F.display, 0);
    lv_obj_t *plus = ui_choice(row, "+15 min", STEP_W, 88, step_clicked, (void *)(intptr_t)EVENT_STEP_MIN);
    lv_obj_set_style_radius(minus, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_radius(plus, LV_RADIUS_CIRCLE, 0);
    lv_obj_t *grid = card_body(card, LV_FLEX_FLOW_ROW_WRAP);
    for (int i = 0; i < EVENT_SLOTS; i++) {
        char s[8];
        snprintf(s, sizeof(s), "%02d:00", EVENT_FIRST_SLOT + i);
        N.hours[i] = ui_choice(grid, s, GRID_W, UI_TOUCH_MIN, hour_clicked, (void *)(intptr_t)i);
    }

    /* the duration */
    card = ui_card(N.when_scroll);
    ui_section_head(card, NULL, "HOW LONG", C_TEXT_2, NULL, NULL, NULL);
    lv_obj_t *durs = card_body(card, LV_FLEX_FLOW_ROW);
    for (int i = 0; i < 4; i++) {
        N.durs[i] = ui_choice(durs, DURATION_LABELS[i], GRID_W, UI_TOUCH_MIN, dur_clicked, (void *)(intptr_t)i);
    }

    lv_obj_t *next = ui_button(N.when_scroll, NULL, "Next: the title  " ICON_CHEVRON_RIGHT, UI_BUTTON_PRIMARY,
                               next_clicked, NULL);
    lv_obj_set_style_margin_top(next, SP_2, 0);
    show_when(false);
    lv_obj_update_layout(strip);
    lv_obj_scroll_to_view(N.days[N.form.day - N.today], LV_ANIM_OFF);
}

static lv_obj_t *when_scroller(lv_obj_t *page)
{
    (void)page;
    return N.when_scroll;
}

static void when_destroy(void *arg)
{
    (void)arg;
    N.when_scroll = NULL;
}

static const ui_detail_t WHEN = {
    .build = when_build,
    .destroy = when_destroy,
    .scroller = when_scroller,
};

/* ---- What ---- */

static int n_templates(const feed_t *f)
{
    return f && f->n_templates ? f->n_templates : (int)(sizeof(DEFAULT_TEMPLATES) / sizeof(DEFAULT_TEMPLATES[0]));
}

static const char *template_at(const feed_t *f, int i)
{
    return f && f->n_templates ? f->templates[i] : DEFAULT_TEMPLATES[i];
}

static void show_what(void)
{
    const char *t = lv_textarea_get_text(N.field);
    event_form_set_title(&N.form, t);
    const size_t n = utf8_chars(t);
    lv_label_set_text_fmt(N.counter, "%u / %d", (unsigned)n, EVENT_TITLE_MAX_CHARS);
    lv_obj_set_style_text_color(N.counter, C(n >= EVENT_TITLE_MAX_CHARS ? C_ACCENT : C_TEXT_3), 0);
    const feed_t *f = app_feed();
    for (int i = 0; i < N.n_chips; i++) {
        ui_choice_set(N.chips[i], strcmp(template_at(f, i), t) == 0);
    }
    const bool ok = event_form_validate(&N.form, N.today) == EVENT_FORM_OK;
    ui_button_set_enabled(N.add, ok);
    if (ok) {
        ui_text_set(N.error, "");
    }
}

static void field_changed(lv_event_t *e)
{
    (void)e;
    show_what();
}

static void chip_clicked(lv_event_t *e)
{
    const int i = (int)(intptr_t)lv_event_get_user_data(e);
    lv_textarea_set_text(N.field, template_at(app_feed(), i));
    show_what();
}

static void summary_clicked(lv_event_t *e)
{
    (void)e;
    ui_pop();
}

/* the newest queued cal_add: the one just added */
static uint32_t newest_add(void)
{
    const app_pending_t *p;
    const int np = app_pending(&p);
    uint32_t id = 0;
    for (int i = 0; i < np; i++) {
        if (p[i].info.type == PDA_ACT_CAL_ADD) {
            id = p[i].id;
        }
    }
    return id;
}

static void add_clicked(lv_event_t *e)
{
    (void)e;
    event_form_set_title(&N.form, lv_textarea_get_text(N.field));
    const event_form_err_t err = event_form_validate(&N.form, app_today());
    if (err != EVENT_FORM_OK) {
        ui_text_set(N.error, event_form_err_text(err));
        if (err == EVENT_FORM_NO_TITLE) {
            ui_keyboard_open(N.field);
        }
        return;
    }
    char json[PDA_ACTION_BUF];
    /* the chosen day's offset: after a DST change it differs from today's */
    const size_t n = event_form_cal_add(&N.form, app_today(), app_tz_min_on(N.form.day), json);
    const int rc = n ? app_queue(json, n) : -2;
    if (rc != 0) {
        ui_toast(UI_TOAST_ERROR, rc == -1 ? "Outbox full: sync first" : "Couldn't save the event");
        return;
    }
    ui_keyboard_close();
    const uint32_t id = newest_add();
    ui_show_tab(UI_TAB_CALENDAR, false);
    calendar_show_added(id);
    ui_toast(UI_TOAST_OK, "Event added");
}

static void what_build(lv_obj_t *page, void *arg)
{
    (void)arg;
    N.what_scroll = ui_page_scroller(page);
    ui_page_title(N.what_scroll, "What?", "Step 2 of 2");

    /* when, from step 1: a tap goes back to it */
    lv_obj_t *card = ui_card(N.what_scroll);
    ui_row_t *r = ui_row(card, summary_clicked, NULL);
    ui_focusable(r->row, false);
    lv_obj_set_flex_align(r->row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_t *i = lv_label_create(r->left);
    lv_label_set_text(i, ICON_CALENDAR_DAYS);
    lv_obj_set_style_text_font(i, F.icon, 0);
    lv_obj_set_style_text_color(i, C(C_ACCENT), 0);
    char day[24], d[24], time[32], when[64];
    agenda_day_name(N.form.day, N.today, day, sizeof(day));
    pda_fmt_day(N.form.day, d, sizeof(d));
    if (strcmp(day, d) != 0) {
        snprintf(when, sizeof(when), "%s, %s", day, d); /* "Tomorrow, Wed 21 Oct" */
    } else {
        snprintf(when, sizeof(when), "%s", d);
    }
    event_form_time_text(&N.form, time, sizeof(time));
    const int32_t w = UI_CARD_W - 2 * UI_CARD_PAD - UI_LEFT_COL - SP_4 - 40;
    N.summary = ui_text(r->mid, F.body_b, C_TEXT, when, w, 0);
    ui_text(r->mid, F.small, C_TEXT_2, time, w, 0);
    lv_obj_remove_flag(r->right, LV_OBJ_FLAG_HIDDEN);
    lv_obj_t *chev = lv_label_create(r->right);
    lv_label_set_text(chev, ICON_CHEVRON_RIGHT);
    lv_obj_set_style_text_font(chev, F.icon_s, 0);
    lv_obj_set_style_text_color(chev, C(C_TEXT_3), 0);

    /* the title */
    card = ui_card(N.what_scroll);
    ui_section_head(card, NULL, "TITLE", C_TEXT_2, NULL, NULL, NULL);
    lv_obj_t *body = card_body(card, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(body, SP_2, 0);
    N.field = ui_text_field(body, "What is it?", EVENT_TITLE_MAX_CHARS, UI_KB_TEXT);
    lv_obj_add_event_cb(N.field, field_changed, LV_EVENT_VALUE_CHANGED, NULL);
    lv_obj_t *foot = lv_obj_create(body);
    lv_obj_remove_style_all(foot);
    lv_obj_remove_flag(foot, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(foot, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(foot, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(foot, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_hor(foot, SP_1, 0);
    N.error = ui_text(foot, F.small, C_DANGER, "", UI_CARD_INNER_W - 140, 0);
    N.counter = lv_label_create(foot);
    lv_obj_set_style_text_font(N.counter, F.caption, 0);

    /* one-tap titles */
    card = ui_card(N.what_scroll);
    ui_section_head(card, NULL, "QUICK TITLES", C_TEXT_2, NULL, NULL, NULL);
    lv_obj_t *chips = card_body(card, LV_FLEX_FLOW_ROW_WRAP);
    const feed_t *f = app_feed();
    N.n_chips = n_templates(f);
    for (int k = 0; k < N.n_chips; k++) {
        N.chips[k] = ui_choice(chips, template_at(f, k), LV_SIZE_CONTENT, UI_TOUCH_MIN, chip_clicked, (void *)(intptr_t)k);
        lv_obj_set_style_radius(N.chips[k], LV_RADIUS_CIRCLE, 0);
    }

    N.add = ui_button(N.what_scroll, ICON_CALENDAR_DAYS, "Add to calendar", UI_BUTTON_PRIMARY, add_clicked, NULL);
    lv_obj_set_style_margin_top(N.add, SP_2, 0);
    if (N.form.title[0]) {
        lv_textarea_set_text(N.field, N.form.title);
    }
    show_what();
}

static lv_obj_t *what_scroller(lv_obj_t *page)
{
    (void)page;
    return N.what_scroll;
}

static void what_destroy(void *arg)
{
    (void)arg;
    ui_keyboard_close();
    N.what_scroll = NULL;
}

static const ui_detail_t WHAT = {
    .build = what_build,
    .destroy = what_destroy,
    .scroller = what_scroller,
};

void event_new_open_for(const char *title, int32_t day, int64_t from_item)
{
    N.today = app_today();
    event_form_init(&N.form, N.today, app_now(), app_tz_min());
    if (day != FEED_NO_DAY && event_form_set_day(&N.form, day, N.today) && day != N.today) {
        event_form_set_hour(&N.form, 9); /* another day: the morning (T5) */
    }
    event_form_set_title(&N.form, title);
    N.form.from_item = from_item;
    ui_push("New event", &WHEN, NULL);
}

void event_new_open(void)
{
    event_new_open_for(NULL, FEED_NO_DAY, -1);
}
