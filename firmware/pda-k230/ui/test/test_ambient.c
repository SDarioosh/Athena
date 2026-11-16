/* ambient.c (the ambient screen's timing and words), the screen timeouts in ui.json
 * (defaults, kept, clamped, an older ui.json without them), and wifi_info.c. */
#include <string.h>
#include <sys/stat.h>

#include "ambient.h"
#include "app_state.h"
#include "check.h"
#include "pda_proto.h"
#include "test_util.h"
#include "wifi_info.h"

static void test_timing(void)
{
    amb_t a;
    amb_reset(&a, 1000);
    amb_cfg_t c = {.dim_after_s = 120, .off_after_s = 900};
    CHECK_EQ(a.stage, AMB_AWAKE);
    /* no activity since the start: dims 120 s after it */
    CHECK(!amb_tick(&a, 1000 + 119999, 0, &c));
    CHECK(amb_tick(&a, 1000 + 120000, 0, &c));
    CHECK_EQ(a.stage, AMB_AMBIENT);
    CHECK_EQ(a.since_ms, 121000);
    /* then off 900 s later; activity doesn't matter while ambient (a touch wakes it) */
    CHECK(!amb_tick(&a, 121000 + 899999, 121000 + 899000, &c));
    CHECK(amb_tick(&a, 121000 + 900000, 0, &c));
    CHECK_EQ(a.stage, AMB_OFF);
    CHECK(!amb_tick(&a, 10000000, 0, &c));
    /* a touch wakes it; the timeout counts from then */
    CHECK(amb_wake(&a, 2000000));
    CHECK(!amb_wake(&a, 2000001));
    CHECK_EQ(a.stage, AMB_AWAKE);
    CHECK(!amb_tick(&a, 2000000 + 119999, 0, &c));
    /* later activity moves it */
    CHECK(!amb_tick(&a, 2000000 + 150000, 2000000 + 100000, &c));
    CHECK(amb_tick(&a, 2000000 + 220000, 2000000 + 100000, &c));
    /* fist / presence left: at once, only from AWAKE */
    amb_reset(&a, 0);
    CHECK(amb_dim(&a, 50));
    CHECK_EQ(a.stage, AMB_AMBIENT);
    CHECK(!amb_dim(&a, 60));
    amb_wake(&a, 70);
    /* someone present with presence wake: no timeout */
    c.hold = true;
    CHECK(!amb_tick(&a, 70 + 10000000, 0, &c));
    c.hold = false;
    CHECK(amb_tick(&a, 70 + 10000000, 0, &c));
    /* never */
    c = (amb_cfg_t){.dim_after_s = 0, .off_after_s = 0};
    amb_reset(&a, 0);
    CHECK(!amb_tick(&a, 100000000, 0, &c));
    amb_dim(&a, 1);
    CHECK(!amb_tick(&a, 100000000, 0, &c));
    CHECK_EQ(a.stage, AMB_AMBIENT);
}

static void test_shift(void)
{
    int seen[5][5] = {{0}};
    int32_t px = 0, py = 0;
    for (int m = 0; m < 25; m++) {
        int32_t dx, dy;
        amb_shift(1000000 + m, &dx, &dy);
        CHECK(dx >= -AMB_SHIFT_PX && dx <= AMB_SHIFT_PX && dy >= -AMB_SHIFT_PX && dy <= AMB_SHIFT_PX);
        const int col = dx / (AMB_SHIFT_PX / 2) + 2, row = dy / (AMB_SHIFT_PX / 2) + 2;
        CHECK(col >= 0 && col < 5 && row >= 0 && row < 5);
        seen[row][col]++;
        if (m > 0) {
            /* one grid step at a time (except the wrap from the last point to the first) */
            const int32_t step = (dx > px ? dx - px : px - dx) + (dy > py ? dy - py : py - dy);
            CHECK(step == AMB_SHIFT_PX / 2);
        }
        px = dx;
        py = dy;
    }
    for (int r = 0; r < 5; r++) {
        for (int c = 0; c < 5; c++) {
            CHECK_EQ(seen[r][c], 1);
        }
    }
    int32_t dx, dy;
    amb_shift(-3, &dx, &dy); /* negative minutes don't break it */
    CHECK(dx >= -AMB_SHIFT_PX && dx <= AMB_SHIFT_PX);
}

