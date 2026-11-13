/* Screens 50..89 of pda_screenshots.c: Talks (list, empty, a conversation with its action
 * items in every state, the transcript waiting / arrived / next part), Status (unpaired,
 * offline, ok, auth failed, no syncd; display; hands-free with the gesture tester; device),
 * the Tailscale QR sheet, pairing (choose, scan, a wrong QR code, waiting, paired, typed with
 * the keyboard, a typo, refused), the unpair / launcher confirmations, the wipe hold, the
 * ambient screen (moved a minute later, timed out, off), the hold ring and the focus ring on
 * the new screens. Included by pda_screenshots.c after its helpers (step, shot, tap_text,
 * find_label, vision, hand_at, set_net). */
#ifndef PDA_SCREENSHOTS_S2_H
#define PDA_SCREENSHOTS_S2_H

#ifndef PDA_UI_FIXTURES
#define PDA_UI_FIXTURES "test/fixtures"
#endif

static void s2_on_press(lv_event_t *e)
{
    (void)e;
    app_touch_activity(); /* as main.c does: a touch keeps the screen awake */
}

/* After ui_init(): touches count as activity, like on the device. */
static void s2_setup(void)
{
    for (lv_indev_t *in = lv_indev_get_next(NULL); in; in = lv_indev_get_next(in)) {
        lv_indev_add_event_cb(in, s2_on_press, LV_EVENT_PRESSED, NULL);
    }
}

static char *s2_read(const char *name, size_t *len)
{
    const char *dir = getenv("UI_FIXTURES");
    char path[512];
    snprintf(path, sizeof(path), "%s/%s", dir ? dir : PDA_UI_FIXTURES, name);
    FILE *f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "can't open %s\n", path);
        exit(2);
    }
    fseek(f, 0, SEEK_END);
    const long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *buf = malloc((size_t)n + 1);
    if (!buf || fread(buf, 1, (size_t)n, f) != (size_t)n) {
        exit(2);
    }
    fclose(f);
    buf[n] = 0;
    *len = (size_t)n;
    return buf;
}

static void s2_commit(uint8_t kind, const char *name)
{
    size_t len;
    char *json = s2_read(name, &len);
    CHECK_EQ(app_commit_blob(kind, 9, (const uint8_t *)json, len), 0);
    free(json);
    step(300);
}

/* scroll whatever holds the label so it is in view (LVGL's own scroll-to-view) */
static void s2_show_text(const char *text)
{
    lv_obj_t *l = find_label(lv_screen_active(), text);
    CHECK(l != NULL);
    if (l) {
        lv_obj_scroll_to_view_recursive(l, LV_ANIM_OFF);
    }
    step(60);
}

static void s2_scroll_end(lv_obj_t *scroll)
{
    lv_obj_scroll_to_y(scroll, LV_COORD_MAX, LV_ANIM_OFF);
    step(60);
}

static void s2_long_press_text(const char *text, uint32_t ms)
{
    int32_t x, y;
    if (center_of(text, &x, &y)) {
        headless_touch(x, y, true);
        step(ms);
        headless_touch(x, y, false);
        step(60);
    }
}

static lv_obj_t *s2_find_type(lv_obj_t *root, const lv_obj_class_t *cls, int nth)
{
    static int seen;
    if (root == lv_screen_active() || root == lv_layer_top() || root == lv_layer_sys()) {
        seen = 0;
    }
    if (lv_obj_check_type(root, cls) && !lv_obj_has_flag(root, LV_OBJ_FLAG_HIDDEN)) {
        if (seen++ == nth) {
            return root;
        }
    }
    for (uint32_t i = 0; i < lv_obj_get_child_count(root); i++) {
        lv_obj_t *r = s2_find_type(lv_obj_get_child(root, (int32_t)i), cls, nth);
        if (r) {
            return r;
        }
    }
    return NULL;
}

static void s2_tap_obj(lv_obj_t *o)
{
    lv_area_t a;
    lv_obj_get_coords(o, &a);
    headless_tap((a.x1 + a.x2) / 2, (a.y1 + a.y2) / 2);
}

