/* Brief (T5 plan §4.1 `briefing`, §5 "Briefing"): the digest Athena wrote of a day, its
 * headline and summary, its highlights; "Refresh digest" queues digest_refresh for the
 * digest's date (today's when there is none), shown as requested until the next sync and
 * as "couldn't refresh" when the server refused it. Rules: brief.c. */
#include <stdio.h>
#include <string.h>

#include "app_state.h"
#include "brief.h"
#include "controls.h"
#include "pda_time.h"
#include "screens.h"
#include "theme.h"
#include "widgets.h"

static struct {
    lv_obj_t *scroll;
    lv_obj_t *refresh_icon, *refresh_title, *refresh_sub;
    const feed_t *feed;
    int32_t day;
    int state; /* the refresh row as shown: 0 none, 1 requested, 2 sent, 3 failed */
} B;

/* a paragraph that wraps with no line limit */
static lv_obj_t *para(lv_obj_t *parent, const lv_font_t *font, uint32_t color, const char *text, int32_t w,
                      int32_t line_space)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_label_set_long_mode(l, LV_LABEL_LONG_MODE_WRAP);
    lv_obj_set_width(l, w);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, C(color), 0);
    lv_obj_set_style_text_line_space(l, line_space, 0);
    lv_label_set_text(l, text);
    return l;
}

