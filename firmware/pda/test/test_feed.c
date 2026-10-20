/* Feed and detail parsing (§4.1, §4.3) against the fictional fixtures, and feed times. */
#include <stdlib.h>
#include <string.h>

#include "check.h"
#include "feed.h"
#include "pda_time.h"

static char *load(const char *name, size_t *len)
{
    const char *dir = getenv("FIXTURES");
    char path[512];
    snprintf(path, sizeof(path), "%s/%s", dir ? dir : "fixtures", name);
    FILE *f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "missing fixture %s\n", path);
        exit(1);
    }
    fseek(f, 0, SEEK_END);
    *len = (size_t)ftell(f);
    fseek(f, 0, SEEK_SET);
    char *buf = malloc(*len + 1);
    if (fread(buf, 1, *len, f) != *len) {
        exit(1);
    }
    buf[*len] = 0;
    fclose(f);
    return buf;
}

static feed_t feed;

static void test_times(void)
{
    int64_t t;
    bool d;
    CHECK(pda_time_parse("2026-10-20T07:00:00Z", &t, &d) && t == 1792479600 && !d);
    CHECK(pda_time_parse("2026-10-20T09:00:00+02:00", &t, &d) && t == 1792479600);
    CHECK(pda_time_parse("2026-10-20T02:30:00-0430", &t, &d) && t == 1792479600);
    CHECK(pda_time_parse("2026-10-20T07:00:00.250Z", &t, &d) && t == 1792479600);
    CHECK(pda_time_parse("2026-10-20 07:00", &t, &d) && t == 1792479600);
    CHECK(pda_time_parse("2026-10-20", &t, &d) && t == 1792454400 && d);
    CHECK(!pda_time_parse("2026-10-20T07", &t, &d));
    CHECK(!pda_time_parse("20-10-2026", &t, &d));
    CHECK(!pda_time_parse("2026-10-20T07:00:00Zjunk", &t, &d));
    CHECK(!pda_time_parse("2026-13-01", &t, &d));

    char b[40];
    CHECK_EQ(pda_local_day(1792479600, 120), 20746);
    CHECK_EQ(pda_local_day(1792540800 - 1, 120), 20747); /* 01:59:59 local on the 21st */
    CHECK_EQ(pda_local_day(1792540800 - 7201, 120), 20746); /* 23:59:59 local on the 20th */
    pda_fmt_date(1792479600, 120, b, sizeof(b));
    CHECK(strcmp(b, "Tue 20 Oct") == 0);
    pda_fmt_hm(1792479600, 120, b, sizeof(b));
    CHECK(strcmp(b, "09:00") == 0);
    pda_fmt_hm(1792479600, -90, b, sizeof(b));
    CHECK(strcmp(b, "05:30") == 0);
    pda_fmt_iso_local(1792479600, 120, b, sizeof(b));
    CHECK(strcmp(b, "2026-10-20T09:00:00+02:00") == 0);
    pda_fmt_iso_local(1792479600, -90, b, sizeof(b));
    CHECK(strcmp(b, "2026-10-20T05:30:00-01:30") == 0);
    pda_fmt_ymd(20746, b, sizeof(b));
    CHECK(strcmp(b, "2026-10-20") == 0);
    pda_fmt_age(30, b, sizeof(b));
    CHECK(strcmp(b, "just now") == 0);
    pda_fmt_age(600, b, sizeof(b));
    CHECK(strcmp(b, "10 min ago") == 0);
    pda_fmt_age(7200, b, sizeof(b));
    CHECK(strcmp(b, "2 h ago") == 0);
    pda_fmt_age(3 * 86400, b, sizeof(b));
    CHECK(strcmp(b, "3 d ago") == 0);
}

static void test_fixture(void)
{
    size_t len;
    char *js = load("feed_demo.json", &len);
    CHECK_EQ(feed_parse(js, len, &feed), 0);
    free(js); /* the feed owns copies of its strings */
    CHECK_EQ(feed.version, 1792479600u);
    CHECK_EQ(feed.generated, 1792479600);
    CHECK(feed.tz && strcmp(feed.tz, "Europe/Berlin") == 0);
    CHECK(feed.has_briefing);
    CHECK(strcmp(feed.brief_date, "2026-10-19") == 0);
    CHECK(strstr(feed.brief_text, "Casa Lupo \xe2\x80\x94 twelve") != NULL); /* em dash kept */
    CHECK_EQ(feed.n_highlights, 4);
    CHECK_EQ(feed.n_conv, 5);
    CHECK(strcmp(feed.conv[0].id, "c_demo_maria_oct") == 0);
    CHECK(strcmp(feed.conv[0].title, "Dinner for Mom\xe2\x80\x99s 70th") == 0);
    CHECK_EQ(feed.conv[0].n_people, 1);
    CHECK_EQ(feed.conv[0].n_points, 2);
    CHECK_EQ(feed.conv[3].n_people, 2);
    CHECK_EQ(feed.n_actions, 7);
    /* sorted: due soonest first, undated last (by id) */
    CHECK_EQ(feed.actions[0].id, 14);
    CHECK(strcmp(feed.actions[0].due, "2026-10-07") == 0);
    CHECK_EQ(feed.actions[1].id, 15);
    CHECK_EQ(feed.actions[2].id, 12);
    CHECK_EQ(feed.actions[3].id, 11);
    CHECK_EQ(feed.actions[4].id, 18);
    CHECK_EQ(feed.actions[5].id, 16);
    CHECK_EQ(feed.actions[5].due_day, FEED_NO_DAY);
    CHECK(feed.actions[5].due[0] == 0);
    CHECK(strcmp(feed.actions[5].owner, "Tom") == 0);
    CHECK_EQ(feed.actions[6].id, 17);
    CHECK_EQ(feed.n_events, 6);
    /* by start time; the all-day events at the end of the week */
    CHECK(strcmp(feed.events[0].title, "Team stand-up") == 0);
    CHECK(strcmp(feed.events[3].title, "Client demo") == 0);
    CHECK(feed.events[4].all_day);
    CHECK_EQ(feed.events[4].start_day, 20751); /* 2026-10-25 */
    CHECK(feed.events[2].where == NULL);
    CHECK_EQ(feed.events[0].end - feed.events[0].start, 15 * 60);
    CHECK_EQ(feed.events[4].end, feed.events[4].start);
    feed_free(&feed);
}