/* the first visible label whose text is exactly s */
static lv_obj_t *s2_find_exact(lv_obj_t *root, const char *s)
{
    if (lv_obj_has_flag(root, LV_OBJ_FLAG_HIDDEN)) {
        return NULL;
    }
    if (lv_obj_check_type(root, &lv_label_class) && strcmp(lv_label_get_text(root), s) == 0) {
        return root;
    }
    for (uint32_t i = 0; i < lv_obj_get_child_count(root); i++) {
        lv_obj_t *r = s2_find_exact(lv_obj_get_child(root, (int32_t)i), s);
        if (r) {
            return r;
        }
    }
    return NULL;
}

static void s2_show_exact(const char *s)
{
    lv_obj_t *l = s2_find_exact(lv_screen_active(), s);
    CHECK(l != NULL);
    if (l) {
        lv_obj_scroll_to_view_recursive(l, LV_ANIM_OFF);
    }
    step(60);
}

static void s2_tap_exact(const char *s)
{
    lv_obj_t *l = s2_find_exact(lv_screen_active(), s);
    CHECK(l != NULL);
    if (l) {
        s2_tap_obj(l);
    }
}

static void s2_status_net(app_net_state_t s, int64_t last_ok, const char *hub, const char *msg)
{
    app_net_t n = {.state = s, .last_ok = last_ok};
    snprintf(n.hub, sizeof(n.hub), "%s", hub ? hub : "");
    snprintf(n.msg, sizeof(n.msg), "%s", msg ? msg : "");
    app_set_net(&n);
}

static void s2_tailnet(app_tailnet_state_t s, const char *url, const char *name)
{
    app_tailnet_t t = {.state = s};
    snprintf(t.url, sizeof(t.url), "%s", url ? url : "");
    snprintf(t.name, sizeof(t.name), "%s", name ? name : "");
    app_set_tailnet(&t);
}

static void s2_settings(bool hands_free, bool presence, int dim, int off)
{
    app_settings_t s = *app_settings();
    s.hands_free = hands_free;
    s.presence_wake = presence;
    s.dim_after_s = dim;
    s.off_after_s = off;
    app_set_settings(&s);
}