static void test_next_event(const char *dir)
{
    CHECK_EQ(app_init(dir, 0x55), 0);
    app_set_clock_override(DEMO_NOW);
    app_set_time(DEMO_NOW * 1000, DEMO_TZ);
    size_t len;
    char *json = read_fixture("feed_insight_demo.json", &len);
    CHECK_EQ(app_commit_blob(PDA_KIND_FEED, 1, (const uint8_t *)json, len), 0);
    free(json);
    const feed_t *f = app_feed();
    CHECK(amb_next_event(NULL, DEMO_NOW, DEMO_TZ) == -1);
    const int i = amb_next_event(f, DEMO_NOW, DEMO_TZ);
    CHECK(i >= 0);
    char w[80];
    amb_fmt_when(&f->events[i], DEMO_TZ, w, sizeof(w));
    printf("  next event: %s, %s\n", f->events[i].title, w);
    CHECK(strcmp(f->events[i].title, "Team stand-up") == 0);
    CHECK(strcmp(w, "Tue 20 Oct \xc2\xb7 09:30 \xe2\x80\x93 09:45") == 0);
    /* during it: still the stand-up (not ended); after it: the next */
    CHECK_EQ(amb_next_event(f, f->events[i].start + 60, DEMO_TZ), i);
    const int j = amb_next_event(f, f->events[i].end, DEMO_TZ);
    CHECK(j >= 0 && j != i);
    /* far ahead: none */
    CHECK_EQ(amb_next_event(f, DEMO_NOW + 400 * 86400, DEMO_TZ), -1);

    amb_fmt_date_long(DEMO_NOW, DEMO_TZ, w, sizeof(w));
    CHECK(strcmp(w, "Tuesday 20 October") == 0);
    amb_fmt_date_long(DEMO_NOW + 12 * 86400, DEMO_TZ, w, sizeof(w));
    CHECK(strcmp(w, "Sunday 1 November") == 0);
    amb_fmt_date_long(1798761600LL - 3600, 120, w, sizeof(w)); /* 2026-12-31T23:00Z = 01:00 on 1 Jan */
    CHECK(strcmp(w, "Friday 1 January") == 0);
    amb_fmt_date_long(0, 0, w, sizeof(w));
    CHECK(strcmp(w, "Thursday 1 January") == 0);

    feed_event_t all = {.title = "Holiday", .all_day = true, .start_day = app_today() + 3};
    amb_fmt_when(&all, DEMO_TZ, w, sizeof(w));
    CHECK(strcmp(w, "Fri 23 Oct \xc2\xb7 all day") == 0);
    feed_event_t pt = {.title = "Call", .start = DEMO_NOW + 3600, .end = DEMO_NOW + 3600};
    amb_fmt_when(&pt, DEMO_TZ, w, sizeof(w));
    CHECK(strcmp(w, "Tue 20 Oct \xc2\xb7 10:05") == 0);
    app_deinit();
}

static void write_file(const char *path, const char *text)
{
    FILE *f = fopen(path, "wb");
    CHECK(f != NULL);
    if (f) {
        fputs(text, f);
        fclose(f);
    }
}