static void test_tolerance(void)
{
    /* unknown keys, items missing required fields, wrong types: skipped, not fatal */
    const char *js =
        "{\"v\":1,\"version\":5,\"extra\":{\"x\":[1,2]},"
        "\"conversations\":[{\"title\":\"no id\"},{\"id\":\"c1\",\"title\":\"ok\",\"new\":1},7],"
        "\"actions\":[{\"id\":\"42\",\"text\":\"string id\"},{\"id\":3,\"text\":\"ok\",\"due\":\"bad\"}],"
        "\"calendar\":[{\"title\":\"no start\"},{\"title\":\"t\",\"start\":\"2026-10-20T10:00:00Z\"}]}";
    CHECK_EQ(feed_parse(js, strlen(js), &feed), 0);
    CHECK_EQ(feed.version, 5u);
    CHECK(!feed.has_briefing);
    CHECK_EQ(feed.n_conv, 1);
    CHECK_EQ(feed.conv[0].at, 0);
    CHECK_EQ(feed.n_actions, 1);
    CHECK_EQ(feed.actions[0].due_day, FEED_NO_DAY);
    CHECK_EQ(feed.n_events, 1);
    feed_free(&feed);

    /* an empty feed is valid */
    const char *empty = "{\"v\":1,\"version\":0}";
    CHECK_EQ(feed_parse(empty, strlen(empty), &feed), 0);
    CHECK(feed.n_conv == 0 && feed.n_actions == 0 && feed.n_events == 0);
    feed_free(&feed);

    /* fatal */
    const char *v2 = "{\"v\":2,\"version\":1}";
    CHECK_EQ(feed_parse(v2, strlen(v2), &feed), FEED_E_VERSION);
    const char *nov = "{\"v\":1}";
    CHECK_EQ(feed_parse(nov, strlen(nov), &feed), FEED_E_FIELD);
    const char *neg = "{\"v\":1,\"version\":-1}";
    CHECK_EQ(feed_parse(neg, strlen(neg), &feed), FEED_E_FIELD);
    const char *broken = "{\"v\":1,\"version\":1";
    CHECK_EQ(feed_parse(broken, strlen(broken), &feed), FEED_E_JSON);
    CHECK_EQ(feed_parse("[1]", 3, &feed), FEED_E_JSON);
}

static void test_details(void)
{
    size_t len;
    char *js = load("detail_transcript_demo.json", &len);
    detail_t *d = malloc(sizeof(*d));
    CHECK_EQ(detail_parse(js, len, d), 0);
    CHECK(strcmp(d->ref, "transcript:c_demo_alice_1on1@0-600") == 0);
    CHECK_EQ(d->n_seg, 5);
    CHECK(strcmp(d->seg[0].s, "Alice") == 0 && !d->seg[0].me);
    CHECK(d->seg[1].me);
    CHECK(d->seg[4].o == 32.0);
    CHECK(!d->more);
    char ref[96];
    CHECK(detail_peek_ref(js, len, ref, sizeof(ref)));
    CHECK(strcmp(ref, d->ref) == 0);
    detail_free(d);
    free(js);

    js = load("detail_conv_demo.json", &len);
    CHECK_EQ(detail_parse(js, len, d), 0);
    CHECK(strcmp(d->ref, "conv:c_demo_alice_1on1") == 0);
    CHECK(d->summary != NULL);
    CHECK_EQ(d->n_points, 3);
    CHECK_EQ(d->n_seg, 0);
    detail_free(d);
    free(js);

    const char *noref = "{\"v\":1,\"title\":\"x\"}";
    CHECK_EQ(detail_parse(noref, strlen(noref), d), FEED_E_FIELD);
    free(d);
}

int main(void)
{
    test_times();
    test_fixture();
    test_tolerance();
    test_details();
    return checks_done("test_feed");
}
