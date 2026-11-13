/* Today (T5 plan §5 "Today as built", on cards): INSIGHT (the hub's headline, or "All
 * caught up!" from the rules), DO FIRST, COMING UP, ALSO WORTH A LOOK; to-dos tick in place,
 * events open a sheet; the big "All caught up!" when nothing is due; "Digest ready on
 * Brief"; pull down to sync. The plan itself is today_plan.c's; the words today_text.c's. */
#include <stdio.h>
#include <string.h>

#include "app_state.h"
#include "pda_time.h"
#include "screens.h"
#include "theme.h"
#include "today_plan.h"
#include "today_text.h"
#include "widgets.h"

#define MAX_TODO_ROWS (TODAY_SECTIONS * TODAY_PLAN_MAX)
#define PULL_ARM 90 /* px pulled past the top (elastic: about a third of the finger's way) arm a sync */

static const char *const LABELS[TODAY_SECTIONS] = {"DO FIRST", "COMING UP", "ALSO WORTH A LOOK"};

static struct {
    lv_obj_t *page, *scroll, *pull, *pull_icon, *pull_text;
    todo_row_t *todos[MAX_TODO_ROWS];
    int n_todos;
    today_plan_t plan;    /* what is on screen */
    bool have_plan;
    const feed_t *feed;   /* the feed it was built from */
    bool pull_armed;
    bool pull_fired; /* this pull has synced: not again until the page is back at the top */
} T;

/* ---- events ---- */

typedef struct {
    int event;
    const char *why;
} event_tap_t;

static void free_ud(lv_event_t *e)
{
    lv_free(lv_event_get_user_data(e));
}

static void event_clicked(lv_event_t *e)
{
    const event_tap_t *t = lv_event_get_user_data(e);
    sheet_event_open(t->event, t->why);
}

static void event_row(lv_obj_t *card, const today_item_t *it)
{
    const feed_t *f = app_feed();
    const feed_event_t *e = &f->events[it->ref.event];
    event_tap_t *tap = lv_malloc_zeroed(sizeof(*tap));
    tap->event = it->ref.event;
    tap->why = it->ref.why;
    ui_row_t *r = ui_row(card, event_clicked, tap);
    lv_obj_add_event_cb(r->row, free_ud, LV_EVENT_DELETE, tap);
    ui_focusable(r->row, false);

    char hm[16], wd[24], why[TODAY_LINE_MAX];
    today_event_column(e, app_today(), app_tz_min(), hm, sizeof(hm), wd, sizeof(wd));
    lv_obj_t *t = ui_text(r->left, e->all_day ? F.caption : F.small_b, C_TEXT, hm, UI_LEFT_COL, 0);
    lv_obj_set_style_pad_top(t, e->all_day ? 7 : 3, 0); /* on the title's first line */
    if (wd[0]) {
        ui_text(r->left, F.caption, C_ACCENT, wd, UI_LEFT_COL, 0);
    }
    const int32_t w = UI_CARD_W - 2 * UI_CARD_PAD - UI_LEFT_COL - SP_4 - 40;
    ui_text(r->mid, F.body_b, C_TEXT, e->title, w, 2);
    today_second_line(f, it, false, app_tz_min(), why, sizeof(why));
    if (why[0]) {
        ui_text(r->mid, F.small, C_TEXT_2, why, w, 2);
    }
    lv_obj_remove_flag(r->right, LV_OBJ_FLAG_HIDDEN);
    lv_obj_t *chev = lv_label_create(r->right);
    lv_label_set_text(chev, ICON_CHEVRON_RIGHT);
    lv_obj_set_style_text_font(chev, F.icon_s, 0);
    lv_obj_set_style_text_color(chev, C(C_TEXT_3), 0);
}

/* ---- the page ---- */

static void more_clicked(lv_event_t *e)
{
    ui_show_tab((ui_tab_t)(intptr_t)lv_event_get_user_data(e), true);
}

