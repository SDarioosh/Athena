/* agenda: the Calendar tab's rows from the demo feed and queued cal_adds. */
#include "agenda.h"
#include "check.h"
#include "pda_actions.h"
#include "pda_time.h"
#include "test_util.h"

static app_pending_t pend_of(uint32_t id, const char *json)
{
    app_pending_t p;
    memset(&p, 0, sizeof(p));
    p.id = id;
    CHECK(pda_action_parse(json, strlen(json), &p.info));
    return p;
}

static const char *title_of(const feed_t *f, const app_pending_t *pend, const agenda_row_t *r)
{
    switch (r->kind) {
    case AGENDA_DAY: return "#";
    case AGENDA_EVENT: return f->events[r->idx].title;
    case AGENDA_PENDING: return pend[r->idx].info.title;
    }
    return "?";
}

static void rows_are(const feed_t *f, const app_pending_t *pend, const agenda_row_t *rows, int n,
                     const char *const *want, int n_want)
{
    CHECK_EQ(n, n_want);
    for (int i = 0; i < n && i < n_want; i++) {
        const char *got = title_of(f, pend, &rows[i]);
        if (strcmp(got, want[i]) != 0) {
            fprintf(stderr, "row %d: got \"%s\", want \"%s\"\n", i, got, want[i]);
            checks_failed++;
        }
    }
}

