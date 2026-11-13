/* talks.c: relative times, people, initials, a conversation's action items and their tap /
 * undo / dismiss / reopen (the T5's on_item), the item's second line, transcript refs. */
#include <string.h>

#include "app_state.h"
#include "check.h"
#include "pda_proto.h"
#include "talks.h"
#include "test_util.h"

static void expect_rel(int64_t at, const char *want)
{
    char b[48];
    talks_rel_time(at, DEMO_NOW, DEMO_TZ, b, sizeof(b));
    if (strcmp(b, want) != 0) {
        fprintf(stderr, "rel_time(%lld): \"%s\", want \"%s\"\n", (long long)(at - DEMO_NOW), b, want);
    }
    CHECK(strcmp(b, want) == 0);
}

static void test_rel_time(void)
{
    /* DEMO_NOW: Tue 20 Oct 2026 09:05 local (07:05Z, +120) */
    expect_rel(0, "");
    expect_rel(DEMO_NOW - 20, "Just now");
    expect_rel(DEMO_NOW, "Just now");
    expect_rel(DEMO_NOW - 60, "1 min ago");
    expect_rel(DEMO_NOW - 25 * 60, "25 min ago");
    expect_rel(DEMO_NOW - 3600, "1 h ago");
    expect_rel(DEMO_NOW - 5 * 3600 - 59 * 60, "5 h ago");
    /* 6 h back is still today? 03:05 local: yes */
    expect_rel(DEMO_NOW - 6 * 3600, "Today 03:05");
    expect_rel(DEMO_NOW - 9 * 3600 - 5 * 60, "Today 00:00");      /* local midnight */
    expect_rel(DEMO_NOW - 9 * 3600 - 10 * 60, "Yesterday 23:55"); /* 21:55Z the 19th */
    expect_rel(DEMO_NOW - 2 * 86400, "Sun 09:05");
    expect_rel(DEMO_NOW - 6 * 86400, "Wed 09:05");
    expect_rel(DEMO_NOW - 7 * 86400, "Tue 13 Oct");
    expect_rel(DEMO_NOW + 86400, "Tomorrow 09:05");
    expect_rel(DEMO_NOW + 3 * 86400, "Fri 23 Oct");
    expect_rel(DEMO_NOW + 3600, "Today 10:05"); /* ahead: never "ago" */
    char small[6];
    talks_rel_time(DEMO_NOW - 25 * 60, DEMO_NOW, DEMO_TZ, small, sizeof(small));
    CHECK_EQ(strlen(small), 5); /* cut, terminated */
}

static void test_people(void)
{
    const char *p[] = {"Alice", "Ben", "Maria", "Tom"};
    char b[96];
    talks_people(p, 0, 2, b, sizeof(b));
    CHECK(strcmp(b, "") == 0);
    talks_people(p, 1, 2, b, sizeof(b));
    CHECK(strcmp(b, "Alice") == 0);
    talks_people(p, 2, 2, b, sizeof(b));
    CHECK(strcmp(b, "Alice and Ben") == 0);
    talks_people(p, 3, 3, b, sizeof(b));
    CHECK(strcmp(b, "Alice, Ben and Maria") == 0);
    talks_people(p, 4, 2, b, sizeof(b));
    CHECK(strcmp(b, "Alice, Ben and 2 more") == 0);

    talks_initials("Maria", b, sizeof(b));
    CHECK(strcmp(b, "M") == 0);
    talks_initials("Aunt Rosa", b, sizeof(b));
    CHECK(strcmp(b, "AR") == 0);
    talks_initials("jonas", b, sizeof(b));
    CHECK(strcmp(b, "J") == 0);
    talks_initials("\xc3\x89lise Martin Weber", b, sizeof(b)); /* Élise: two letters at most */
    CHECK(strcmp(b, "\xc3\x89M") == 0);
    talks_initials("", b, sizeof(b));
    CHECK(strcmp(b, "?") == 0);
    talks_initials(NULL, b, sizeof(b));
    CHECK(strcmp(b, "?") == 0);
}

static int action_index(const feed_t *f, int64_t id)
{
    for (int i = 0; i < f->n_actions; i++) {
        if (f->actions[i].id == id) {
            return i;
        }
    }
    return -1;
}

