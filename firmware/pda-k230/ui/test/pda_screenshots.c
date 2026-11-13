/* Renders every screen and state built so far from the fictional fixtures to PNG, through
 * the real UI and the headless backend, with LVGL's clock driven by hand (the T5 README's
 * example clock: 2026-10-20T07:05:00Z, display offset +120 min). Taps and drags go through
 * LVGL's input like a finger. Output: $OUT_DIR (default ./screens). Also a smoke test: it
 * fails if a state that should be on screen isn't. */
#include <errno.h>
#include <sys/stat.h>

#include "app_state.h"
#include "check.h"
#include "lvgl.h"
#include "platform.h"
#include "test_util.h"
#include "theme.h"
#include "ui.h"

static const char *out_dir;
static int64_t mono = 5000000;
static int shots;

static int fake_send_local(const char *json, size_t len)
{
    printf("  LOCAL to syncd: %.*s\n", (int)len, json);
    return 0;
}

/* LVGL's clock and app_mono_ms() move together */
static void step(uint32_t ms)
{
    for (uint32_t t = 0; t < ms; t += 5) {
        mono += 5;
        app_set_mono_override(mono);
        headless_advance(5);
    }
}

static void shot(const char *name)
{
    char path[512];
    snprintf(path, sizeof(path), "%s/%s.png", out_dir, name);
    CHECK_EQ(headless_png(path), 0);
    printf("  %s\n", path);
    shots++;
}

static void put_feed(const char *name)
{
    size_t len;
    char *json = read_fixture(name, &len);
    CHECK_EQ(app_commit_blob(1, 1, (const uint8_t *)json, len), 0);
    free(json);
    step(300);
}

/* the first label under root whose text starts with s (anywhere: holds s), depth first,
 * visible only */
static lv_obj_t *find_label_ex(lv_obj_t *root, const char *s, bool anywhere)
{
    if (lv_obj_has_flag(root, LV_OBJ_FLAG_HIDDEN)) {
        return NULL;
    }
    if (lv_obj_check_type(root, &lv_label_class)) {
        const char *t = lv_label_get_text(root);
        if (t && (anywhere ? strstr(t, s) != NULL : strncmp(t, s, strlen(s)) == 0)) {
            return root;
        }
    }
    for (uint32_t i = 0; i < lv_obj_get_child_count(root); i++) {
        lv_obj_t *r = find_label_ex(lv_obj_get_child(root, (int32_t)i), s, anywhere);
        if (r) {
            return r;
        }
    }
    return NULL;
}

static lv_obj_t *find_label(lv_obj_t *root, const char *s)
{
    return find_label_ex(root, s, true);
}

static bool center_of(const char *text, int32_t *x, int32_t *y)
{
    lv_obj_t *l = find_label_ex(lv_screen_active(), text, false);
    CHECK(l != NULL);
    if (!l) {
        fprintf(stderr, "no label \"%s\" on screen\n", text);
        return false;
    }
    lv_area_t a;
    lv_obj_get_coords(l, &a);
    *x = (a.x1 + a.x2) / 2;
    *y = (a.y1 + a.y2) / 2;
    return true;
}

static void tap_text(const char *text)
{
    int32_t x, y;
    if (center_of(text, &x, &y)) {
        headless_tap(x, y);
    }
}

static void vision(const char *line)
{
    vision_event_t e;
    CHECK(vision_parse_line(line, strlen(line), &e));
    ui_vision_event(&e);
}

static void hand_at(const char *text, double dx, const char *shape)
{
    int32_t x, y;
    if (!center_of(text, &x, &y)) {
        return;
    }
    char line[160];
    snprintf(line, sizeof(line), "{\"t\":\"hand\",\"x\":%.4f,\"y\":%.4f,\"shape\":\"%s\"}",
             (double)x / UI_W + dx, (double)y / UI_H, shape);
    vision(line);
}

