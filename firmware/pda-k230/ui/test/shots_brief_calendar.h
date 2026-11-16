/* pda_screenshots.c, part: Brief, Calendar and the new-event flow (shots 30..49). Included
 * by pda_screenshots.c after its helpers (step, shot, tap_text, find_label, hand_at,
 * vision); runs after its own shots, with the empty feed in. */
#pragma once

#include "src/widgets/buttonmatrix/lv_buttonmatrix_private.h"

static lv_obj_t *keyboard(void)
{
    lv_obj_t *top = lv_layer_top();
    for (uint32_t i = 0; i < lv_obj_get_child_count(top); i++) {
        lv_obj_t *c = lv_obj_get_child(top, (int32_t)i);
        if (lv_obj_check_type(c, &lv_keyboard_class) && !lv_obj_has_flag(c, LV_OBJ_FLAG_HIDDEN)) {
            return c;
        }
    }
    return NULL;
}

/* the middle of the key labelled k */
static bool key_xy(const char *k, int32_t *x, int32_t *y)
{
    lv_obj_t *kb = keyboard();
    CHECK(kb != NULL);
    if (!kb) {
        return false;
    }
    const lv_buttonmatrix_t *m = (const lv_buttonmatrix_t *)kb;
    for (uint32_t i = 0; i < m->btn_cnt; i++) {
        const char *t = lv_buttonmatrix_get_button_text(kb, i);
        if (t && strcmp(t, k) == 0) {
            const lv_area_t a = m->button_areas[i];
            *x = kb->coords.x1 + (a.x1 + a.x2) / 2;
            *y = kb->coords.y1 + (a.y1 + a.y2) / 2;
            return true;
        }
    }
    fprintf(stderr, "no key \"%s\"\n", k);
    CHECK(0);
    return false;
}

/* tap the key labelled k, like a finger */
static void tap_key(const char *k)
{
    int32_t x, y;
    if (key_xy(k, &x, &y)) {
        headless_tap(x, y);
        step(30);
    }
}

/* type s on the keyboard: capitals with shift (the first one comes by itself), other
 * characters from 123 */
static void type_text(lv_obj_t *field, const char *s)
{
    for (const char *p = s; *p; p++) {
        const char k[2] = {*p, 0};
        if (*p == ' ') {
            tap_key("space");
        } else if (*p >= 'A' && *p <= 'Z') {
            if (lv_textarea_get_text(field)[0]) {
                tap_key("\xe2\x86\x91");
            }
            tap_key(k);
        } else if ((*p >= 'a' && *p <= 'z') || *p == ',' || *p == '.') {
            tap_key(k);
        } else {
            tap_key("123");
            tap_key(k);
            tap_key("abc");
        }
    }
}

static lv_obj_t *find_textarea(lv_obj_t *root)
{
    if (lv_obj_check_type(root, &lv_textarea_class)) {
        return root;
    }
    for (uint32_t i = 0; i < lv_obj_get_child_count(root); i++) {
        lv_obj_t *r = find_textarea(lv_obj_get_child(root, (int32_t)i));
        if (r) {
            return r;
        }
    }
    return NULL;
}

/* the insight feed with the relay's digest text: the headline, a blank line, the summary */
static void put_feed_with_headline(uint32_t blob)
{
    size_t len;
    char *json = read_fixture("feed_insight_demo.json", &len);
    static const char from[] = "\"text\":\"A quiet Monday. ";
    char *at = strstr(json, from);
    CHECK(at != NULL);
    if (!at) {
        free(json);
        return;
    }
    char *out = malloc(len + 8);
    const size_t head = (size_t)(at - json) + sizeof(from) - 2; /* up to the space */
    memcpy(out, json, head);
    size_t w = head;
    memcpy(out + w, "\\n\\n", 4);
    w += 4;
    memcpy(out + w, json + head + 1, len - head - 1);
    w += len - head - 1;
    CHECK_EQ(app_commit_blob(1, blob, (const uint8_t *)out, w), 0);
    free(out);
    free(json);
    step(300);
}

/* the page in front: the newest detail layer or the sheet's scrim */
static lv_obj_t *front(void)
{
    lv_obj_t *scr = lv_screen_active();
    for (int32_t i = (int32_t)lv_obj_get_child_count(scr) - 1; i >= 0; i--) {
        lv_obj_t *c = lv_obj_get_child(scr, i);
        if (!lv_obj_has_flag(c, LV_OBJ_FLAG_HIDDEN)) {
            return c;
        }
    }
    return scr;
}

/* tap the label holding text on the page in front */
static void tap_front(const char *text)
{
    lv_obj_t *l = find_label_ex(front(), text, true);
    CHECK(l != NULL);
    if (!l) {
        fprintf(stderr, "no label \"%s\" in front\n", text);
        return;
    }
    lv_area_t a;
    lv_obj_get_coords(l, &a);
    headless_tap((a.x1 + a.x2) / 2, (a.y1 + a.y2) / 2);
}

static void tap_obj(lv_obj_t *o)
{
    lv_area_t a;
    lv_obj_get_coords(o, &a);
    headless_tap((a.x1 + a.x2) / 2, (a.y1 + a.y2) / 2);
}