static void digest_card(const feed_t *f, int32_t today)
{
    lv_obj_t *card = ui_card(B.scroll);
    lv_obj_set_style_bg_color(card, C(C_ACCENT_TINT), 0);
    lv_obj_set_style_border_color(card, lv_color_mix(C(C_ACCENT), C(C_BG), LV_OPA_30), 0);
    lv_obj_set_style_border_width(card, 1, 0);
    lv_obj_set_style_pad_bottom(card, UI_CARD_PAD + SP_1, 0);
    const int32_t day = brief_day(f);
    char label[48] = "DIGEST";
    if (day != FEED_NO_DAY) {
        char d[24];
        brief_day_name(day, today, d, sizeof(d));
        if (day == today || day == today - 1) {
            snprintf(label, sizeof(label), "%s'S DIGEST", d);
        } else {
            snprintf(label, sizeof(label), "DIGEST OF %s", d);
        }
        for (char *c = label; *c; c++) {
            if (*c >= 'a' && *c <= 'z') {
                *c = (char)(*c - 'a' + 'A');
            }
        }
    }
    ui_section_head(card, ICON_NEWSPAPER, label, C_ACCENT, NULL, NULL, NULL);
    char head[BRIEF_HEADLINE_MAX + 1];
    const char *body;
    brief_split(f->brief_text, head, sizeof(head), &body);
    lv_obj_t *col = lv_obj_create(card);
    lv_obj_remove_style_all(col);
    lv_obj_remove_flag(col, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(col, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_style_pad_hor(col, UI_CARD_PAD, 0);
    lv_obj_set_flex_flow(col, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(col, SP_4, 0);
    if (head[0]) {
        para(col, F.body_b, C_TEXT, head, UI_CARD_INNER_W, 4);
    }
    if (body[0]) {
        para(col, F.lead, C_TEXT, body, UI_CARD_INNER_W, 8);
    }
}

static void highlights_card(const feed_t *f)
{
    if (f->n_highlights == 0) {
        return;
    }
    lv_obj_t *card = ui_card(B.scroll);
    ui_section_head(card, NULL, "HIGHLIGHTS", C_TEXT_2, NULL, NULL, NULL);
    for (int i = 0; i < f->n_highlights; i++) {
        if (i > 0) {
            lv_obj_t *h = ui_hairline(card);
            lv_obj_set_style_margin_left(h, UI_CARD_PAD + SP_8, 0);
            lv_obj_set_width(h, UI_CARD_W - UI_CARD_PAD - SP_8);
        }
        lv_obj_t *row = lv_obj_create(card);
        lv_obj_remove_style_all(row);
        lv_obj_remove_flag(row, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_size(row, LV_PCT(100), LV_SIZE_CONTENT);
        lv_obj_set_style_pad_hor(row, UI_CARD_PAD, 0);
        lv_obj_set_style_pad_ver(row, SP_4, 0);
        lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
        /* an accent dot on the first line's middle */
        lv_obj_t *dot = lv_obj_create(row);
        lv_obj_remove_style_all(dot);
        lv_obj_set_size(dot, 10, 10);
        lv_obj_set_style_radius(dot, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_color(dot, C(C_ACCENT), 0);
        lv_obj_set_style_bg_opa(dot, LV_OPA_COVER, 0);
        lv_obj_set_style_margin_top(dot, (lv_font_get_line_height(F.body) - 10) / 2, 0);
        lv_obj_set_style_margin_right(dot, SP_8 - 10, 0);
        ui_text(row, F.body, C_TEXT, f->highlights[i], UI_CARD_INNER_W - SP_8, 5);
    }
}

/* ---- refresh ---- */

static int refresh_state(const char *date)
{
    const app_pending_t *p;
    const int np = app_pending(&p);
    const app_pending_t *hit = brief_find_pending(p, np, date);
    if (hit) {
        return hit->listed ? 2 : 1;
    }
    const app_failed_t *fl;
    const int nf = app_failed(&fl);
    return brief_find_failed(fl, nf, date) ? 3 : 0;
}

static void show_refresh(void)
{
    if (!B.refresh_title) {
        return;
    }
    const feed_t *f = app_feed();
    char date[16];
    brief_refresh_date(f, app_today(), date, sizeof(date));
    B.state = refresh_state(date);
    const bool have = f && f->has_briefing;
    char sub[96];
    const char *icon = ICON_REFRESH_CW, *title = have ? "Refresh digest" : "Write today's digest";
    uint32_t icon_c = C_ACCENT, sub_c = C_TEXT_2;
    if (have) {
        char d[24];
        const int32_t day = brief_day(f);
        pda_fmt_day(day != FEED_NO_DAY ? day : app_today(), d, sizeof(d));
        snprintf(sub, sizeof(sub), "Athena writes the digest of %s again", d);
    } else {
        snprintf(sub, sizeof(sub), "From today's conversations so far");
    }
    switch (B.state) {
    case 1:
        icon = ICON_UPLOAD;
        icon_c = C_TEXT_3;
        title = "Refresh requested";
        snprintf(sub, sizeof(sub), "Waiting for the next sync");
        break;
    case 2:
        icon = ICON_UPLOAD;
        icon_c = C_TEXT_3;
        title = "Refresh requested";
        snprintf(sub, sizeof(sub), "Sent: the new digest comes with the next feed");
        break;
    case 3:
        icon = ICON_CIRCLE_ALERT;
        icon_c = C_DANGER;
        sub_c = C_DANGER;
        snprintf(sub, sizeof(sub), "Couldn't refresh. Tap to try again");
        break;
    default: break;
    }
    lv_label_set_text(B.refresh_icon, icon);
    lv_obj_set_style_text_color(B.refresh_icon, C(icon_c), 0);
    ui_text_set(B.refresh_title, title);
    lv_obj_set_style_text_color(B.refresh_title, C(B.state == 1 || B.state == 2 ? C_TEXT_2 : C_TEXT), 0);
    ui_text_set(B.refresh_sub, sub);
    lv_obj_set_style_text_color(B.refresh_sub, C(sub_c), 0);
}

static void refresh_clicked(lv_event_t *e)
{
    (void)e;
    if (B.state == 1 || B.state == 2) {
        ui_toast(UI_TOAST_INFO, "Already requested: it comes with the next sync");
        return;
    }
    char json[PDA_ACTION_BUF];
    const size_t n = brief_refresh_action(app_feed(), app_today(), json);
    const int rc = n ? app_queue(json, n) : -2;
    if (rc == 0) {
        ui_toast(UI_TOAST_OK, "Digest refresh requested");
    } else {
        ui_toast(UI_TOAST_ERROR, rc == -1 ? "Outbox full: sync first" : "Couldn't save the request");
    }
}

static void refresh_card(void)
{
    lv_obj_t *card = ui_card(B.scroll);
    ui_row_t *r = ui_row(card, refresh_clicked, NULL);
    ui_focusable(r->row, false);
    lv_obj_set_flex_align(r->row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    B.refresh_icon = lv_label_create(r->left);
    lv_obj_set_style_text_font(B.refresh_icon, F.icon, 0);
    const int32_t w = UI_CARD_W - 2 * UI_CARD_PAD - UI_LEFT_COL - SP_4;
    B.refresh_title = ui_text(r->mid, F.body_b, C_TEXT, "", w, 0);
    B.refresh_sub = ui_text(r->mid, F.small, C_TEXT_2, "", w, 2);
    show_refresh();
}

static void rebuild(void)
{
    const feed_t *f = app_feed();
    const int32_t y = lv_obj_get_scroll_y(B.scroll);
    lv_obj_clean(B.scroll);
    B.refresh_title = NULL;
    const int32_t today = app_today();
    char aside[24] = "";
    const int32_t day = brief_day(f);
    if (day != FEED_NO_DAY) {
        pda_fmt_day(day, aside, sizeof(aside));
    }
    ui_page_title(B.scroll, "Brief", aside[0] ? aside : NULL);
    if (!f) {
        ui_no_feed(B.scroll);
        return;
    }
    if (!f->has_briefing) {
        lv_obj_t *es = ui_empty_state(B.scroll, ICON_NEWSPAPER, C_TEXT_2, "No digest yet",
                                      "Athena writes a digest of each day's conversations. Ask for "
                                      "today's now, or wait for the evening.");
        lv_obj_set_style_pad_bottom(es, SP_4, 0);
        refresh_card();
        return;
    }
    digest_card(f, today);
    highlights_card(f);
    refresh_card();
    lv_obj_update_layout(B.scroll);
    lv_obj_scroll_to_y(B.scroll, y > 0 ? y : 0, LV_ANIM_OFF);
}

static void build(lv_obj_t *page)
{
    B.scroll = ui_page_scroller(page);
}

static void update(lv_obj_t *page, unsigned what)
{
    (void)page;
    const feed_t *f = app_feed();
    if ((what & UI_DIRTY_FEED) || f != B.feed || app_today() != B.day || (!f && (what & UI_DIRTY_NET))) {
        B.feed = f;
        B.day = app_today();
        rebuild();
        return;
    }
    if (what & UI_DIRTY_OUTBOX) {
        show_refresh();
    }
}

static lv_obj_t *scroller(lv_obj_t *page)
{
    (void)page;
    return B.scroll;
}

const ui_screen_t screen_brief = {
    .label = "Brief",
    .icon = ICON_NEWSPAPER,
    .build = build,
    .update = update,
    .scroller = scroller,
};