static void test_settings(const char *dir)
{
    /* an older ui.json without the timeouts: the defaults, the rest kept */
    char path[96];
    mkdir(dir, 0700);
    snprintf(path, sizeof(path), "%s/ui.json", dir);
    write_file(path, "{\"v\":1,\"hands_free\":true,\"presence_wake\":false,\"brightness\":40,\"tz_min\":60}");
    CHECK_EQ(app_init(dir, 1), 0);
    const app_settings_t *s = app_settings();
    CHECK(s->hands_free);
    CHECK_EQ(s->brightness, 40);
    CHECK_EQ(s->dim_after_s, APP_DIM_AFTER_DEFAULT);
    CHECK_EQ(s->off_after_s, APP_OFF_AFTER_DEFAULT);
    CHECK_EQ(app_tz_min(), 60);

    /* set, saved, read back after a restart; clamped */
    app_settings_t n = *s;
    n.dim_after_s = 30;
    n.off_after_s = 0;
    CHECK_EQ(app_set_settings(&n), 0);
    app_deinit();
    CHECK_EQ(app_init(dir, 1), 0);
    CHECK_EQ(app_settings()->dim_after_s, 30);
    CHECK_EQ(app_settings()->off_after_s, 0);
    CHECK(app_settings()->hands_free);
    CHECK_EQ(app_tz_min(), 60);
    n = *app_settings();
    n.dim_after_s = -5;
    n.off_after_s = APP_TIMEOUT_MAX + 10;
    app_set_settings(&n);
    CHECK_EQ(app_settings()->dim_after_s, 0);
    CHECK_EQ(app_settings()->off_after_s, APP_TIMEOUT_MAX);
    app_deinit();

    /* out-of-range values in the file: ignored (the defaults) */
    write_file(path, "{\"v\":1,\"dim_after_s\":-1,\"off_after_s\":99999999}");
    CHECK_EQ(app_init(dir, 1), 0);
    CHECK_EQ(app_settings()->dim_after_s, APP_DIM_AFTER_DEFAULT);
    CHECK_EQ(app_settings()->off_after_s, APP_OFF_AFTER_DEFAULT);

    /* activity time */
    app_set_mono_override(777000);
    app_touch_activity();
    CHECK_EQ(app_last_activity_ms(), 777000);
    app_set_mono_override(0);
    app_deinit();
}

static void test_wifi(const char *dir)
{
    static const char proc[] =
        "Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n"
        " face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n"
        "  eth9: 0000   10.  -90.  -256        0      0      0      0      0        0\n"
        " wlan0: 0000   54.  -56.  -256        0      0      0      0      0        0\n";
    int lvl = 0;
    CHECK(wifi_parse_wireless(proc, "wlan0", &lvl));
    CHECK_EQ(lvl, -56);
    CHECK(wifi_parse_wireless(proc, "eth9", &lvl));
    CHECK_EQ(lvl, -90);
    CHECK(!wifi_parse_wireless(proc, "wlan1", &lvl));
    CHECK(!wifi_parse_wireless("", "wlan0", &lvl));
    CHECK(wifi_parse_wireless("wlan0: 0000 40 190 0\n", "wlan0", &lvl)); /* unsigned dBm */
    CHECK_EQ(lvl, -66);
    CHECK(!wifi_parse_wireless("wlan0: x\n", "wlan0", &lvl));
    CHECK(strcmp(wifi_signal_word(-50), "Strong") == 0);
    CHECK(strcmp(wifi_signal_word(-65), "Good") == 0);
    CHECK(strcmp(wifi_signal_word(-75), "Fair") == 0);
    CHECK(strcmp(wifi_signal_word(-85), "Weak") == 0);

    /* the files: a fake /proc and /sys */
    char p[128], sys[128], d[160];
    snprintf(p, sizeof(p), "%s/wireless", dir);
    snprintf(sys, sizeof(sys), "%s/net", dir);
    mkdir(sys, 0755);
    snprintf(d, sizeof(d), "%s/wlan0", sys);
    mkdir(d, 0755);
    snprintf(d, sizeof(d), "%s/wlan0/operstate", sys);
    write_file(d, "up\n");
    write_file(p, proc);
    wifi_info_t w;
    wifi_read(p, sys, &w);
    CHECK(w.present && w.up && w.have_level);
    CHECK_EQ(w.level_dbm, -56);
    write_file(d, "dormant\n");
    wifi_read(p, sys, &w);
    CHECK(w.present && !w.up);
    char none[160];
    snprintf(none, sizeof(none), "%s/nothing", dir);
    wifi_read(none, none, &w);
    CHECK(!w.present && !w.up && !w.have_level && !w.ip[0]);
}

int main(void)
{
    test_timing();
    test_shift();
    test_next_event(temp_dir());
    char sdir[64];
    snprintf(sdir, sizeof(sdir), "%s/s", temp_dir());
    test_settings(sdir);
    test_wifi(temp_dir());
    return checks_done("test_ambient");
}