static void brief_calendar_shots(void)
{
    /* ---- the empty feed: no digest, no events ---- */
    ui_show_tab(UI_TAB_BRIEF, false);
    step(300);
    CHECK(find_label(lv_screen_active(), "No digest yet") != NULL);
    shot("30_brief_empty");
    ui_show_tab(UI_TAB_CALENDAR, false);
    step(300);
    CHECK(find_label(lv_screen_active(), "Nothing planned") != NULL);
    shot("31_calendar_empty");

    /* ---- Brief ---- */
    put_feed_with_headline(3);
    ui_show_tab(UI_TAB_BRIEF, false);
    step(300);
    CHECK(find_label(lv_screen_active(), "YESTERDAY'S DIGEST") != NULL);
    shot("32_brief");
    lv_obj_t *bsc = lv_obj_get_child(ui_tab_page(UI_TAB_BRIEF), 0);
    lv_obj_scroll_to_y(bsc, LV_COORD_MAX, LV_ANIM_OFF);
    step(50);
    shot("33_brief_end");
    const int before = app_outbox_count();
    tap_text("Refresh digest");
    step(300);
    CHECK_EQ(app_outbox_count(), before + 1);
    CHECK(find_label(lv_screen_active(), "Refresh requested") != NULL);
    shot("34_brief_refresh_requested");
    step(3000);

    /* ---- Calendar ---- */
    ui_show_tab(UI_TAB_CALENDAR, false);
    step(300);
    CHECK(find_label(lv_screen_active(), "TODAY \xc2\xb7 TUE 20 OCT") != NULL);
    CHECK(find_label(lv_screen_active(), "Call Tom") == NULL); /* over at 09:05 */
    shot("35_calendar");
    lv_obj_t *csc = lv_obj_get_child(ui_tab_page(UI_TAB_CALENDAR), 0);
    lv_obj_scroll_to_y(csc, LV_COORD_MAX, LV_ANIM_OFF);
    step(50);
    shot("36_calendar_end");
    lv_obj_scroll_to_y(csc, 0, LV_ANIM_OFF);
    step(50);
    tap_text("Lunch with Maria");
    step(400);
    CHECK(ui_sheet_is_open());
    CHECK(find_label(lv_screen_active(), "Open the calendar") == NULL);
    shot("37_calendar_event_sheet");
    ui_sheet_close();
    step(400);

    /* ---- a new event: When ---- */
    tap_obj(lv_obj_get_child(ui_tab_page(UI_TAB_CALENDAR), 1)); /* the + */
    step(400);
    CHECK(ui_in_detail());
    CHECK(find_label(lv_screen_active(), "When?") != NULL);
    shot("38_new_event_when");
    tap_front("Thu"); /* Thu 22 Oct */
    tap_front("14:00");
    tap_front("+15 min");
    tap_front("1 hour");
    step(300);
    CHECK(find_label(lv_screen_active(), "14:15") != NULL);
    shot("39_new_event_when_picked");
    tap_front("All day");
    step(300);
    shot("40_new_event_all_day");
    tap_front("16:00"); /* a time again: 30 min */
    step(200);

    /* ---- What ---- */
    tap_front("Next: the title");
    step(400);
    CHECK(find_label(lv_screen_active(), "What?") != NULL);
    CHECK(find_label(front(), "Thu 22 Oct") != NULL && find_label(front(), "16:00 \xe2\x80\x93 16:30") != NULL);
    tap_front("Add to calendar"); /* no title yet: it says so and opens the keyboard */
    step(400);
    CHECK(find_label(lv_screen_active(), "Give it a title") != NULL);
    CHECK(keyboard() != NULL);
    shot("41_new_event_what_no_title");
    lv_obj_t *field = find_textarea(lv_screen_active());
    CHECK(field != NULL);
    if (!field) {
        return;
    }
    type_text(field, "Dinner with Maria & Tom");
    step(300);
    CHECK(strcmp(lv_textarea_get_text(field), "Dinner with Maria & Tom") == 0);
    shot("42_new_event_keyboard");
    {
        /* a key held down shows its pop-over; letting go types it */
        int32_t x, y;
        if (key_xy("s", &x, &y)) {
            headless_touch(x, y, true);
            step(100);
            shot("49_new_event_key_pressed");
            headless_touch(x, y, false);
            step(50);
            tap_key("\xe2\x86\x90"); /* and away again */
        }
        CHECK(strcmp(lv_textarea_get_text(field), "Dinner with Maria & Tom") == 0);
    }
    tap_key("123");
    step(100);
    shot("43_new_event_keyboard_123");
    tap_key("Done");
    step(400);
    CHECK(keyboard() == NULL);
    tap_front("Focus time"); /* a quick title replaces it */
    step(300);
    CHECK(strcmp(lv_textarea_get_text(field), "Focus time") == 0);
    shot("44_new_event_what_template");
    lv_textarea_set_text(field, "Dinner with Maria & Tom");
    const int n0 = app_outbox_count();
    tap_front("Add to calendar");
    step(150);
    CHECK_EQ(app_outbox_count(), n0 + 1);
    CHECK_EQ(ui_current_tab(), UI_TAB_CALENDAR);
    step(300);
    CHECK(find_label(lv_screen_active(), "Waiting to sync") != NULL);
    shot("45_calendar_pending_added");
    step(3000); /* the flash and the toast go */
    shot("46_calendar_pending");

    /* the pending event's sheet: it can be taken back */
    tap_text("Dinner with Maria");
    step(400);
    CHECK(find_label(lv_screen_active(), "Don't add it") != NULL);
    shot("47_calendar_pending_sheet");
    ui_sheet_close();
    step(400);

    /* hands-free: the focus ring on a calendar row */
    lv_obj_scroll_to_y(csc, 0, LV_ANIM_OFF);
    step(50);
    app_settings_t set = *app_settings();
    set.hands_free = true;
    app_set_settings(&set);
    step(100);
    hand_at("Lunch with Maria", 0.1, "point");
    step(100);
    vision("{\"t\":\"gesture\",\"g\":\"point\",\"conf\":0.9}");
    step(400);
    shot("48_calendar_handsfree_focus");
    vision("{\"t\":\"hand\",\"shape\":\"none\"}");
    set.hands_free = false;
    app_set_settings(&set);
    step(300);
}