int main(void)
{
    size_t len;
    char *json = read_fixture("feed_insight_demo.json", &len);
    static feed_t f;
    CHECK_EQ(feed_parse(json, len, &f), 0);
    free(json);
    const int32_t today = pda_local_day(DEMO_NOW, DEMO_TZ);
    static agenda_row_t rows[AGENDA_MAX];

    /* events only: the call at 08:00 local is over at 09:05 and left out */
    int n = agenda_build(&f, NULL, 0, today, DEMO_NOW, DEMO_TZ, rows, AGENDA_MAX);
    static const char *const want1[] = {
        "#", "Team stand-up", "Lunch with Maria", "1:1 with Alice",
        "#", "Dentist follow-up (Dr. Patel)",
        "#", "Ben\xe2\x80\x99s PR review session",
        "#", "Client demo",
        "#", "Jonas: long run (cheer at km 20)",
        "#", "Lease renewal reminder",
    };
    rows_are(&f, NULL, rows, n, want1, (int)(sizeof(want1) / sizeof(want1[0])));
    CHECK_EQ(rows[0].day, today);
    CHECK_EQ(rows[4].day, today + 1);
    CHECK_EQ(rows[10].day, today + 5);
    CHECK(rows[11].all_day);
    CHECK(!rows[1].now);
    CHECK_EQ(agenda_count_items(rows, n), 8);

    /* the stand-up (07:30Z-07:45Z) is on at 07:35Z */
    n = agenda_build(&f, NULL, 0, today, DEMO_NOW + 30 * 60, DEMO_TZ, rows, AGENDA_MAX);
    CHECK(rows[1].now);
    CHECK(!rows[2].now);
    /* ... and over at 07:45Z */
    n = agenda_build(&f, NULL, 0, today, DEMO_NOW + 40 * 60, DEMO_TZ, rows, AGENDA_MAX);
    CHECK(strcmp(title_of(&f, NULL, &rows[1]), "Lunch with Maria") == 0);

    /* queued adds merged in by day and time */
    app_pending_t pend[6];
    pend[0] = pend_of(11, "{\"t\":\"cal_add\",\"title\":\"Call Ben\",\"start\":\"2026-10-20T09:30:00+02:00\","
                          "\"end\":\"2026-10-20T09:45:00+02:00\"}"); /* = the stand-up: after it */
    pend[1] = pend_of(12, "{\"t\":\"cal_add\",\"title\":\"Pack\",\"start\":\"2026-10-21\",\"all_day\":true}");
    pend[2] = pend_of(13, "{\"t\":\"cal_add\",\"title\":\"Over\",\"start\":\"2026-10-20T08:00:00+02:00\","
                          "\"end\":\"2026-10-20T08:30:00+02:00\"}"); /* over at 09:05 */
    pend[3] = pend_of(14, "{\"t\":\"cal_add\",\"title\":\"Far\",\"start\":\"2026-11-09T10:00:00+01:00\","
                          "\"end\":\"2026-11-09T10:30:00+01:00\"}"); /* +20 days: its own day */
    pend[4] = pend_of(15, "{\"t\":\"cal_add\",\"title\":\"Too far\",\"start\":\"2026-12-01\",\"all_day\":true}");
    pend[5] = pend_of(16, "{\"t\":\"item_status\",\"id\":42,\"status\":\"done\"}");
    n = agenda_build(&f, pend, 6, today, DEMO_NOW, DEMO_TZ, rows, AGENDA_MAX);
    static const char *const want2[] = {
        "#", "Team stand-up", "Call Ben", "Lunch with Maria", "1:1 with Alice",
        "#", "Pack", "Dentist follow-up (Dr. Patel)",
        "#", "Ben\xe2\x80\x99s PR review session",
        "#", "Client demo",
        "#", "Jonas: long run (cheer at km 20)",
        "#", "Lease renewal reminder",
        "#", "Far",
    };
    rows_are(&f, pend, rows, n, want2, (int)(sizeof(want2) / sizeof(want2[0])));
    CHECK_EQ(rows[2].kind, AGENDA_PENDING);
    CHECK_EQ(rows[2].idx, 0);
    CHECK_EQ(rows[2].end - rows[2].start, 15 * 60);
    CHECK(rows[6].all_day);
    CHECK_EQ(rows[17].day, today + 20);

    /* pending only, without a feed */
    n = agenda_build(NULL, pend, 2, today, DEMO_NOW, DEMO_TZ, rows, AGENDA_MAX);
    CHECK_EQ(n, 4);

    /* a cut list never ends on a heading */
    n = agenda_build(&f, NULL, 0, today, DEMO_NOW, DEMO_TZ, rows, 5);
    CHECK_EQ(n, 4);
    CHECK(rows[n - 1].kind != AGENDA_DAY);

    /* the local day decides: 22:30Z on the 20th is 00:30 on the 21st at +02:00, still the
     * 20th at -05:00; an event that started yesterday and is still on shows today */
    static const char late[] =
        "{\"v\":1,\"version\":1,\"generated\":\"2026-10-20T07:00:00Z\",\"conversations\":[],\"actions\":[],"
        "\"calendar\":[{\"id\":\"a\",\"title\":\"Late\",\"start\":\"2026-10-20T22:30:00Z\",\"end\":\"2026-10-20T23:00:00Z\"},"
        "{\"id\":\"b\",\"title\":\"Trip\",\"start\":\"2026-10-19T20:00:00Z\",\"end\":\"2026-10-21T08:00:00Z\"},"
        "{\"id\":\"c\",\"title\":\"Next week\",\"start\":\"2026-10-28T08:00:00Z\",\"end\":\"2026-10-28T09:00:00Z\"}]}";
    static feed_t g;
    CHECK_EQ(feed_parse(late, sizeof(late) - 1, &g), 0);
    n = agenda_build(&g, NULL, 0, today, DEMO_NOW, DEMO_TZ, rows, AGENDA_MAX);
    CHECK_EQ(n, 4);
    CHECK(strcmp(title_of(&g, NULL, &rows[1]), "Trip") == 0);
    CHECK(rows[1].now);
    CHECK_EQ(rows[2].day, today + 1);
    const int32_t today_ny = pda_local_day(DEMO_NOW, -300);
    n = agenda_build(&g, NULL, 0, today_ny, DEMO_NOW, -300, rows, AGENDA_MAX);
    CHECK_EQ(n, 3);
    CHECK_EQ(rows[0].day, today_ny);

    char d[24];
    agenda_day_name(today, today, d, sizeof(d));
    CHECK(strcmp(d, "Today") == 0);
    agenda_day_name(today + 1, today, d, sizeof(d));
    CHECK(strcmp(d, "Tomorrow") == 0);
    agenda_day_name(today + 2, today, d, sizeof(d));
    CHECK(strcmp(d, "Thu 22 Oct") == 0);

    feed_free(&f);
    feed_free(&g);
    return checks_done("test_agenda");
}