static void screens_s2(void)
{
    /* the "All caught up" feed of shot 20 has no conversations */
    s2_settings(false, false, 120, 900);
    ui_show_tab(UI_TAB_TALKS, false);
    step(300);
    CHECK(find_label(lv_screen_active(), "No conversations") != NULL);
    shot("50_talks_empty");

    /* ---- Talks ---- */
    s2_commit(1, "feed_talks_demo.json");
    ui_show_tab(UI_TAB_TALKS, false);
    step(300);
    CHECK(find_label(lv_screen_active(), "25 min ago") != NULL);
    shot("51_talks");
    lv_obj_t *talks = lv_obj_get_child(ui_tab_page(UI_TAB_TALKS), 0);
    s2_scroll_end(talks);
    shot("52_talks_end");
    lv_obj_scroll_to_y(talks, 0, LV_ANIM_OFF);
    step(60);

    /* a conversation: the stand-up, five suggested action items */
    s2_show_text("Stand-up: API migration");
    tap_text("Stand-up: API migration");
    step(400);
    CHECK(ui_in_detail());
    shot("53_conv_detail");
    /* the items in every state: two queued and sent (one refused), one done, one dismissed */
    s2_show_text("Tap: done");
    tap_text("Book the big meeting room");
    tap_text("Ask Alice for the slides");
    step(300);
    {
        /* syncd lists everything waiting; the hub refuses "Ask Alice…" (item 21) */
        pda_ob_entry_t e[8];
        uint32_t rem;
        bool was;
        const pda_store_t *st = app_store();
        CHECK(st->ob_list(NULL, 0, e, 8, &rem) >= 2);
        const app_pending_t *pi = app_pending_item(21);
        CHECK(pi != NULL);
        if (pi) {
            CHECK_EQ(st->ob_ack(NULL, pi->id, 1, &was), 0);
        }
    }
    step(3000); /* the "Couldn't sync" toast goes */
    s2_show_text("Tap: done");
    tap_text("Review Ben\xe2\x80\x99s pull");
    s2_long_press_text("Send Ben the load test", 700);
    step(300);
    CHECK(find_label(lv_screen_active(), "Dismissed") != NULL);
    shot("54_conv_items_states");
    tap_text("Review Ben\xe2\x80\x99s pull"); /* a second tap: undone */
    step(250);
    shot("55_conv_item_undone");
    step(3000);

    /* the transcript: not cached -> fetch queued, waiting; then it arrives */
    s2_show_text("Read the transcript");
    tap_text("Read the transcript");
    step(400);
    CHECK(app_pending_fetch("transcript:c_demo_standup@0-600") != NULL);
    shot("56_transcript_waiting");
    s2_commit(2, "detail_transcript_standup_1.json");
    CHECK(find_label(lv_screen_active(), "TRANSCRIPT") != NULL);
    shot("57_transcript");
    {
        lv_obj_t *l = find_label(lv_screen_active(), "Next part");
        CHECK(l != NULL);
        if (l) {
            lv_obj_scroll_to_view_recursive(l, LV_ANIM_OFF);
        }
        step(60);
    }
    shot("58_transcript_next_part");
    tap_text("Next part");
    step(300);
    CHECK(app_pending_fetch("transcript:c_demo_standup@600-1200") != NULL);
    s2_commit(2, "detail_transcript_standup_2.json");
    CHECK(find_label(lv_screen_active(), "End of the transcript") != NULL);
    shot("59_transcript_part2");
    ui_pop();
    step(400);
    ui_pop();
    step(400);

    /* ---- Status ---- */
    s2_tailnet(APP_TAILNET_RUNNING, NULL, "athena-pda.tail1234.ts.net");
    app_vision_t vv = {.connected = true, .camera = APP_CAMERA_OFF};
    app_set_vision(&vv);
    s2_status_net(APP_NET_UNPAIRED, 0, NULL, NULL);
    ui_show_tab(UI_TAB_STATUS, false);
    step(300);
    shot("60_status_unpaired");
    s2_status_net(APP_NET_OK, DEMO_NOW - 300, "hub.example.ts.net", NULL);
    step(300);
    CHECK(find_label(lv_screen_active(), "Up to date") != NULL);
    shot("61_status_ok");
    s2_status_net(APP_NET_OFFLINE, DEMO_NOW - 2 * 3600, "hub.example.ts.net", "hub.example.ts.net isn't reachable");
    step(300);
    shot("62_status_offline");
    s2_status_net(APP_NET_AUTH_FAILED, DEMO_NOW - 26 * 3600, "hub.example.ts.net", NULL);
    step(300);
    shot("63_status_auth_failed");
    app_set_link(false);
    step(300);
    shot("64_status_no_syncd");
    app_set_link(true);
    s2_status_net(APP_NET_OK, DEMO_NOW - 300, "hub.example.ts.net", NULL);
    step(300);
    s2_show_text("DISPLAY");
    s2_show_text("Then turn the screen off");
    shot("65_status_display");

    /* hands-free on, the camera on; practice mode; a gesture and a hand in the tester */
    s2_settings(true, true, 120, 900);
    vv.camera = APP_CAMERA_ON;
    vv.fps = 15;
    app_set_vision(&vv);
    step(300);
    s2_show_text("Practice here");
    tap_text("Practice here");
    step(200);
    vision("{\"t\":\"gesture\",\"g\":\"swipe_left\",\"conf\":0.93}");
    vision("{\"t\":\"hand\",\"x\":0.62,\"y\":0.35,\"shape\":\"open\"}");
    step(300);
    CHECK_EQ(ui_current_tab(), UI_TAB_STATUS); /* practice: the swipe didn't change the tab */
    CHECK(find_label(lv_screen_active(), "Swipe left") != NULL);
    s2_show_text("HANDS-FREE");
    shot("66_status_handsfree_tester");
    vision("{\"t\":\"hand\",\"shape\":\"none\"}");
    tap_text("Practice here");
    step(200);
    s2_scroll_end(lv_obj_get_child(ui_tab_page(UI_TAB_STATUS), 0));
    shot("67_status_device");

    /* the hands-free focus ring on Status: point at "Sync now" */
    lv_obj_scroll_to_y(lv_obj_get_child(ui_tab_page(UI_TAB_STATUS), 0), 0, LV_ANIM_OFF);
    step(100);
    hand_at("Sync now", 0.0, "point");
    step(100);
    vision("{\"t\":\"gesture\",\"g\":\"point\",\"conf\":0.9}");
    step(400);
    shot("68_status_focus_ring");
    vision("{\"t\":\"hand\",\"shape\":\"none\"}");
    s2_settings(false, false, 120, 900);
    vv.camera = APP_CAMERA_OFF;
    app_set_vision(&vv);
    step(300);

    /* Tailscale needs a login: the button, then the QR sheet */
    s2_tailnet(APP_TAILNET_NEEDS_LOGIN, "https://login.tailscale.com/a/f1c710a1demo", NULL);
    step(300);
    s2_show_exact("Show the login code");
    shot("69_status_tailscale_login");
    tap_text("Show the login code");
    step(400);
    CHECK(ui_sheet_is_open());
    shot("70_tailscale_qr_sheet");
    s2_tailnet(APP_TAILNET_RUNNING, NULL, "athena-pda.tail1234.ts.net");
    step(400);
    CHECK(!ui_sheet_is_open()); /* closes itself once Tailscale is up */
    step(3000);                 /* its toast goes */

    /* ---- pairing: choose, scan, a wrong QR code, waiting, paired ---- */
    s2_status_net(APP_NET_UNPAIRED, 0, NULL, NULL);
    step(300);
    lv_obj_scroll_to_y(lv_obj_get_child(ui_tab_page(UI_TAB_STATUS), 0), 0, LV_ANIM_OFF);
    step(60);
    s2_show_exact("Pair with Odysseus");
    s2_tap_exact("Pair with Odysseus");
    step(400);
    CHECK(ui_in_detail());
    shot("71_pairing_choose");
    tap_text("Scan the QR code");
    step(300);
    bool g, p, q;
    app_vision_mode(&g, &p, &q);
    CHECK(q); /* vision's qr mode is on */
    vv.camera = APP_CAMERA_ON;
    app_set_vision(&vv);
    step(900);
    shot("72_pairing_scan");
    vision("{\"t\":\"qr\",\"text\":\"https://example.org/menu\"}");
    step(300);
    CHECK(find_label(lv_screen_active(), "isn't an Odysseus pairing code") != NULL);
    shot("73_pairing_scan_wrong_qr");
    vision("{\"t\":\"qr\",\"text\":\"odysseus-pair:v1?url=https%3A%2F%2Fhub.example.ts.net&code=Fict1onalPairCode_k230\"}");
    step(300);
    app_vision_mode(&g, &p, &q);
    CHECK(!q); /* the camera goes off once the code is read */
    vv.camera = APP_CAMERA_OFF;
    app_set_vision(&vv);
    step(300);
    shot("74_pairing_waiting");
    app_set_pair_result(true, "Paired with hub.example.ts.net.");
    step(400);
    CHECK(find_label(lv_screen_active(), "Paired") != NULL);
    shot("75_pairing_paired");
    s2_tap_exact("Done");
    step(400);
    CHECK(!ui_in_detail());

    /* typed: the keyboard, a typo, then syncd refuses the code */
    s2_status_net(APP_NET_UNPAIRED, 0, NULL, NULL);
    step(300);
    s2_show_exact("Pair with Odysseus");
    s2_tap_exact("Pair with Odysseus");
    step(400);
    tap_text("Type the code");
    step(400);
    lv_obj_t *addr = s2_find_type(lv_screen_active(), &lv_textarea_class, 0);
    lv_obj_t *code = s2_find_type(lv_screen_active(), &lv_textarea_class, 1);
    CHECK(addr && code);
    if (addr && code) {
        lv_textarea_set_text(addr, "https://hub.example.ts.net");
        lv_textarea_set_text(code, "Fict1on");
    }
    step(300);
    shot("76_pairing_type_keyboard");
    s2_tap_exact("Pair");
    step(300);
    CHECK(find_label(lv_screen_active(), "8 to 100") != NULL);
    shot("77_pairing_type_typo");
    code = s2_find_type(lv_screen_active(), &lv_textarea_class, 1);
    if (code) {
        lv_textarea_set_text(code, "Fict1onalPairCode_k230");
    }
    s2_tap_exact("Pair");
    step(300);
    app_set_pair_result(false, "That pairing code was already used. In Odysseus open Settings \xe2\x86\x92 "
                               "Integrations \xe2\x86\x92 Pair a phone for a new one.");
    step(400);
    shot("78_pairing_failed");
    ui_pop();
    step(400);

    /* ---- confirmations ---- */
    s2_status_net(APP_NET_OK, DEMO_NOW - 300, "hub.example.ts.net", NULL);
    step(300);
    s2_show_exact("Unpair");
    s2_tap_exact("Unpair");
    step(400);
    CHECK(ui_sheet_is_open());
    shot("79_unpair_confirm");
    ui_sheet_close();
    step(400);
    s2_scroll_end(lv_obj_get_child(ui_tab_page(UI_TAB_STATUS), 0));
    tap_text("Open LilyGO");
    step(400);
    shot("80_launcher_confirm");
    ui_sheet_close();
    step(400);
    {
        int32_t x, y;
        if (center_of("Hold to wipe", &x, &y)) {
            headless_touch(x, y, true);
            step(800);
            shot("81_wipe_holding");
            headless_touch(x, y, false);
            step(60);
            CHECK(app_feed() != NULL); /* let go early: nothing wiped */
            step(3000);
        }
    }

    /* ---- the ambient screen ---- */
    ui_show_tab(UI_TAB_TODAY, false);
    step(300);
    ui_screen_off(); /* as a fist or presence "left" would */
    step(300);
    CHECK(ui_screen_is_off());
    shot("82_ambient");
    app_set_clock_override(DEMO_NOW + 7 * 60); /* seven minutes later: moved a little */
    step(1200);
    shot("83_ambient_later");
    headless_tap(284, 600); /* a touch wakes it */
    step(300);
    CHECK(!ui_screen_is_off());
    s2_settings(false, false, 30, 5);
    step(31000); /* no touch for 30 s: the ambient screen; 5 s later, off */
    CHECK(ui_screen_is_off());
    step(5500);
    shot("84_ambient_off");
    headless_tap(284, 600);
    step(300);
    CHECK(!ui_screen_is_off());
    s2_settings(false, false, APP_DIM_AFTER_DEFAULT, APP_OFF_AFTER_DEFAULT);
    app_set_clock_override(DEMO_NOW);
    step(100);

    /* ---- the hold ring on the tick, and the focus ring on Talks ---- */
    s2_settings(true, false, 120, 900);
    ui_show_tab(UI_TAB_TASKS, false);
    step(300);
    lv_obj_scroll_to_y(lv_obj_get_child(ui_tab_page(UI_TAB_TASKS), 0), 0, LV_ANIM_OFF);
    step(60);
    hand_at("Coffee beans", 0.1, "point");
    vision("{\"t\":\"gesture\",\"g\":\"point\",\"conf\":0.9}");
    step(300);
    hand_at("Coffee beans", 0.1, "thumbs_up");
    step(550);
    shot("85_handsfree_hold_ring_tasks");
    vision("{\"t\":\"hand\",\"x\":0.5,\"y\":0.3,\"shape\":\"open\"}"); /* abort: nothing changes */
    vision("{\"t\":\"hand\",\"shape\":\"none\"}");
    step(300);
    ui_show_tab(UI_TAB_TALKS, false);
    step(300);
    hand_at("Dinner for Mom", 0.0, "point");
    vision("{\"t\":\"gesture\",\"g\":\"point\",\"conf\":0.9}");
    step(400);
    shot("86_talks_focus_ring");
    vision("{\"t\":\"hand\",\"shape\":\"none\"}");
    s2_settings(false, false, APP_DIM_AFTER_DEFAULT, APP_OFF_AFTER_DEFAULT);
    step(200);
}

#endif
