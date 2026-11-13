/* Calendar (T5 plan §5 "Calendar"): the agenda from today to +7 days, a card per day
 * ("Today", "Tomorrow", then the date), all-day events first, times in the display offset,
 * the place under the title. A queued cal_add shows at once as pending (tap: undo it while
 * syncd hasn't taken it); one the server refused shows under "Couldn't add". A tap on an
 * event opens its sheet; the round "+" starts a new event (event_new.c). The grouping is
 * agenda.c's. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "agenda.h"
#include "app_state.h"
#include "calendar_ui.h"
#include "controls.h"
#include "pda_time.h"
#include "screens.h"
#include "theme.h"
#include "widgets.h"

#define ROW_W (UI_CARD_W - 2 * UI_CARD_PAD - UI_LEFT_COL - SP_4 - 40)

static struct {
    lv_obj_t *page, *scroll, *fab;
    const feed_t *feed;
    int32_t day;
    uint32_t sig; /* the pending cal_adds and failures on screen */
    agenda_row_t rows[AGENDA_MAX];
    int n;
    uint32_t flash_id;
    lv_obj_t *flashed; /* the row this rebuild flashes (it scrolls there) */
} CAL;

/* ---- pending and failed: what changed? ---- */

static uint32_t outbox_sig(void)
{
    uint32_t h = 2166136261u;
    const app_pending_t *p;
    const int np = app_pending(&p);
    for (int i = 0; i < np; i++) {
        if (p[i].info.type == PDA_ACT_CAL_ADD) {
            h = (h ^ (p[i].id * 2u + p[i].listed)) * 16777619u;
        }
    }
    const app_failed_t *fl;
    const int nf = app_failed(&fl);
    for (int i = 0; i < nf; i++) {
        h = (h ^ (uint32_t)(fl[i].type + 100)) * 16777619u;
    }
    return h;
}

/* ---- rows ---- */

static void event_clicked(lv_event_t *e)
{
    sheet_event_open((int)(intptr_t)lv_event_get_user_data(e), NULL);
}

static void inset_rule(lv_obj_t *card)
{
    lv_obj_t *h = ui_hairline(card);
    lv_obj_set_style_margin_left(h, UI_CARD_PAD + UI_LEFT_COL + SP_4, 0);
    lv_obj_set_width(h, UI_CARD_W - UI_CARD_PAD - UI_LEFT_COL - SP_4);
}

static void chevron(ui_row_t *r)
{
    lv_obj_remove_flag(r->right, LV_OBJ_FLAG_HIDDEN);
    lv_obj_t *c = lv_label_create(r->right);
    lv_label_set_text(c, ICON_CHEVRON_RIGHT);
    lv_obj_set_style_text_font(c, F.icon_s, 0);
    lv_obj_set_style_text_color(c, C(C_TEXT_3), 0);
}

/* the time column: start over end, "Now" while on, or "All day" */
static void time_column(lv_obj_t *left, const agenda_row_t *a, uint32_t color)
{
    if (a->all_day) {
        lv_obj_t *t = ui_text(left, F.caption, color == C_TEXT ? C_TEXT_2 : color, "All day", UI_LEFT_COL, 0);
        lv_obj_set_style_margin_top(t, 5, 0);
        return;
    }
    char hm[12];
    pda_fmt_hm(a->start, app_tz_min(), hm, sizeof(hm));
    lv_obj_t *t = ui_text(left, F.small_b, color, hm, UI_LEFT_COL, 0);
    lv_obj_set_style_margin_top(t, 3, 0);
    if (a->now) {
        ui_text(left, F.caption, C_ACCENT, "Now", UI_LEFT_COL, 0);
    } else if (a->end > a->start) {
        pda_fmt_hm(a->end, app_tz_min(), hm, sizeof(hm));
        ui_text(left, F.caption, C_TEXT_3, hm, UI_LEFT_COL, 0);
    }
}

static void event_row(lv_obj_t *card, const agenda_row_t *a)
{
    const feed_event_t *e = &app_feed()->events[a->idx];
    ui_row_t *r = ui_row(card, event_clicked, (void *)(intptr_t)a->idx);
    ui_focusable(r->row, false);
    time_column(r->left, a, C_TEXT);
    ui_text(r->mid, F.body_b, C_TEXT, e->title, ROW_W, 2);
    if (e->where) {
        char w[160];
        snprintf(w, sizeof(w), ICON_MAP_PIN " %s", e->where);
        ui_text(r->mid, F.small, C_TEXT_2, w, ROW_W, 1);
    }
    chevron(r);
}

