/* Talks (T5 plan §5 Conversations): the feed's conversations of the last 48 hours (the
 * relay sends only those, newest first), one card each: who, when (relative), the title,
 * the summary's start, and how many suggested action items it has. A tap opens the
 * conversation (talk_detail.c). */
#include <stdio.h>
#include <string.h>

#include "app_state.h"
#include "screens.h"
#include "talks.h"
#include "theme.h"
#include "ui_kit.h"
#include "widgets.h"

#define AVATAR 44
#define AVATARS_MAX 3

static struct {
    lv_obj_t *scroll;
    const feed_t *feed;
    int64_t minute;
    bool stale; /* the clock moved while hidden: rebuild when shown */
} K;

static void conv_clicked(lv_event_t *e)
{
    const int i = (int)(intptr_t)lv_event_get_user_data(e);
    const feed_t *f = app_feed();
    if (f && i >= 0 && i < f->n_conv) {
        talk_detail_open(f->conv[i].id);
    }
}

static void avatars(lv_obj_t *parent, const feed_conv_t *c)
{
    lv_obj_t *stack = lv_obj_create(parent);
    lv_obj_remove_style_all(stack);
    lv_obj_remove_flag(stack, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    const int n = c->n_people < AVATARS_MAX ? c->n_people : AVATARS_MAX;
    const int32_t step = AVATAR - 14; /* they overlap */
    lv_obj_set_size(stack, n > 0 ? AVATAR + (n - 1) * step : AVATAR, AVATAR);
    for (int k = n - 1; k >= 0; k--) { /* the first on top */
        lv_obj_t *a = kit_avatar(stack, c->people[k], AVATAR);
        lv_obj_set_pos(a, k * step, 0);
    }
    if (n == 0) {
        kit_avatar(stack, "?", AVATAR);
    }
}

static int pending_for(const feed_t *f, const feed_conv_t *c, int *n_items)
{
    int idx[FEED_MAX_ACTIONS];
    const int n = talks_conv_actions(f, c->id, idx, FEED_MAX_ACTIONS);
    int pending = 0;
    for (int k = 0; k < n; k++) {
        pending += app_pending_item(f->actions[idx[k]].id) != NULL;
    }
    *n_items = n;
    return pending;
}

static void conv_card(const feed_t *f, int i)
{
    const feed_conv_t *c = &f->conv[i];
    lv_obj_t *card = ui_card(K.scroll);
    lv_obj_set_style_pad_all(card, UI_CARD_PAD, 0);
    lv_obj_set_style_pad_row(card, SP_3, 0);
    lv_obj_add_flag(card, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_style_bg_color(card, C(C_SURFACE_2), LV_STATE_PRESSED);
    lv_obj_add_event_cb(card, conv_clicked, LV_EVENT_CLICKED, (void *)(intptr_t)i);
    ui_focusable(card, false);

    /* who and when */
    lv_obj_t *top = kit_row(card, SP_3);
    lv_obj_set_width(top, LV_PCT(100));
    avatars(top, c);
    char who[120], when[48];
    talks_people(c->people, c->n_people, 2, who, sizeof(who));
    talks_rel_time(c->at, app_now(), app_tz_min(), when, sizeof(when));
    lv_obj_t *wl = ui_text(top, F.small_b, C_TEXT_2, who[0] ? who : "Someone", 0, 0);
    lv_obj_set_flex_grow(wl, 1);
    lv_obj_set_width(wl, 1); /* grow takes the rest; "…" if long */
    lv_obj_t *tl = ui_text(top, F.caption, C_TEXT_3, when, 0, 0);
    lv_obj_set_width(tl, LV_SIZE_CONTENT); /* the whole card is the tap target: no chevron */

    ui_text(card, F.body_b, C_TEXT, c->title ? c->title : "Conversation", UI_CARD_INNER_W, 2);
    if (c->summary) {
        lv_obj_t *s = ui_text(card, F.small, C_TEXT_2, c->summary, UI_CARD_INNER_W, 2);
        lv_obj_set_style_text_line_space(s, 4, 0);
        lv_obj_set_style_max_height(s, 2 * lv_font_get_line_height(F.small) + 4, 0);
    }
    int n_items;
    const int pend = pending_for(f, c, &n_items);
    if (n_items > 0 || pend > 0) {
        lv_obj_t *chips = kit_row(card, SP_2);
        lv_obj_set_style_margin_top(chips, SP_1, 0);
        if (n_items > 0) {
            char t[40];
            snprintf(t, sizeof(t), n_items == 1 ? "1 action item" : "%d action items", n_items);
            ui_chip(chips, ICON_LIST_CHECKS, t, C_ACCENT);
        }
        if (pend > 0) {
            char t[40];
            snprintf(t, sizeof(t), "%d waiting", pend);
            ui_chip(chips, ICON_UPLOAD, t, C_TEXT_2);
        }
    }
}

static void rebuild(void)
{
    const feed_t *f = app_feed();
    const int32_t y = lv_obj_get_scroll_y(K.scroll);
    lv_obj_clean(K.scroll);
    char aside[32] = "";
    if (f && f->n_conv) {
        snprintf(aside, sizeof(aside), "%d in 48 h", f->n_conv);
    }
    ui_page_title(K.scroll, "Talks", aside[0] ? aside : NULL);
    if (!f) {
        ui_no_feed(K.scroll);
        return;
    }
    if (f->n_conv == 0) {
        ui_empty_state(K.scroll, ICON_MESSAGES_SQUARE, C_TEXT_2, "No conversations",
                       "Nothing recorded in the last 48 hours. Conversations appear here once Athena "
                       "has summarised them.");
        return;
    }
    for (int i = 0; i < f->n_conv; i++) {
        conv_card(f, i);
    }
    lv_obj_update_layout(K.scroll);
    lv_obj_scroll_to_y(K.scroll, y > 0 ? y : 0, LV_ANIM_OFF);
}

static void build(lv_obj_t *page)
{
    K.scroll = ui_page_scroller(page);
}

static void update(lv_obj_t *page, unsigned what)
{
    (void)page;
    const int64_t minute = app_now() / 60;
    if (what & (UI_DIRTY_FEED | UI_DIRTY_OUTBOX) || app_feed() != K.feed ||
        ((what & UI_DIRTY_NET) && !app_feed())) {
        K.feed = app_feed();
        K.minute = minute;
        K.stale = false;
        rebuild();
        return;
    }
    if ((what & UI_DIRTY_CLOCK) && minute != K.minute) {
        K.minute = minute;
        K.stale = true; /* "12 min ago" moved on */
    }
    if (K.stale && ((what & UI_DIRTY_SHOWN) || ui_tab_visible(UI_TAB_TALKS))) {
        K.stale = false;
        rebuild();
    }
}

static lv_obj_t *scroller(lv_obj_t *page)
{
    (void)page;
    return K.scroll;
}

const ui_screen_t screen_talks = {
    .label = "Talks",
    .icon = ICON_MESSAGES_SQUARE,
    .build = build,
    .update = update,
    .scroller = scroller,
};
