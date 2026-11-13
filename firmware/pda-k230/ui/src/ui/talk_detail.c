/* A conversation (T5 plan §5 "Conversation"), pushed over Talks: the title, who and when,
 * the summary, the key points, its suggested action items (tap: done, hold: dismiss, tap a
 * waiting one: undo, as on the T5) and the way to the transcript.
 *
 * The transcript (§4.3): pages of 10 minutes (transcript:<id>@<from>-<to>), from the
 * detail cache, or asked for with a `fetch` action and shown when the blob arrives
 * (UI_DIRTY_DETAIL); chat bubbles, the owner's lines (`me`) on the right; "Next part" and a
 * swipe left follow `next` in place; swipe right / back returns. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app_state.h"
#include "icons_extra.h"
#include "pda_actions.h"
#include "screens.h"
#include "talks.h"
#include "theme.h"
#include "ui_kit.h"
#include "widgets.h"

#define ITEMS_MAX 12
#define BUBBLE_MAX_W (UI_CARD_W * 4 / 5)

/* ==================== the conversation ==================== */

typedef struct {
    int idx; /* f->actions */
    int64_t id;
    lv_obj_t *row, *tick, *x, *title, *sub, *mark;
    talks_item_view_t shown;
    bool have_shown;
} item_row_t;

typedef struct {
    char id[PDA_REF_MAX];
    lv_obj_t *scroll;
    const feed_t *feed;
    item_row_t items[ITEMS_MAX];
    int n_items;
    bool long_fired;
} conv_arg_t;

static const feed_conv_t *find_conv(const char *id)
{
    const feed_t *f = app_feed();
    for (int i = 0; f && i < f->n_conv; i++) {
        if (f->conv[i].id && strcmp(f->conv[i].id, id) == 0) {
            return &f->conv[i];
        }
    }
    return NULL;
}

static void item_refresh(item_row_t *r, bool animate)
{
    const feed_t *f = app_feed();
    if (!f || r->idx >= f->n_actions) {
        return;
    }
    const feed_action_t *a = &f->actions[r->idx];
    talks_item_view_t v;
    talks_item_view(a, &v);
    if (r->have_shown && memcmp(&v, &r->shown, sizeof(v)) == 0) {
        return;
    }
    const bool anim = animate && r->have_shown && v.state != r->shown.state;
    r->shown = v;
    r->have_shown = true;
    const bool closed = v.state != TALKS_ITEM_OPEN;
    ui_tick_set(r->tick, v.state == TALKS_ITEM_DONE, anim);
    ui_show(r->x, v.state == TALKS_ITEM_DISMISSED);
    lv_obj_set_style_text_decor(r->title, closed ? LV_TEXT_DECOR_STRIKETHROUGH : LV_TEXT_DECOR_NONE, 0);
    lv_obj_set_style_text_color(r->title, C(closed ? C_TEXT_3 : C_TEXT), 0);
    char line[160];
    talks_item_line(a, &v, app_today(), line, sizeof(line));
    if (v.failed && !v.pending) {
        char l2[180];
        snprintf(l2, sizeof(l2), ICON_CIRCLE_ALERT " %s", line);
        ui_text_set(r->sub, l2);
    } else {
        ui_text_set(r->sub, line);
    }
    const bool overdue = !v.pending && a->due_day != FEED_NO_DAY && a->due_day < app_today();
    lv_obj_set_style_text_color(r->sub, C(v.failed && !v.pending ? C_DANGER
                                          : v.pending           ? C_ACCENT
                                          : overdue             ? C_DANGER
                                          : closed              ? C_TEXT_3
                                                                : C_TEXT_2),
                                0);
    ui_show(r->sub, line[0] != 0);
    ui_show(r->mark, v.pending);
}

