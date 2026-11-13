/* app_state: the feed, the outbox and its pending / failed marks, the checklist taps (undo,
 * reverse), the store pda_proto drives, ui.json, the status, reload from disk. */
#include <string.h>
#include <sys/stat.h>

#include "app_state.h"
#include "check.h"
#include "pda_proto.h"
#include "test_util.h"

static int ev_count[16];
static int32_t ev_last_arg[16];

static void listener(void *ctx, app_ev_t ev, int32_t arg)
{
    (void)ctx;
    ev_count[ev]++;
    ev_last_arg[ev] = arg;
}

static void put_feed(const char *name)
{
    size_t len;
    char *json = read_fixture(name, &len);
    CHECK_EQ(app_commit_blob(PDA_KIND_FEED, 7, (const uint8_t *)json, len), 0);
    free(json);
}

static void test_feed_and_taps(const char *dir)
{
    CHECK_EQ(app_init(dir, 0x1234), 0);
    app_add_listener(listener, NULL);
    CHECK(app_feed() == NULL);
    pda_status_t st;
    app_set_clock_override(DEMO_NOW);
    app_status(&st);
    CHECK_EQ(st.feed_version, 0);
    CHECK_EQ(st.outbox, 0);
    CHECK(st.flags & PDA_STATUS_TIME_VALID);
    CHECK(!(st.flags & PDA_STATUS_WINDOW));

    /* a broken feed is refused, nothing stored */
    CHECK_EQ(app_commit_blob(PDA_KIND_FEED, 1, (const uint8_t *)"{\"v\":1", 6), PDA_E_BAD_STATE);
    CHECK(app_feed() == NULL);

    put_feed("feed_insight_demo.json");
    const feed_t *f = app_feed();
    CHECK(f != NULL);
    CHECK_EQ(f->version, 1792479600u);
    CHECK_EQ(ev_count[APP_EV_FEED], 1);
    app_status(&st);
    CHECK_EQ(st.feed_version, 1792479600u);

    const int work = note_index(f, "note_demo_work");
    CHECK(work >= 0);
    app_todo_view_t v;
    app_todo_view(work, 0, &v);
    CHECK(!v.done && !v.pending && !v.failed);

    /* tap: queued, shown done at once */
    CHECK_EQ(app_todo_tap(work, 0), APP_TODO_QUEUED);
    CHECK_EQ(app_outbox_count(), 1);
    app_todo_view(work, 0, &v);
    CHECK(v.done && v.pending);
    const app_pending_t *p;
    CHECK_EQ(app_pending(&p), 1);
    CHECK_EQ(p[0].info.type, PDA_ACT_TODO_TOGGLE);
    CHECK(strcmp(p[0].info.ref, "note_demo_work") == 0);
    CHECK(p[0].info.done);

    /* tap again before syncd has it: undone, nothing queued */
    CHECK_EQ(app_todo_tap(work, 0), APP_TODO_UNDONE);
    CHECK_EQ(app_outbox_count(), 0);
    app_todo_view(work, 0, &v);
    CHECK(!v.done && !v.pending);

    /* tap, syncd lists it, tap again: the reverse is queued */
    CHECK_EQ(app_todo_tap(work, 0), APP_TODO_QUEUED);
    pda_ob_entry_t e[2];
    uint32_t rem = 99;
    const pda_store_t *s = app_store();
    CHECK_EQ(s->ob_list(NULL, 0, e, 2, &rem), 1);
    CHECK_EQ(rem, 0);
    CHECK(strstr(e[0].json, "\"t\":\"todo_toggle\"") != NULL);
    CHECK(strstr(e[0].json, "\"done\":true") != NULL);
    CHECK(app_pending_todo("note_demo_work", 0)->listed);
    CHECK_EQ(app_todo_tap(work, 0), APP_TODO_REVERSE);
    CHECK_EQ(app_outbox_count(), 2);
    app_todo_view(work, 0, &v);
    CHECK(!v.done && v.pending); /* the newest toggle wins */

    /* the server refuses the first for good: "couldn't sync" with the item's text */
    bool was = false;
    const int failed_before = ev_count[APP_EV_FAILED];
    CHECK_EQ(s->ob_ack(NULL, e[0].action_id, 1, &was), 0);
    CHECK(was);
    CHECK_EQ(ev_count[APP_EV_FAILED], failed_before + 1);
    const app_failed_t *fl;
    CHECK_EQ(app_failed(&fl), 1);
    CHECK(strcmp(fl[0].label, "Send the Q4 budget sheet to Alice") == 0);
    CHECK(app_failed_todo("note_demo_work", 0) != NULL);
    app_todo_view(work, 0, &v);
    CHECK(v.failed);
    /* an ack of an id that is gone is still fine */
    CHECK_EQ(s->ob_ack(NULL, e[0].action_id, 0, &was), 0);
    CHECK(!was);

    /* a new feed shows the server's truth: failed marks go */
    put_feed("feed_demo.json");
    CHECK_EQ(app_failed(&fl), 0);
    CHECK_EQ(app_outbox_count(), 1); /* the reverse toggle still waits */

    /* TIME sets the display offset only, kept in ui.json */
    CHECK_EQ(s->set_time(NULL, DEMO_NOW * 1000, DEMO_TZ), 0);
    CHECK_EQ(app_tz_min(), DEMO_TZ);
    CHECK(app_now() == DEMO_NOW);

    /* settings */
    app_settings_t set = *app_settings();
    CHECK(!set.hands_free);
    CHECK_EQ(set.brightness, 80);
    set.hands_free = true;
    set.brightness = 250; /* clamped */
    CHECK_EQ(app_set_settings(&set), 0);
    CHECK(app_settings()->hands_free);
    CHECK_EQ(app_settings()->brightness, 100);
    bool g, pr, q;
    app_vision_mode(&g, &pr, &q);
    CHECK(g && !pr && !q);
    app_set_qr_scan(true);
    app_vision_mode(&g, &pr, &q);
    CHECK(q);

    /* user activity: status bit4 for a minute */
    const int64_t t0 = app_mono_ms() + 10 * APP_ACTIVE_MS;
    app_set_mono_override(t0);
    CHECK(!app_user_active());
    app_touch_activity();
    app_status(&st);
    CHECK(st.flags & PDA_STATUS_ACTIVE);
    app_set_mono_override(t0 + APP_ACTIVE_MS + 1);
    CHECK(!app_user_active());
    app_set_mono_override(0);

    /* power */
    app_set_power(76, true, true);
    app_status(&st);
    CHECK_EQ(st.battery_pct, 76);
    CHECK(st.flags & PDA_STATUS_CHARGING);
    CHECK(st.flags & PDA_STATUS_USB);

    /* a detail blob is cached by its ref */
    size_t dl;
    char *dj = read_fixture("detail_conv_demo.json", &dl);
    CHECK_EQ(app_commit_blob(PDA_KIND_DETAIL, 9, (const uint8_t *)dj, dl), 0);
    free(dj);
    CHECK_EQ(ev_count[APP_EV_DETAIL], 1);
    app_remove_listener(listener, NULL);
}