static void set_net(app_net_state_t s, int64_t last_ok)
{
    app_net_t n = {.state = s, .last_ok = last_ok};
    snprintf(n.hub, sizeof(n.hub), "hub.example.ts.net");
    app_set_net(&n);
}

#include "shots_brief_calendar.h" /* 30..49: Brief, Calendar, the new event */
#include "pda_screenshots_s2.h" /* screens 50..89: Talks, Status, pairing, ambient */

int main(void)
{
    out_dir = getenv("OUT_DIR");
    if (!out_dir) {
        out_dir = "screens";
    }
    mkdir(out_dir, 0755);
    const char *dir = temp_dir();
    char state[64];
    snprintf(state, sizeof(state), "%s/s", dir);
    CHECK_EQ(app_init(state, 7), 0);
    app_set_clock_override(DEMO_NOW);
    app_set_mono_override(mono);
    app_set_time(DEMO_NOW * 1000, DEMO_TZ);
    app_set_power(76, false, false);
    app_set_link(true);
    set_net(APP_NET_OK, DEMO_NOW - 300);

    lv_init();
    lv_display_t *disp = headless_create(UI_W, UI_H);
    headless_manual_time(true);
    theme_init(disp);
    const ui_hooks_t hooks = {.send_local = fake_send_local};
    ui_set_hooks(&hooks);
    ui_init(disp);
    s2_setup();
    step(100);

    /* ---- no feed yet ---- */
    shot("01_today_no_feed");

    /* ---- Today with the hub's insight ---- */
    put_feed("feed_insight_demo.json");
    CHECK(find_label(lv_screen_active(), "INSIGHT") != NULL);
    CHECK(find_label(lv_screen_active(), "DO FIRST") != NULL);
    shot("02_today_insight");
    lv_obj_t *scroller = lv_obj_get_child(ui_tab_page(UI_TAB_TODAY), 1);
    lv_obj_scroll_to_y(scroller, LV_COORD_MAX, LV_ANIM_OFF);
    step(50);
    shot("03_today_insight_end");
    lv_obj_scroll_to_y(scroller, 0, LV_ANIM_OFF);
    step(50);

    /* ---- a tick: the real tap, mid-animation and settled ---- */
    tap_text("Send the Q4 budget sheet");
    CHECK_EQ(app_outbox_count(), 1);
    step(20); /* the release already ran ~60 ms of the 220 ms pop: catch it midway */
    shot("04_today_tick_anim");
    step(400);
    shot("05_today_ticked");

    /* ---- an event's sheet ---- */
    tap_text("Lunch with Maria");
    step(400);
    CHECK(ui_sheet_is_open());
    shot("06_event_sheet");
    ui_sheet_close();
    step(400);

    /* ---- pull down to sync, before letting go ---- */
    headless_drag(284, 300, 284, 640, 300, false);
    shot("07_today_pull_to_sync");
    headless_touch(284, 640, false);
    step(900);

    /* ---- Tasks: the pending tick from Today shows; then syncd's ack refuses it ---- */
    ui_show_tab(UI_TAB_TASKS, true);
    step(110);
    shot("08_tab_transition");
    step(300);
    shot("09_tasks");
    {
        /* syncd lists the toggle and the hub refuses it for good: "Couldn't sync" + toast */
        pda_ob_entry_t e[2];
        uint32_t rem;
        bool was;
        const pda_store_t *s = app_store();
        CHECK_EQ(s->ob_list(NULL, 0, e, 2, &rem), 1);
        CHECK_EQ(s->ob_ack(NULL, e[0].action_id, 1, &was), 0);
    }
    step(300);
    CHECK(find_label(lv_screen_active(), "Couldn't sync") != NULL);
    shot("10_tasks_couldnt_sync_toast");
    lv_obj_t *tsc = lv_obj_get_child(ui_tab_page(UI_TAB_TASKS), 0);
    lv_obj_scroll_to_y(tsc, LV_COORD_MAX, LV_ANIM_OFF);
    step(3000); /* the toast goes */
    shot("11_tasks_end");

    /* ---- the header's states: syncing, changes waiting, the camera on, battery low ---- */
    ui_show_tab(UI_TAB_TODAY, false);
    lv_obj_scroll_to_y(scroller, 0, LV_ANIM_OFF);
    step(300);
    tap_text("Rehearse the demo");
    set_net(APP_NET_SYNCING, DEMO_NOW - 300);
    app_vision_t v = {.connected = true, .camera = APP_CAMERA_ON, .fps = 15};
    app_set_vision(&v);
    app_set_power(12, false, false);
    step(200);
    shot("12_header_syncing_camera_pending");

    /* ---- hands-free: the focus ring and the hand cursor, then a thumbs-up hold ---- */
    app_settings_t set = *app_settings();
    set.hands_free = true;
    app_set_settings(&set);
    set_net(APP_NET_OK, DEMO_NOW - 2 * 3600); /* "synced 2 h ago" */
    app_set_power(76, true, true);
    step(100);
    hand_at("Send the Q4 budget", 0.08, "point");
    step(100);
    vision("{\"t\":\"gesture\",\"g\":\"point\",\"conf\":0.9}");
    step(400);
    shot("13_handsfree_focus_cursor");
    hand_at("Send the Q4 budget", 0.08, "thumbs_up");
    step(600);
    shot("14_handsfree_thumbs_up_hold");
    step(500); /* the hold commits: the focused to-do ticks */
    step(300);
    CHECK_EQ(app_outbox_count(), 2);
    vision("{\"t\":\"hand\",\"shape\":\"none\"}");
    step(300);
    shot("15_handsfree_after_hold");

    /* ---- hands-free swipe: the next tab (a placeholder) ---- */
    vision("{\"t\":\"gesture\",\"g\":\"swipe_left\",\"conf\":0.9}");
    step(400);
    CHECK_EQ(ui_current_tab(), UI_TAB_BRIEF);
    shot("16_brief_placeholder");
    set.hands_free = false;
    app_set_settings(&set);
    ui_show_tab(UI_TAB_STATUS, false);
    step(100);
    shot("17_status_placeholder");

    /* ---- the rules (no insight for today) ---- */
    put_feed("feed_demo.json");
    ui_show_tab(UI_TAB_TODAY, false);
    lv_obj_scroll_to_y(scroller, 0, LV_ANIM_OFF);
    step(300);
    shot("18_today_rules");
    lv_obj_scroll_to_y(scroller, LV_COORD_MAX, LV_ANIM_OFF);
    step(50);
    shot("19_today_rules_end");
    lv_obj_scroll_to_y(scroller, 0, LV_ANIM_OFF);

    /* ---- nothing at all: "All caught up!" ---- */
    static const char empty[] =
        "{\"v\":1,\"version\":1792480000,\"generated\":\"2026-10-20T07:00:00Z\",\"tz\":\"Europe/Berlin\","
        "\"conversations\":[],\"actions\":[],\"calendar\":[],"
        "\"todos\":[{\"id\":\"note_demo_done\",\"title\":\"House\",\"pinned\":true,\"due\":null,"
        "\"items\":[{\"i\":0,\"text\":\"Water the plants\",\"done\":true}]}]}";
    CHECK_EQ(app_commit_blob(1, 2, (const uint8_t *)empty, sizeof(empty) - 1), 0);
    ui_show_tab(UI_TAB_TODAY, false);
    step(300);
    CHECK(find_label(lv_screen_active(), "All caught up!") != NULL);
    shot("20_today_empty");

    brief_calendar_shots();
    /* the next scenes start from the empty feed of shot 20 again */
    CHECK_EQ(app_commit_blob(1, 3, (const uint8_t *)empty, sizeof(empty) - 1), 0);
    step(300);
    screens_s2();

    printf("%d screenshots in %s\n", shots, out_dir);
    return checks_done("pda_screenshots");
}