static void item_tap(item_row_t *r, bool long_press)
{
    switch (talks_item_tap(r->idx, long_press)) {
    case TALKS_TAP_QUEUED: break; /* the tick says it */
    case TALKS_TAP_UNDONE: ui_toast(UI_TOAST_INFO, "Undone"); break;
    case TALKS_TAP_REOPEN: ui_toast(UI_TOAST_INFO, "Reopening it with the next sync"); break;
    case TALKS_TAP_FULL: ui_toast(UI_TOAST_ERROR, "Outbox full: sync first"); break;
    case TALKS_TAP_ERROR: ui_toast(UI_TOAST_ERROR, "Couldn't save the change"); break;
    }
}

static void item_event(lv_event_t *e)
{
    item_row_t *r = lv_event_get_user_data(e);
    const lv_event_code_t code = lv_event_get_code(e);
    if (code == LV_EVENT_LONG_PRESSED) {
        item_tap(r, true);
    } else if (code == LV_EVENT_SHORT_CLICKED) {
        item_tap(r, false);
    }
}

static void item_row(lv_obj_t *card, conv_arg_t *c, int idx)
{
    const feed_t *f = app_feed();
    if (c->n_items == ITEMS_MAX) {
        return;
    }
    item_row_t *r = &c->items[c->n_items++];
    memset(r, 0, sizeof(*r));
    r->idx = idx;
    r->id = f->actions[idx].id;
    ui_row_t *row = ui_row(card, NULL, NULL);
    r->row = row->row;
    lv_obj_add_flag(r->row, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(r->row, item_event, LV_EVENT_SHORT_CLICKED, r);
    lv_obj_add_event_cb(r->row, item_event, LV_EVENT_LONG_PRESSED, r);
    ui_focusable(r->row, false);
    lv_obj_set_width(row->left, UI_TICK);
    lv_obj_set_flex_align(row->left, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    r->tick = ui_tick(row->left);
    /* dismissed: an x in the ring */
    r->x = kit_icon(r->tick, F.icon_s, ICON_X, C_TEXT_3);
    lv_obj_center(r->x);
    lv_obj_add_flag(r->x, LV_OBJ_FLAG_HIDDEN);
    lv_obj_set_style_pad_top(row->mid, 4, 0);
    const int32_t w = UI_CARD_W - 2 * UI_CARD_PAD - UI_TICK - SP_4 - 40;
    r->title = ui_text(row->mid, F.body, C_TEXT, f->actions[idx].text, w, 3);
    r->sub = ui_text(row->mid, F.small, C_TEXT_2, "", w, 2);
    r->mark = kit_icon(row->right, F.icon_s, ICON_UPLOAD, C_TEXT_3);
    item_refresh(r, false);
}

static void transcript_clicked(lv_event_t *e)
{
    const conv_arg_t *c = lv_event_get_user_data(e);
    char ref[PDA_REF_MAX];
    if (talks_transcript_ref(c->id, ref, sizeof(ref))) {
        const feed_conv_t *cv = find_conv(c->id);
        talk_transcript_open(ref, cv && cv->title ? cv->title : "Transcript");
    }
}

static void section(lv_obj_t *card, const char *icon, const char *label)
{
    ui_section_head(card, icon, label, C_TEXT_2, NULL, NULL, NULL);
}

static void conv_rebuild(conv_arg_t *c)
{
    const int32_t y = lv_obj_get_scroll_y(c->scroll);
    lv_obj_clean(c->scroll);
    c->n_items = 0;
    c->feed = app_feed();
    const feed_conv_t *cv = find_conv(c->id);
    if (!cv) {
        ui_empty_state(c->scroll, ICON_MESSAGES_SQUARE, C_TEXT_2, "Not in the feed",
                       "This conversation is older than 48 hours now, or was removed in Odysseus.");
        return;
    }
    /* the title, who and when */
    lv_obj_t *head = kit_col(c->scroll, SP_3);
    lv_obj_set_width(head, LV_PCT(100));
    lv_obj_set_style_pad_hor(head, SP_1, 0);
    lv_obj_t *t = ui_text(head, F.title, C_TEXT, cv->title ? cv->title : "Conversation", UI_CARD_W - SP_2, 3);
    lv_obj_set_style_text_line_space(t, 0, 0);
    lv_obj_t *meta = kit_row(head, SP_3);
    lv_obj_set_width(meta, LV_PCT(100));
    for (int k = 0; k < cv->n_people && k < 3; k++) {
        lv_obj_t *a = kit_avatar(meta, cv->people[k], 40);
        if (k > 0) {
            lv_obj_set_style_margin_left(a, -SP_6, 0);
        }
    }
    char who[120], when[48], line[200];
    talks_people(cv->people, cv->n_people, 3, who, sizeof(who));
    talks_rel_time(cv->at, app_now(), app_tz_min(), when, sizeof(when));
    snprintf(line, sizeof(line), "%s%s%s", who, who[0] && when[0] ? " \xc2\xb7 " : "", when);
    lv_obj_t *ml = ui_text(meta, F.small, C_TEXT_2, line, 0, 0);
    lv_obj_set_flex_grow(ml, 1);
    lv_obj_set_width(ml, 1);

    if (cv->summary) {
        lv_obj_t *card = ui_card(c->scroll);
        section(card, ICON_SPARKLES, "SUMMARY");
        lv_obj_t *b = kit_card_block(card, 0);
        lv_obj_set_style_pad_top(b, 0, 0);
        lv_obj_set_style_pad_bottom(b, SP_5, 0);
        lv_obj_t *s = ui_text(b, F.body, C_TEXT, cv->summary, UI_CARD_INNER_W, 14);
        lv_obj_set_style_text_line_space(s, 6, 0);
        lv_obj_set_style_max_height(s, 14 * lv_font_get_line_height(F.body) + 13 * 6, 0);
    }
    if (cv->n_points) {
        lv_obj_t *card = ui_card(c->scroll);
        section(card, NULL, "KEY POINTS");
        lv_obj_t *b = kit_card_block(card, SP_4);
        lv_obj_set_style_pad_top(b, 0, 0);
        lv_obj_set_style_pad_bottom(b, SP_5, 0);
        for (int k = 0; k < cv->n_points; k++) {
            lv_obj_t *r = kit_row(b, SP_4);
            lv_obj_set_flex_align(r, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
            lv_obj_t *dot = lv_obj_create(r);
            lv_obj_remove_style_all(dot);
            lv_obj_set_size(dot, 10, 10);
            lv_obj_set_style_margin_top(dot, 14, 0);
            lv_obj_set_style_radius(dot, LV_RADIUS_CIRCLE, 0);
            lv_obj_set_style_bg_color(dot, C(C_ACCENT), 0);
            lv_obj_set_style_bg_opa(dot, LV_OPA_COVER, 0);
            ui_text(r, F.body, C_TEXT, cv->points[k], UI_CARD_INNER_W - 10 - SP_4, 4);
        }
    }
    const feed_t *f = app_feed();
    int idx[ITEMS_MAX];
    const int n = talks_conv_actions(f, cv->id, idx, ITEMS_MAX);
    if (n > 0) {
        lv_obj_t *card = ui_card(c->scroll);
        section(card, ICON_LIST_CHECKS, "SUGGESTED ACTION ITEMS");
        for (int k = 0; k < n; k++) {
            if (k > 0) {
                lv_obj_t *h = ui_hairline(card);
                lv_obj_set_style_margin_left(h, UI_CARD_PAD + UI_TICK + SP_4, 0);
                lv_obj_set_width(h, UI_CARD_W - UI_CARD_PAD - UI_TICK - SP_4);
            }
            item_row(card, c, idx[k]);
        }
        lv_obj_t *hint = kit_card_block(card, 0);
        lv_obj_set_style_pad_top(hint, 0, 0);
        lv_obj_set_style_pad_bottom(hint, SP_4, 0);
        ui_text(hint, F.caption, C_TEXT_3, "Tap: done  \xc2\xb7  hold: dismiss  \xc2\xb7  tap again: undo",
                UI_CARD_INNER_W, 2);
    }
    /* the transcript */
    char ref[PDA_REF_MAX];
    talks_transcript_ref(cv->id, ref, sizeof(ref));
    detail_t *cached = app_load_detail(ref);
    const bool have = cached != NULL;
    if (cached) {
        detail_free(cached);
        free(cached);
    }
    lv_obj_t *tb = kit_col(c->scroll, SP_2);
    lv_obj_set_width(tb, LV_PCT(100));
    kit_button(tb, KIT_SECONDARY, F.icon_s, ICON_MESSAGES_SQUARE, "Read the transcript", transcript_clicked, c);
    lv_obj_t *note = ui_text(tb, F.caption, C_TEXT_3,
                             have                        ? "On this PDA"
                             : app_pending_fetch(ref)    ? "Requested: it comes with the next sync"
                                                         : "Fetched from Athena with the next sync",
                             UI_CARD_W, 1);
    lv_obj_set_style_text_align(note, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_update_layout(c->scroll);
    lv_obj_scroll_to_y(c->scroll, y > 0 ? y : 0, LV_ANIM_OFF);
}

static void conv_build(lv_obj_t *page, void *argp)
{
    conv_arg_t *c = argp;
    c->scroll = ui_page_scroller(page);
    conv_rebuild(c);
}

static void conv_update(lv_obj_t *page, void *argp, unsigned what)
{
    (void)page;
    conv_arg_t *c = argp;
    if ((what & UI_DIRTY_FEED) || app_feed() != c->feed) {
        conv_rebuild(c);
        return;
    }
    if (what & UI_DIRTY_OUTBOX) {
        for (int i = 0; i < c->n_items; i++) {
            item_refresh(&c->items[i], true);
        }
    }
    if (what & (UI_DIRTY_DETAIL | UI_DIRTY_SHOWN)) {
        conv_rebuild(c); /* "On this PDA" under the transcript button */
    }
}

static lv_obj_t *conv_scroller(lv_obj_t *page)
{
    return lv_obj_get_child(page, 0);
}

static const ui_detail_t CONV = {
    .build = conv_build,
    .update = conv_update,
    .destroy = free,
    .scroller = conv_scroller,
};

void talk_detail_open(const char *conv_id)
{
    conv_arg_t *c = calloc(1, sizeof(*c));
    if (!c) {
        return;
    }
    snprintf(c->id, sizeof(c->id), "%s", conv_id);
    ui_push("Conversation", &CONV, c);
}

/* ==================== the transcript ==================== */

typedef struct {
    char ref[PDA_REF_MAX];
    char title[160];
    lv_obj_t *scroll;
    detail_t *d;
    int part; /* 0 the first page */
} tr_arg_t;

static tr_arg_t *s_tr_live; /* the open transcript (one at a time) */

static void tr_free_detail(tr_arg_t *t)
{
    if (t->d) {
        detail_free(t->d);
        free(t->d);
        t->d = NULL;
    }
}

static void tr_destroy(void *argp)
{
    tr_arg_t *t = argp;
    if (s_tr_live == t) {
        s_tr_live = NULL;
    }
    tr_free_detail(t);
    free(t);
}

static void spin(void *var, int32_t v)
{
    lv_obj_set_style_transform_rotation(var, v, 0);
}

static void sync_clicked(lv_event_t *e)
{
    (void)e;
    ui_request_sync();
}

static void tr_rebuild(tr_arg_t *t);

static void fetch_clicked(lv_event_t *e)
{
    tr_arg_t *t = lv_event_get_user_data(e);
    char json[PDA_ACTION_BUF];
    const size_t n = pda_action_fetch(json, t->ref);
    const int rc = n ? app_queue(json, n) : -2;
    if (rc != 0) {
        ui_toast(UI_TOAST_ERROR, rc == -1 ? "Outbox full: sync first" : "Couldn't ask for the transcript");
    }
    /* the queued fetch is an OUTBOX change: tr_update redraws (not here, inside the tap) */
}

static void next_clicked(lv_event_t *e);

static void waiting(tr_arg_t *t)
{
    const bool pending = app_pending_fetch(t->ref) != NULL;
    lv_obj_t *box = kit_col(t->scroll, SP_4);
    lv_obj_set_width(box, LV_PCT(100));
    lv_obj_set_style_pad_top(box, SP_12 * 2, 0);
    lv_obj_set_flex_align(box, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_t *ring = lv_obj_create(box);
    lv_obj_remove_style_all(ring);
    lv_obj_remove_flag(ring, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(ring, 168, 168);
    lv_obj_set_style_radius(ring, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(ring, lv_color_mix(C(C_ACCENT), C(C_BG), LV_OPA_20), 0);
    lv_obj_set_style_bg_opa(ring, LV_OPA_COVER, 0);
    lv_obj_set_style_margin_bottom(ring, SP_4, 0);
    lv_obj_t *ic = kit_icon(ring, F.icon_xl, pending ? ICON_LOADER : ICON_MESSAGES_SQUARE, C_ACCENT);
    lv_obj_center(ic);
    if (pending) {
        lv_obj_update_layout(ring);
        lv_obj_set_style_transform_pivot_x(ic, lv_obj_get_width(ic) / 2, 0);
        lv_obj_set_style_transform_pivot_y(ic, lv_obj_get_height(ic) / 2, 0);
        lv_anim_t a;
        lv_anim_init(&a);
        lv_anim_set_var(&a, ic);
        lv_anim_set_exec_cb(&a, spin);
        lv_anim_set_values(&a, 0, 3600);
        lv_anim_set_duration(&a, 1400);
        lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
        lv_anim_start(&a);
    }
    lv_obj_t *tl = ui_text(box, F.title, C_TEXT, pending ? "On its way" : "Not on this PDA", UI_CARD_W, 1);
    lv_obj_set_style_text_align(tl, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_t *s = ui_text(box, F.small, C_TEXT_2,
                          pending ? "The transcript comes with the next sync. It opens here by itself; "
                                    "you can also come back later."
                                  : "Ask Athena for it; it arrives with the next sync.",
                          UI_CARD_W - 2 * SP_8, 4);
    lv_obj_set_style_text_align(s, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_t *bc = kit_col(box, SP_3);
    lv_obj_set_width(bc, LV_PCT(100));
    lv_obj_set_style_margin_top(bc, SP_6, 0);
    if (pending) {
        kit_button(bc, KIT_SECONDARY, F.icon_s, ICON_REFRESH_CW, "Sync now", sync_clicked, NULL);
    } else {
        kit_button(bc, KIT_PRIMARY, F.icon_s, ICON_ARROW_DOWN, "Fetch the transcript", fetch_clicked, t);
    }
}

static lv_obj_t *bubble(lv_obj_t *parent, const detail_seg_t *s, bool first_of_run)
{
    lv_obj_t *line = lv_obj_create(parent);
    lv_obj_remove_style_all(line);
    lv_obj_remove_flag(line, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(line, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(line, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(line, LV_FLEX_ALIGN_START, s->me ? LV_FLEX_ALIGN_END : LV_FLEX_ALIGN_START,
                          s->me ? LV_FLEX_ALIGN_END : LV_FLEX_ALIGN_START);
    lv_obj_set_style_pad_row(line, SP_1, 0);
    if (first_of_run) {
        lv_obj_set_style_margin_top(line, SP_3, 0);
        char who[96], off[16];
        talks_offset(s->o, off, sizeof(off));
        snprintf(who, sizeof(who), "%s  \xc2\xb7  %s", s->me ? "You" : (s->s ? s->s : "Someone"), off);
        lv_obj_t *n = ui_text(line, F.caption, s->me ? C_ACCENT : C_TEXT_2, who, 0, 0);
        lv_obj_set_width(n, LV_SIZE_CONTENT);
        lv_obj_set_style_max_width(n, BUBBLE_MAX_W, 0);
        lv_obj_set_style_pad_hor(n, SP_2, 0);
    }
    lv_obj_t *b = lv_obj_create(line);
    lv_obj_remove_style_all(b);
    lv_obj_remove_flag(b, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(b, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_style_radius(b, 26, 0);
    lv_obj_set_style_pad_hor(b, SP_5, 0);
    lv_obj_set_style_pad_ver(b, SP_3 + 2, 0);
    lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
    if (s->me) {
        lv_obj_set_style_bg_color(b, C(C_ACCENT_TINT), 0);
        lv_obj_set_style_border_color(b, lv_color_mix(C(C_ACCENT), C(C_BG), LV_OPA_30), 0);
        lv_obj_set_style_border_width(b, 1, 0);
    } else {
        lv_obj_set_style_bg_color(b, C(C_SURFACE_2), 0);
    }
    lv_obj_t *l = lv_label_create(b);
    lv_label_set_long_mode(l, LV_LABEL_LONG_MODE_WRAP);
    lv_obj_set_width(l, LV_SIZE_CONTENT);
    lv_obj_set_style_max_width(l, BUBBLE_MAX_W - 2 * SP_5, 0);
    lv_obj_set_style_text_font(l, F.body, 0);
    lv_obj_set_style_text_color(l, C(C_TEXT), 0);
    lv_obj_set_style_text_line_space(l, 4, 0);
    lv_label_set_text(l, s->x ? s->x : "");
    return line;
}

static void tr_rebuild(tr_arg_t *t)
{
    lv_obj_clean(t->scroll);
    if (!t->d) {
        t->d = app_load_detail(t->ref);
    }
    if (!t->d) {
        waiting(t);
        return;
    }
    const detail_t *d = t->d;
    lv_obj_t *head = kit_col(t->scroll, SP_1);
    lv_obj_set_width(head, LV_PCT(100));
    lv_obj_set_style_pad_hor(head, SP_1, 0);
    char part[32], cap[64];
    talks_part_label(d->from, d->to, part, sizeof(part));
    snprintf(cap, sizeof(cap), "TRANSCRIPT%s%s", part[0] ? "  \xc2\xb7  " : "", part);
    lv_obj_t *c = ui_text(head, F.caption, C_ACCENT, cap, 0, 0);
    lv_obj_set_width(c, LV_SIZE_CONTENT);
    lv_obj_set_style_text_letter_space(c, 2, 0);
    ui_text(head, F.body_b, C_TEXT, d->title ? d->title : t->title, UI_CARD_W - SP_2, 2);

    lv_obj_t *chat = kit_col(t->scroll, SP_1);
    lv_obj_set_width(chat, LV_PCT(100));
    for (int i = 0; i < d->n_seg; i++) {
        const detail_seg_t *s = &d->seg[i];
        const detail_seg_t *p = i > 0 ? &d->seg[i - 1] : NULL;
        const bool run = p && p->me == s->me && ((!p->s && !s->s) || (p->s && s->s && strcmp(p->s, s->s) == 0));
        bubble(chat, s, !run);
    }
    if (d->n_seg == 0) {
        ui_text(chat, F.body, C_TEXT_2, "No speech in this part.", UI_CARD_W, 2);
    }
    lv_obj_t *foot = kit_col(t->scroll, SP_2);
    lv_obj_set_width(foot, LV_PCT(100));
    lv_obj_set_style_margin_top(foot, SP_4, 0);
    if (d->next) {
        char nl[64];
        const double len = d->to - d->from > 0 ? d->to - d->from : TALKS_TRANSCRIPT_PAGE_S;
        char np[32];
        talks_part_label(d->to, d->to + len, np, sizeof(np));
        snprintf(nl, sizeof(nl), "Next part  \xc2\xb7  %s", np);
        lv_obj_t *b = kit_button(foot, KIT_SECONDARY, F.icon_s, ICON_CHEVRON_RIGHT, nl, next_clicked, t);
        /* the chevron after the words */
        lv_obj_move_to_index(lv_obj_get_child(b, 0), 1);
    } else {
        lv_obj_t *end = ui_text(foot, F.caption, C_TEXT_3, "End of the transcript", UI_CARD_W, 1);
        lv_obj_set_style_text_align(end, LV_TEXT_ALIGN_CENTER, 0);
    }
}

static bool tr_next(lv_obj_t *page, void *argp)
{
    (void)page;
    tr_arg_t *t = argp;
    if (!t->d || !t->d->next) {
        return false;
    }
    char ref[PDA_REF_MAX];
    snprintf(ref, sizeof(ref), "%s", t->d->next);
    tr_free_detail(t);
    snprintf(t->ref, sizeof(t->ref), "%s", ref);
    t->part++;
    t->d = app_load_detail(t->ref);
    if (!t->d && !app_pending_fetch(t->ref)) {
        char json[PDA_ACTION_BUF];
        const size_t n = pda_action_fetch(json, t->ref);
        if (!n || app_queue(json, n) != 0) {
            ui_toast(UI_TOAST_ERROR, "Outbox full: sync first");
        }
    }
    tr_rebuild(t);
    lv_obj_scroll_to_y(t->scroll, 0, LV_ANIM_OFF);
    return true;
}

static void next_async(void *argp)
{
    if (argp == s_tr_live) { /* still open */
        tr_next(NULL, argp);
    }
}

/* after the tap: the next page replaces the button that was tapped */
static void next_clicked(lv_event_t *e)
{
    lv_async_call(next_async, lv_event_get_user_data(e));
}

static void tr_build(lv_obj_t *page, void *argp)
{
    tr_arg_t *t = argp;
    s_tr_live = t;
    t->scroll = ui_page_scroller(page);
    tr_rebuild(t);
}

static void tr_update(lv_obj_t *page, void *argp, unsigned what)
{
    (void)page;
    tr_arg_t *t = argp;
    if (!t->d && (what & (UI_DIRTY_DETAIL | UI_DIRTY_OUTBOX | UI_DIRTY_FEED))) {
        tr_rebuild(t); /* arrived, or the fetch was listed / dropped */
    }
}

static lv_obj_t *tr_scroller(lv_obj_t *page)
{
    return lv_obj_get_child(page, 0);
}

static const ui_detail_t TRANSCRIPT = {
    .build = tr_build,
    .update = tr_update,
    .destroy = tr_destroy,
    .scroller = tr_scroller,
    .next = tr_next,
};

void talk_transcript_open(const char *ref, const char *title)
{
    tr_arg_t *t = calloc(1, sizeof(*t));
    if (!t) {
        return;
    }
    snprintf(t->ref, sizeof(t->ref), "%s", ref);
    snprintf(t->title, sizeof(t->title), "%s", title ? title : "");
    t->d = app_load_detail(ref);
    if (!t->d && !app_pending_fetch(ref)) {
        char json[PDA_ACTION_BUF];
        const size_t n = pda_action_fetch(json, ref);
        const int rc = n ? app_queue(json, n) : -2;
        if (rc != 0) {
            ui_toast(UI_TOAST_ERROR, rc == -1 ? "Outbox full: sync first" : "Couldn't ask for the transcript");
        }
    }
    ui_push("Transcript", &TRANSCRIPT, t);
}
