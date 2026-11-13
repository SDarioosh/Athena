/* brief: the digest's headline and summary, its day, and digest_refresh. */
#include "brief.h"
#include "check.h"
#include "pda_actions.h"
#include "pda_time.h"
#include "test_util.h"

static void str_is(const char *got, const char *want)
{
    if (strcmp(got, want) != 0) {
        fprintf(stderr, "got  \"%s\"\nwant \"%s\"\n", got, want);
        checks_failed++;
    }
}

static app_pending_t pend_of(uint32_t id, bool listed, const char *json)
{
    app_pending_t p;
    memset(&p, 0, sizeof(p));
    p.id = id;
    p.listed = listed;
    CHECK(pda_action_parse(json, strlen(json), &p.info));
    return p;
}

int main(void)
{
    char head[BRIEF_HEADLINE_MAX + 1];
    const char *body;

    /* the relay's text: headline, a blank line, the summary */
    brief_split("A quiet Monday.\n\nMaria confirmed the dinner.\nBen is worried.", head, sizeof(head), &body);
    str_is(head, "A quiet Monday.");
    str_is(body, "Maria confirmed the dinner.\nBen is worried.");
    /* no blank line: all summary */
    brief_split("A quiet Monday. Maria confirmed the dinner.", head, sizeof(head), &body);
    str_is(head, "");
    str_is(body, "A quiet Monday. Maria confirmed the dinner.");
    /* a first paragraph of two lines, or a very long one, is no headline */
    brief_split("One\nTwo\n\nThree", head, sizeof(head), &body);
    str_is(head, "");
    str_is(body, "One\nTwo\n\nThree");
    char lng[400];
    memset(lng, 'x', 300);
    strcpy(lng + 300, "\n\nrest");
    brief_split(lng, head, sizeof(head), &body);
    str_is(head, "");
    CHECK(body == lng);
    /* extra blank lines and NULL */
    brief_split("\n\nHead\n\n\n\nBody", head, sizeof(head), &body);
    str_is(head, "Head");
    str_is(body, "Body");
    brief_split(NULL, head, sizeof(head), &body);
    str_is(head, "");
    str_is(body, "");

    /* the demo feed's digest is yesterday's */
    size_t len;
    char *json = read_fixture("feed_demo.json", &len);
    static feed_t f;
    CHECK_EQ(feed_parse(json, len, &f), 0);
    free(json);
    const int32_t today = pda_local_day(DEMO_NOW, DEMO_TZ);
    CHECK_EQ(brief_day(&f), today - 1);
    char d[24];
    brief_day_name(brief_day(&f), today, d, sizeof(d));
    str_is(d, "Yesterday");
    brief_day_name(today, today, d, sizeof(d));
    str_is(d, "Today");
    brief_day_name(today - 3, today, d, sizeof(d));
    str_is(d, "Sat 17 Oct");

    /* digest_refresh: the briefing's date (T5), today's without one */
    char date[16], js[PDA_ACTION_BUF];
    brief_refresh_date(&f, today, date, sizeof(date));
    str_is(date, "2026-10-19");
    size_t n = brief_refresh_action(&f, today, js);
    CHECK_EQ(n, strlen(js));
    str_is(js, "{\"t\":\"digest_refresh\",\"date\":\"2026-10-19\"}");
    CHECK_EQ(brief_day(NULL), FEED_NO_DAY);
    brief_refresh_date(NULL, today, date, sizeof(date));
    str_is(date, "2026-10-20");
    feed_t none;
    memset(&none, 0, sizeof(none));
    brief_refresh_action(&none, today, js);
    str_is(js, "{\"t\":\"digest_refresh\",\"date\":\"2026-10-20\"}");

    /* pending and failed, by date */
    app_pending_t p[3];
    p[0] = pend_of(1, true, "{\"t\":\"digest_refresh\",\"date\":\"2026-10-18\"}");
    p[1] = pend_of(2, false, "{\"t\":\"digest_refresh\",\"date\":\"2026-10-19\"}");
    p[2] = pend_of(3, false, "{\"t\":\"item_status\",\"id\":42,\"status\":\"done\"}");
    const app_pending_t *hit = brief_find_pending(p, 3, "2026-10-19");
    CHECK(hit != NULL && hit->id == 2 && !hit->listed);
    CHECK(brief_find_pending(p, 3, "2026-10-20") == NULL);
    CHECK(brief_find_pending(p, 0, "2026-10-19") == NULL);
    app_failed_t fl[2];
    memset(fl, 0, sizeof(fl));
    fl[0].type = PDA_ACT_CAL_ADD;
    snprintf(fl[0].what, sizeof(fl[0].what), "2026-10-19");
    fl[1].type = PDA_ACT_DIGEST_REFRESH;
    snprintf(fl[1].what, sizeof(fl[1].what), "2026-10-19");
    CHECK(brief_find_failed(fl, 2, "2026-10-19"));
    CHECK(!brief_find_failed(fl, 1, "2026-10-19"));
    CHECK(!brief_find_failed(fl, 2, "2026-10-18"));

    /* through the app state: a queued refresh shows as pending until acked */
    const char *dir = temp_dir();
    char state[64];
    snprintf(state, sizeof(state), "%s/s", dir);
    CHECK_EQ(app_init(state, 7), 0);
    n = brief_refresh_action(&f, today, js);
    CHECK_EQ(app_queue(js, n), 0);
    const app_pending_t *all;
    const int np = app_pending(&all);
    CHECK(brief_find_pending(all, np, "2026-10-19") != NULL);
    app_deinit();

    feed_free(&f);
    return checks_done("test_brief");
}