static void test_items(const char *dir)
{
    CHECK_EQ(app_init(dir, 0x77), 0);
    app_set_clock_override(DEMO_NOW);
    app_set_time(DEMO_NOW * 1000, DEMO_TZ);
    size_t len;
    char *json = read_fixture("feed_insight_demo.json", &len);
    CHECK_EQ(app_commit_blob(PDA_KIND_FEED, 1, (const uint8_t *)json, len), 0);
    free(json);
    const feed_t *f = app_feed();
    CHECK(f != NULL);

    int idx[8];
    const int n = talks_conv_actions(f, "c_demo_maria_oct", idx, 8);
    CHECK_EQ(n, 2);
    CHECK(f->actions[idx[0]].conv && strcmp(f->actions[idx[0]].conv, "c_demo_maria_oct") == 0);
    CHECK_EQ(talks_conv_actions(f, "c_none", idx, 8), 0);
    CHECK_EQ(talks_conv_actions(NULL, "c_demo_maria_oct", idx, 8), 0);
    CHECK_EQ(talks_conv_actions(f, "c_demo_standup", idx, 1), 1); /* max */

    const int a = action_index(f, 17); /* Alice's vendor contracts: no due, owner Me */
    CHECK(a >= 0);
    talks_item_view_t v;
    char line[160];
    talks_item_view(&f->actions[a], &v);
    CHECK(v.state == TALKS_ITEM_OPEN && !v.pending && !v.listed && !v.failed);
    talks_item_line(&f->actions[a], &v, app_today(), line, sizeof(line));
    CHECK(strcmp(line, "Me") == 0);

    /* tap: done, queued; a second tap before syncd has it: undone, nothing queued */
    CHECK_EQ(talks_item_tap(a, false), TALKS_TAP_QUEUED);
    CHECK_EQ(app_outbox_count(), 1);
    talks_item_view(&f->actions[a], &v);
    CHECK(v.state == TALKS_ITEM_DONE && v.pending && !v.listed);
    talks_item_line(&f->actions[a], &v, app_today(), line, sizeof(line));
    CHECK(strstr(line, "Done") == line && strstr(line, "tap to undo"));
    CHECK_EQ(talks_item_tap(a, false), TALKS_TAP_UNDONE);
    CHECK_EQ(app_outbox_count(), 0);
    talks_item_view(&f->actions[a], &v);
    CHECK(v.state == TALKS_ITEM_OPEN && !v.pending);

    /* long press: dismissed */
    CHECK_EQ(talks_item_tap(a, true), TALKS_TAP_QUEUED);
    talks_item_view(&f->actions[a], &v);
    CHECK(v.state == TALKS_ITEM_DISMISSED && v.pending);
    const app_pending_t *pend;
    CHECK_EQ(app_pending(&pend), 1);
    CHECK(strcmp(pend[0].info.status, "dismissed") == 0);
    CHECK_EQ(pend[0].info.item_id, 17);

    /* syncd lists it: a tap now queues the reverse ("open"), which the item shows */
    pda_ob_entry_t e[4];
    uint32_t rem;
    CHECK_EQ(app_store()->ob_list(NULL, 0, e, 4, &rem), 1);
    talks_item_view(&f->actions[a], &v);
    CHECK(v.listed);
    talks_item_line(&f->actions[a], &v, app_today(), line, sizeof(line));
    CHECK(strstr(line, "sent") != NULL);
    CHECK_EQ(talks_item_tap(a, false), TALKS_TAP_REOPEN);
    CHECK_EQ(app_outbox_count(), 2);
    talks_item_view(&f->actions[a], &v);
    CHECK(v.state == TALKS_ITEM_OPEN && v.pending && !v.listed);
    /* and a tap on that takes the reopen back: the listed dismissal shows again */
    CHECK_EQ(talks_item_tap(a, false), TALKS_TAP_UNDONE);
    talks_item_view(&f->actions[a], &v);
    CHECK(v.state == TALKS_ITEM_DISMISSED && v.listed);

    /* the hub refuses the dismissal: "Couldn't sync" until the next feed */
    bool was;
    CHECK_EQ(app_store()->ob_ack(NULL, e[0].action_id, 1, &was), 0);
    talks_item_view(&f->actions[a], &v);
    CHECK(v.failed && !v.pending && v.state == TALKS_ITEM_OPEN);
    talks_item_line(&f->actions[a], &v, app_today(), line, sizeof(line));
    CHECK(strstr(line, "Couldn't sync") == line);

    /* due dates: overdue, today, tomorrow, later */
    feed_action_t x = {.id = 99, .text = "x", .owner = "Me", .due_day = app_today() - 3};
    talks_item_view_t open = {0};
    talks_item_line(&x, &open, app_today(), line, sizeof(line));
    CHECK(strcmp(line, "Overdue since Sat 17 Oct \xc2\xb7 Me") == 0);
    x.due_day = app_today();
    talks_item_line(&x, &open, app_today(), line, sizeof(line));
    CHECK(strcmp(line, "Due today \xc2\xb7 Me") == 0);
    x.due_day = app_today() + 1;
    x.owner = NULL;
    talks_item_line(&x, &open, app_today(), line, sizeof(line));
    CHECK(strcmp(line, "Due tomorrow") == 0);
    x.due_day = app_today() + 3;
    talks_item_line(&x, &open, app_today(), line, sizeof(line));
    CHECK(strcmp(line, "Due Fri 23 Oct") == 0);

    CHECK_EQ(talks_item_tap(-1, false), TALKS_TAP_ERROR);
    CHECK_EQ(talks_item_tap(f->n_actions, false), TALKS_TAP_ERROR);
    app_deinit();
}

static void test_transcript_words(void)
{
    char b[96];
    CHECK(talks_transcript_ref("c_demo_alice_1on1", b, sizeof(b)) > 0);
    CHECK(strcmp(b, "transcript:c_demo_alice_1on1@0-600") == 0);
    char tiny[8];
    CHECK_EQ(talks_transcript_ref("c_demo_alice_1on1", tiny, sizeof(tiny)), 0);
    talks_part_label(0, 600, b, sizeof(b));
    CHECK(strcmp(b, "0\xe2\x80\x93" "10 min") == 0);
    talks_part_label(600, 1200, b, sizeof(b));
    CHECK(strcmp(b, "10\xe2\x80\x93" "20 min") == 0);
    talks_part_label(600, 600, b, sizeof(b));
    CHECK(strcmp(b, "") == 0);
    talks_offset(0, b, sizeof(b));
    CHECK(strcmp(b, "00:00") == 0);
    talks_offset(136.7, b, sizeof(b));
    CHECK(strcmp(b, "02:16") == 0);
    talks_offset(3736, b, sizeof(b));
    CHECK(strcmp(b, "1:02:16") == 0);
    talks_offset(-4, b, sizeof(b));
    CHECK(strcmp(b, "00:00") == 0);
}

int main(void)
{
    test_rel_time();
    test_people();
    test_items(temp_dir());
    test_transcript_words();
    return checks_done("test_talks");
}