static void test_reload(const char *dir)
{
    /* everything comes back from disk */
    CHECK_EQ(app_init(dir, 0x9999), 0);
    CHECK(app_feed() != NULL);
    CHECK_EQ(app_feed()->version, 1792479600u);
    CHECK(note_index(app_feed(), "note_demo_untitled") >= 0);
    CHECK_EQ(app_outbox_count(), 1);
    CHECK_EQ(app_tz_min(), DEMO_TZ);
    CHECK(app_settings()->hands_free);
    CHECK_EQ(app_settings()->brightness, 100);
    const app_pending_t *p;
    CHECK_EQ(app_pending(&p), 1);
    CHECK(!p[0].listed); /* listed is RAM only: syncd sees it again */
    detail_t *d = app_load_detail("conv:c_demo_alice_1on1");
    CHECK(d != NULL);
    if (d) {
        CHECK(strcmp(d->title, "1:1 with Alice: Berlin move and vendor contracts") == 0);
        detail_free(d);
        free(d);
    }
    CHECK(app_load_detail("conv:nope") == NULL);

    /* wipe */
    CHECK_EQ(app_wipe(), 0);
    CHECK(app_feed() == NULL);
    CHECK_EQ(app_outbox_count(), 0);

    /* a directory too long for the outbox's paths is refused */
    char longdir[128];
    memset(longdir, 'a', sizeof(longdir));
    longdir[0] = '/';
    longdir[100] = 0;
    CHECK_EQ(app_init(longdir, 1), -1);
    app_deinit();
}

int main(void)
{
    const char *dir = temp_dir();
    test_feed_and_taps(dir);
    test_reload(dir);
    return checks_done("test_app_state");
}