/* ---- a pending add's sheet ---- */

typedef struct {
    uint32_t id;
} pend_arg_t;

static const app_pending_t *pending_by_id(uint32_t id)
{
    const app_pending_t *p;
    const int np = app_pending(&p);
    for (int i = 0; i < np; i++) {
        if (p[i].id == id) {
            return &p[i];
        }
    }
    return NULL;
}

static void cancel_clicked(lv_event_t *e)
{
    const uint32_t id = (uint32_t)(uintptr_t)lv_event_get_user_data(e);
    ui_sheet_close();
    if (app_cancel(id) == 0) {
        ui_toast(UI_TOAST_OK, "Not added");
    } else {
        ui_toast(UI_TOAST_INFO, "Already sent to Odysseus");
    }
}

static void sheet_line(lv_obj_t *s, const char *icon, uint32_t icon_color, const char *text, uint32_t color)
{
    lv_obj_t *row = lv_obj_create(s);
    lv_obj_remove_style_all(row);
    lv_obj_remove_flag(row, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(row, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_set_style_pad_column(row, SP_4, 0);
    lv_obj_t *i = lv_label_create(row);
    lv_label_set_text(i, icon);
    lv_obj_set_style_text_font(i, F.small, 0);
    lv_obj_set_style_text_color(i, C(icon_color), 0);
    lv_obj_set_style_pad_top(i, 2, 0);
    ui_text(row, F.body, color, text, UI_W - 2 * (UI_MARGIN + SP_2) - 26 - SP_4, 4);
}

static void pending_sheet(lv_obj_t *s, void *argp)
{
    const pend_arg_t *arg = argp;
    const app_pending_t *p = pending_by_id(arg->id);
    if (!p) {
        ui_text(s, F.body, C_TEXT_2, "This event has been synced.", UI_CARD_W, 2);
        return;
    }
    const pda_action_info_t *a = &p->info;
    lv_obj_t *t = ui_text(s, F.title, C_TEXT, a->title, UI_W - 2 * (UI_MARGIN + SP_2), 3);
    lv_obj_set_style_margin_bottom(t, SP_2, 0);
    int64_t t0, t1;
    bool d0, d1;
    char when[80] = "";
    if (pda_time_parse(a->start, &t0, &d0)) {
        const bool all_day = a->all_day || d0;
        const int32_t day = all_day ? pda_local_day(t0, 0) : pda_local_day(t0, app_tz_min());
        char d[24];
        agenda_day_name(day, app_today(), d, sizeof(d));
        if (all_day) {
            snprintf(when, sizeof(when), "%s, all day", d);
        } else {
            char a0[12], a1[12] = "";
            pda_fmt_hm(t0, app_tz_min(), a0, sizeof(a0));
            if (a->end[0] && pda_time_parse(a->end, &t1, &d1)) {
                pda_fmt_hm(t1, app_tz_min(), a1, sizeof(a1));
            }
            snprintf(when, sizeof(when), "%s %s%s%s", d, a0, a1[0] ? " \xe2\x80\x93 " : "", a1);
        }
    }
    sheet_line(s, ICON_CLOCK, C_TEXT_2, when, C_TEXT);
    if (p->listed) {
        sheet_line(s, ICON_UPLOAD, C_ACCENT, "On its way to Odysseus. It shows as a normal event after the next sync.",
                   C_TEXT_2);
        return;
    }
    sheet_line(s, ICON_UPLOAD, C_ACCENT, "Waiting for the next sync. Until then you can take it back.", C_TEXT_2);
    lv_obj_t *b = ui_button(s, ICON_UNDO, "Don't add it", UI_BUTTON_SECONDARY, cancel_clicked,
                            (void *)(uintptr_t)arg->id);
    lv_obj_set_style_text_color(b, C(C_DANGER), 0);
    lv_obj_set_style_margin_top(b, SP_4, 0);
}

static void pending_clicked(lv_event_t *e)
{
    pend_arg_t *a = calloc(1, sizeof(*a));
    if (!a) {
        return;
    }
    a->id = (uint32_t)(uintptr_t)lv_event_get_user_data(e);
    ui_sheet_open(pending_sheet, a, free);
}

static void flash_anim(void *var, int32_t v)
{
    lv_obj_set_style_bg_opa(var, (lv_opa_t)v, 0);
}

static void flash_done(lv_anim_t *a)
{
    lv_obj_remove_local_style_prop(a->var, LV_STYLE_BG_OPA, 0);
    lv_obj_remove_local_style_prop(a->var, LV_STYLE_BG_COLOR, 0);
}

static void pending_row(lv_obj_t *card, const agenda_row_t *a, const app_pending_t *p)
{
    ui_row_t *r = ui_row(card, pending_clicked, (void *)(uintptr_t)p->id);
    ui_focusable(r->row, false);
    time_column(r->left, a, C_TEXT_3);
    ui_text(r->mid, F.body_b, C_TEXT_2, p->info.title, ROW_W, 2);
    lv_obj_t *chip = ui_chip(r->mid, ICON_UPLOAD, p->listed ? "Sending" : "Waiting to sync", C_ACCENT);
    lv_obj_set_style_margin_top(chip, SP_1, 0);
    chevron(r);
    if (p->id == CAL.flash_id) {
        CAL.flash_id = 0;
        CAL.flashed = r->row;
        lv_obj_set_style_bg_color(r->row, C(C_ACCENT_TINT), 0);
        lv_obj_set_style_bg_opa(r->row, LV_OPA_COVER, 0);
        lv_anim_t an;
        lv_anim_init(&an);
        lv_anim_set_var(&an, r->row);
        lv_anim_set_exec_cb(&an, flash_anim);
        lv_anim_set_values(&an, LV_OPA_COVER, LV_OPA_TRANSP);
        lv_anim_set_delay(&an, 900);
        lv_anim_set_duration(&an, UI_ANIM_SLOW);
        lv_anim_set_completed_cb(&an, flash_done);
        lv_anim_start(&an);
    }
}

static void day_card_head(lv_obj_t *card, int32_t day, int32_t today)
{
    char name[24], date[24], label[64];
    agenda_day_name(day, today, name, sizeof(name));
    pda_fmt_day(day, date, sizeof(date));
    if (day <= today + 1) {
        snprintf(label, sizeof(label), "%s \xc2\xb7 %s", name, date);
    } else {
        snprintf(label, sizeof(label), "%s", date);
    }
    for (char *c = label; *c; c++) {
        if (*c >= 'a' && *c <= 'z') {
            *c = (char)(*c - 'a' + 'A');
        }
    }
    ui_section_head(card, NULL, label, day == today ? C_ACCENT : C_TEXT_2, NULL, NULL, NULL);
}

static void failed_card(void)
{
    const app_failed_t *fl;
    const int nf = app_failed(&fl);
    lv_obj_t *card = NULL;
    for (int i = 0; i < nf; i++) {
        if (fl[i].type != PDA_ACT_CAL_ADD) {
            continue;
        }
        if (!card) {
            card = ui_card(CAL.scroll);
            ui_section_head(card, ICON_CIRCLE_ALERT, "COULDN'T ADD", C_DANGER, NULL, NULL, NULL);
        } else {
            inset_rule(card);
        }
        ui_row_t *r = ui_row(card, NULL, NULL);
        lv_obj_t *i_l = lv_label_create(r->left);
        lv_label_set_text(i_l, ICON_CALENDAR_DAYS);
        lv_obj_set_style_text_font(i_l, F.icon_s, 0);
        lv_obj_set_style_text_color(i_l, C(C_DANGER), 0);
        ui_text(r->mid, F.body_b, C_TEXT, fl[i].label, ROW_W, 2);
        ui_text(r->mid, F.small, C_TEXT_2, "Odysseus refused it. Add it again with +.", ROW_W, 2);
    }
}

static void rebuild(void)
{
    const feed_t *f = app_feed();
    const int32_t y = lv_obj_get_scroll_y(CAL.scroll);
    lv_obj_clean(CAL.scroll);
    CAL.flashed = NULL;
    const int32_t today = app_today();
    char range[40] = "";
    if (app_time_valid()) {
        char a[24];
        pda_fmt_day(today + AGENDA_DAYS - 1, a, sizeof(a));
        snprintf(range, sizeof(range), "until %s", a);
    }
    ui_page_title(CAL.scroll, "Calendar", range[0] ? range : NULL);
    const app_pending_t *p;
    const int np = app_pending(&p);
    CAL.n = agenda_build(f, p, np, today, app_now(), app_tz_min(), CAL.rows, AGENDA_MAX);
    CAL.sig = outbox_sig();
    if (!f && CAL.n == 0) {
        ui_no_feed(CAL.scroll);
        return;
    }
    lv_obj_t *card = NULL;
    bool first = true;
    for (int i = 0; i < CAL.n; i++) {
        const agenda_row_t *a = &CAL.rows[i];
        if (a->kind == AGENDA_DAY) {
            card = ui_card(CAL.scroll);
            day_card_head(card, a->day, today);
            first = true;
            continue;
        }
        if (!first) {
            inset_rule(card);
        }
        first = false;
        if (a->kind == AGENDA_EVENT) {
            event_row(card, a);
        } else {
            pending_row(card, a, &p[a->idx]);
        }
    }
    failed_card();
    if (CAL.n == 0) {
        char d[24], text[160];
        pda_fmt_day(today + AGENDA_DAYS - 1, d, sizeof(d));
        snprintf(text, sizeof(text), "No events from today to %s. Tap + to add one.", d);
        ui_empty_state(CAL.scroll, ICON_CALENDAR_DAYS, C_TEXT_2, "Nothing planned", text);
    }
    lv_obj_update_layout(CAL.scroll);
    if (CAL.flashed) {
        /* the new event in the middle of the page */
        lv_area_t ra, sa;
        lv_obj_get_coords(CAL.flashed, &ra);
        lv_obj_get_coords(CAL.scroll, &sa);
        lv_obj_scroll_by_bounded(CAL.scroll, 0, (sa.y1 + sa.y2) / 2 - (ra.y1 + ra.y2) / 2, LV_ANIM_OFF);
    } else if (y > 0) {
        lv_obj_scroll_to_y(CAL.scroll, y, LV_ANIM_OFF);
    }
    CAL.flash_id = 0;
}

/* ---- the page ---- */

static void fab_clicked(lv_event_t *e)
{
    (void)e;
    if (!app_time_valid()) {
        ui_toast(UI_TOAST_ERROR, "The clock isn't set yet");
        return;
    }
    event_new_open();
}

void calendar_show_added(uint32_t action_id)
{
    CAL.flash_id = action_id;
    if (CAL.scroll) {
        rebuild();
    }
}

static void build(lv_obj_t *page)
{
    CAL.page = page;
    CAL.scroll = ui_page_scroller(page);
    lv_obj_set_style_pad_bottom(CAL.scroll, UI_FAB_D + 2 * UI_MARGIN, 0); /* the last row clears the + */
    CAL.fab = ui_fab(page, fab_clicked, NULL);
}

static void update(lv_obj_t *page, unsigned what)
{
    (void)page;
    const feed_t *f = app_feed();
    bool redo = (what & UI_DIRTY_FEED) || f != CAL.feed || app_today() != CAL.day;
    if (!redo && (what & UI_DIRTY_OUTBOX)) {
        redo = outbox_sig() != CAL.sig;
    }
    if (!redo && (what & UI_DIRTY_CLOCK)) {
        /* a new minute: only when an event ended or began */
        static agenda_row_t now_rows[AGENDA_MAX];
        const app_pending_t *p;
        const int np = app_pending(&p);
        const int n = agenda_build(f, p, np, app_today(), app_now(), app_tz_min(), now_rows, AGENDA_MAX);
        redo = n != CAL.n;
        for (int i = 0; i < n && !redo; i++) {
            const agenda_row_t *a = &now_rows[i], *b = &CAL.rows[i];
            redo = a->kind != b->kind || a->idx != b->idx || a->day != b->day || a->now != b->now;
        }
    }
    if (!redo && (what & UI_DIRTY_NET) && !f) {
        redo = true; /* the "no feed" text follows the link */
    }
    if (redo) {
        CAL.feed = f;
        CAL.day = app_today();
        rebuild();
    }
}

static lv_obj_t *scroller(lv_obj_t *page)
{
    (void)page;
    return CAL.scroll;
}

const ui_screen_t screen_calendar = {
    .label = "Calendar",
    .icon = ICON_CALENDAR_DAYS,
    .build = build,
    .update = update,
    .scroller = scroller,
};