static void inset_rule(lv_obj_t *card)
{
    lv_obj_t *h = ui_hairline(card);
    lv_obj_set_style_margin_left(h, UI_CARD_PAD + UI_LEFT_COL + SP_4, 0);
    lv_obj_set_width(h, UI_CARD_W - UI_CARD_PAD - UI_LEFT_COL - SP_4);
}

static void insight_card(const today_plan_t *p, bool any)
{
    lv_obj_t *card = ui_card(T.scroll);
    lv_obj_set_style_bg_color(card, C(C_ACCENT_TINT), 0);
    lv_obj_set_style_border_color(card, lv_color_mix(C(C_ACCENT), C(C_BG), LV_OPA_30), 0);
    lv_obj_set_style_border_width(card, 1, 0);
    lv_obj_set_style_pad_bottom(card, UI_CARD_PAD, 0);
    ui_section_head(card, ICON_SPARKLES, "INSIGHT", C_ACCENT, NULL, NULL, NULL);
    const char *headline = p->from_insight ? p->headline : NULL;
    if (headline) {
        lv_obj_t *l = ui_text(card, F.lead, C_TEXT, headline, UI_CARD_INNER_W, 7);
        lv_obj_set_style_margin_left(l, UI_CARD_PAD, 0);
        lv_obj_set_style_text_line_space(l, 6, 0);
        lv_obj_set_style_max_height(l, 7 * lv_font_get_line_height(F.lead) + 6 * 6, 0);
    } else if (any) {
        /* no insight for today: the rules made the sections (T5: "All caught up!") */
        lv_obj_t *row = lv_obj_create(card);
        lv_obj_remove_style_all(row);
        lv_obj_remove_flag(row, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_size(row, LV_PCT(100), LV_SIZE_CONTENT);
        lv_obj_set_style_pad_hor(row, UI_CARD_PAD, 0);
        lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        lv_obj_set_style_pad_column(row, SP_3, 0);
        lv_obj_t *tick = ui_tick(row);
        lv_obj_set_size(tick, 32, 32);
        lv_obj_set_style_border_width(tick, 0, 0);
        ui_tick_set(tick, true, false);
        ui_text(row, F.body_b, C_TEXT, "All caught up!", UI_CARD_INNER_W - 48, 0);
    }
}

static void section_card(const today_plan_t *p, int s)
{
    const today_section_t *sec = &p->sec[s];
    const feed_t *f = app_feed();
    lv_obj_t *card = ui_card(T.scroll);
    char more[32] = "";
    ui_tab_t to = UI_TAB_TASKS;
    if (sec->more > 0) {
        snprintf(more, sizeof(more), "+%d more " ICON_CHEVRON_RIGHT, sec->more);
        to = 2 * sec->more_events >= sec->more ? UI_TAB_CALENDAR : UI_TAB_TASKS;
    }
    ui_section_head(card, NULL, LABELS[s], C_TEXT_2, more[0] ? more : NULL, more_clicked, (void *)(intptr_t)to);
    for (int i = 0; i < sec->n; i++) {
        const today_item_t *it = &sec->items[i];
        if (i > 0) {
            inset_rule(card);
        }
        if (it->ref.kind == FEED_REF_TODO) {
            char why[TODAY_LINE_MAX];
            today_second_line(f, it, false, app_tz_min(), why, sizeof(why));
            todo_row_t *t = todo_row(card, it->ref.note, it->ref.item, F.body_b, 2, why, UI_LEFT_COL);
            if (T.n_todos < MAX_TODO_ROWS) {
                T.todos[T.n_todos++] = t;
            }
        } else {
            event_row(card, it);
        }
    }
}

static void digest_clicked(lv_event_t *e)
{
    (void)e;
    ui_show_tab(UI_TAB_BRIEF, true);
}

static void digest_card(const feed_t *f)
{
    lv_obj_t *card = ui_card(T.scroll);
    ui_row_t *r = ui_row(card, digest_clicked, NULL);
    ui_focusable(r->row, false);
    lv_obj_t *i = lv_label_create(r->left);
    lv_label_set_text(i, ICON_NEWSPAPER);
    lv_obj_set_style_text_font(i, F.icon, 0);
    lv_obj_set_style_text_color(i, C(C_ACCENT), 0);
    const int32_t w = UI_CARD_W - 2 * UI_CARD_PAD - UI_LEFT_COL - SP_4 - 40;
    ui_text(r->mid, F.body_b, C_TEXT, "Digest ready on Brief", w, 0);
    if (f->brief_date) {
        int64_t t;
        bool date_only;
        char d[24] = "";
        if (pda_time_parse(f->brief_date, &t, &date_only)) {
            pda_fmt_day(pda_local_day(t, 0), d, sizeof(d));
        }
        char line[64];
        snprintf(line, sizeof(line), "The digest of %s", d[0] ? d : f->brief_date);
        ui_text(r->mid, F.small, C_TEXT_2, line, w, 1);
    }
    lv_obj_remove_flag(r->right, LV_OBJ_FLAG_HIDDEN);
    lv_obj_t *chev = lv_label_create(r->right);
    lv_label_set_text(chev, ICON_CHEVRON_RIGHT);
    lv_obj_set_style_text_font(chev, F.icon_s, 0);
    lv_obj_set_style_text_color(chev, C(C_TEXT_3), 0);
}

static void rebuild(void)
{
    const feed_t *f = app_feed();
    const int32_t y = lv_obj_get_scroll_y(T.scroll);
    lv_obj_clean(T.scroll);
    T.n_todos = 0;
    char date[24];
    pda_fmt_date(app_now(), app_tz_min(), date, sizeof(date));
    ui_page_title(T.scroll, "Today", app_time_valid() ? date : NULL);
    if (!f) {
        ui_no_feed(T.scroll);
        T.feed = NULL;
        T.have_plan = false;
        return;
    }
    const today_plan_t *p = &T.plan;
    bool any = false;
    for (int s = 0; s < TODAY_SECTIONS; s++) {
        any |= p->sec[s].n > 0;
    }
    if ((!p->from_insight && any) || (p->from_insight && p->headline)) {
        insight_card(p, any);
    }
    for (int s = 0; s < TODAY_SECTIONS; s++) {
        if (p->sec[s].n > 0) {
            section_card(p, s);
        }
    }
    if (!any) {
        ui_empty_state(T.scroll, ICON_CHECK, C_ACCENT, "All caught up!",
                       "Nothing due and nothing on the calendar for the next 7 days.");
    }
    if (f->has_briefing) {
        digest_card(f);
    }
    lv_obj_update_layout(T.scroll);
    lv_obj_scroll_to_y(T.scroll, y > 0 ? y : 0, LV_ANIM_OFF);
}

/* ---- pull down to sync ---- */

static void on_scroll(lv_event_t *e)
{
    const lv_event_code_t code = lv_event_get_code(e);
    const int32_t y = lv_obj_get_scroll_y(T.scroll);
    if (code == LV_EVENT_SCROLL) {
        if (y >= 0) {
            lv_obj_add_flag(T.pull, LV_OBJ_FLAG_HIDDEN);
            T.pull_fired = false;
            T.pull_armed = false;
            return;
        }
        if (T.pull_fired) {
            return;
        }
        lv_obj_remove_flag(T.pull, LV_OBJ_FLAG_HIDDEN);
        const int32_t pulled = -y;
        lv_obj_set_style_opa(T.pull, (lv_opa_t)(pulled >= PULL_ARM ? 255 : pulled * 255 / PULL_ARM), 0);
        lv_indev_t *in = lv_indev_active();
        const bool pressing = in && lv_indev_get_state(in) == LV_INDEV_STATE_PRESSED;
        if (pulled >= PULL_ARM) {
            T.pull_armed = true;
        } else if (pressing) {
            T.pull_armed = false; /* pulled back before letting go */
        }
        lv_label_set_text(T.pull_text, T.pull_armed ? "Release to sync" : "Pull to sync");
        lv_obj_set_style_transform_rotation(T.pull_icon, T.pull_armed ? 1800 : 0, 0);
        lv_obj_set_style_text_color(T.pull_icon, C(T.pull_armed ? C_ACCENT : C_TEXT_2), 0);
    } else if (code == LV_EVENT_SCROLL_END) {
        if (T.pull_armed && !T.pull_fired) {
            T.pull_armed = false;
            T.pull_fired = true;
            lv_label_set_text(T.pull_text, "Syncing\xe2\x80\xa6");
            ui_request_sync();
        }
        if (y >= 0) {
            lv_obj_add_flag(T.pull, LV_OBJ_FLAG_HIDDEN);
        }
    }
}

static void build(lv_obj_t *page)
{
    T.page = page;
    /* the pull indicator sits under the scroller: pulling the content down reveals it */
    T.pull = lv_obj_create(page);
    lv_obj_remove_style_all(T.pull);
    lv_obj_remove_flag(T.pull, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(T.pull, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_align(T.pull, LV_ALIGN_TOP_MID, 0, SP_6);
    lv_obj_set_flex_flow(T.pull, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(T.pull, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(T.pull, SP_3, 0);
    T.pull_icon = lv_label_create(T.pull);
    lv_label_set_text(T.pull_icon, ICON_ARROW_DOWN);
    lv_obj_set_style_text_font(T.pull_icon, F.icon_s, 0);
    lv_obj_set_style_transform_pivot_x(T.pull_icon, 12, 0);
    lv_obj_set_style_transform_pivot_y(T.pull_icon, 12, 0);
    T.pull_text = lv_label_create(T.pull);
    lv_obj_set_style_text_font(T.pull_text, F.small, 0);
    lv_obj_set_style_text_color(T.pull_text, C(C_TEXT_2), 0);
    lv_label_set_text(T.pull_text, "Pull to sync");
    lv_obj_add_flag(T.pull, LV_OBJ_FLAG_HIDDEN);

    T.scroll = ui_page_scroller(page);
    lv_obj_add_event_cb(T.scroll, on_scroll, LV_EVENT_SCROLL, NULL);
    lv_obj_add_event_cb(T.scroll, on_scroll, LV_EVENT_SCROLL_END, NULL);
}

static void update(lv_obj_t *page, unsigned what)
{
    (void)page;
    if (what & (UI_DIRTY_FEED | UI_DIRTY_CLOCK | UI_DIRTY_NET)) {
        const feed_t *f = app_feed();
        today_plan_t p;
        memset(&p, 0, sizeof(p));
        if (f) {
            const int64_t now = app_now();
            today_plan_for_today(f, app_today(), now, app_tz_min(), &p);
        }
        /* a new minute only rebuilds when the plan changed (an event ended, a new day) */
        const bool changed = (what & UI_DIRTY_FEED) || f != T.feed || !T.have_plan ||
                             memcmp(&p, &T.plan, sizeof(p)) != 0 || (!f && (what & UI_DIRTY_NET));
        if (changed) {
            T.plan = p;
            T.have_plan = f != NULL;
            T.feed = f;
            rebuild();
            return;
        }
    }
    if (what & UI_DIRTY_OUTBOX) {
        for (int i = 0; i < T.n_todos; i++) {
            todo_row_refresh(T.todos[i], true);
        }
    }
}

static lv_obj_t *scroller(lv_obj_t *page)
{
    (void)page;
    return T.scroll;
}

const ui_screen_t screen_today = {
    .label = "Today",
    .icon = ICON_SUN,
    .build = build,
    .update = update,
    .scroller = scroller,
};
