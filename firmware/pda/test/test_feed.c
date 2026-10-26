/* Feed and detail parsing (§4.1, §4.3) against the fictional fixtures, and feed times. */
#include <stdint.h>
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
    /* QC P3: NULL, a dot without digits, offsets out of range or with a bare colon */
    CHECK(!pda_time_parse(NULL, &t, &d));
    CHECK(!pda_time_parse("2026-10-24T12:00:00.Z", &t, &d));
    CHECK(!pda_time_parse("2026-10-24T12:00:00.", &t, &d));
    CHECK(!pda_time_parse("2026-10-24T12:00:00+99:99", &t, &d));
    CHECK(!pda_time_parse("2026-10-24T12:00:00+02:60", &t, &d));
    CHECK(!pda_time_parse("2026-10-24T12:00:00+15:00", &t, &d));
    CHECK(!pda_time_parse("2026-10-24T12:00:00+14:30", &t, &d));
    CHECK(!pda_time_parse("2026-10-24T12:00:00+02:", &t, &d));
    CHECK(pda_time_parse("2026-10-24T12:00:00.5+14:00", &t, &d) && t == 1792843200 - 14 * 3600);
    CHECK(pda_time_parse("2026-10-24T12:00:00-12:00", &t, &d) && t == 1792843200 + 12 * 3600);
    CHECK(pda_time_parse("2026-10-24T12:00:00+02", &t, &d) && t == 1792843200 - 2 * 3600);
    CHECK(pda_time_parse("2026-10-24T12:00:00+0545", &t, &d) && t == 1792843200 - 345 * 60);

    char b[40];
    CHECK_EQ(pda_local_day(1792479600, 120), 20746);
    CHECK_EQ(pda_local_day(1792540800 - 1, 120), 20747); /* 01:59:59 local on the 21st */
    CHECK_EQ(pda_local_day(1792540800 - 7201, 120), 20746); /* 23:59:59 local on the 20th */
    /* QC P4: floor division at and below 0, and at INT64_MIN without overflow */
    CHECK_EQ(pda_local_day(-1, 0), -1);
    CHECK_EQ(pda_local_day(-86400, 0), -1);
    CHECK_EQ(pda_local_day(-86401, 0), -2);
    CHECK_EQ(pda_local_day(0, 0), 0);
    CHECK_EQ(pda_local_day(INT64_MIN, 0), (int32_t)(INT64_MIN / 86400 - 1));
    CHECK_EQ(pda_local_day(INT64_MAX, 0), (int32_t)(INT64_MAX / 86400));
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
    /* todos (§4.4): the hub's order and item indexes kept, null title, due date */
    CHECK(feed.has_todos);
    CHECK_EQ(feed.n_todos, 3);
    CHECK(strcmp(feed.todos[0].id, "note_demo_groceries") == 0 && feed.todos[0].pinned);
    CHECK_EQ(feed.todos[0].n_items, 3);
    CHECK_EQ(feed.todos[0].items[2].i, 3); /* not renumbered */
    CHECK(strcmp(feed.todos[0].items[1].text, "Bergk\xc3\xa4se f\xc3\xbcr Freitag") == 0);
    CHECK(feed.todos[0].items[1].done && !feed.todos[0].items[0].done);
    CHECK_EQ(feed.todos[0].due_day, FEED_NO_DAY);
    CHECK(strcmp(feed.todos[1].due, "2026-11-08") == 0);
    CHECK(feed.todos[2].title == NULL);
    CHECK_EQ(feed.n_templates, 4);
    CHECK(strcmp(feed.templates[0], "Call") == 0);
    /* no `insight` key: nothing from it */
    CHECK(!feed.has_insight);
    CHECK(feed.insight_headline == NULL && feed.insight_date[0] == 0);
    for (int s = 0; s < FEED_INSIGHT_SECTIONS; s++) {
        CHECK_EQ(feed.insight[s].n, 0);
    }
    feed_free(&feed);
}

/* The index of a todo note by id, or -1. */
static int note_idx(const feed_t *f, const char *id)
{
    for (int n = 0; n < f->n_todos; n++) {
        if (strcmp(f->todos[n].id, id) == 0) {
            return n;
        }
    }
    return -1;
}

static const char *event_id(const feed_t *f, const feed_insight_item_t *it)
{
    return it->kind == FEED_REF_EVENT ? f->events[it->event].id : "";
}

/* `insight` (§4.1) in the fictional fixture: refs resolved to indexes, the bad ones gone. */
static void test_insight_fixture(void)
{
    size_t len;
    char *js = load("feed_insight_demo.json", &len);
    CHECK_EQ(feed_parse(js, len, &feed), 0);
    free(js);
    CHECK(feed.has_insight);
    CHECK(strcmp(feed.insight_date, "2026-10-20") == 0);
    CHECK_EQ(feed.insight_day, 20746);
    CHECK(feed.insight_headline && strstr(feed.insight_headline, "Friday\xe2\x80\x99s client demo") == feed.insight_headline);
    CHECK_EQ(feed.n_events, 9);
    CHECK_EQ(feed.n_todos, 7);

    /* focus: the unresolvable note ref is dropped; the past event is still there (the
     * plan drops it, not the parser) */
    const feed_insight_list_t *fo = &feed.insight[FEED_INSIGHT_FOCUS];
    CHECK_EQ(fo->n, 4);
    const int work = note_idx(&feed, "note_demo_work");
    const int demo = note_idx(&feed, "note_demo_demo");
    CHECK(fo->items[0].kind == FEED_REF_TODO && fo->items[0].note == work && fo->items[0].item == 0);
    CHECK(strcmp(feed.todos[work].items[fo->items[0].item].text, "Send the Q4 budget sheet to Alice") == 0);
    CHECK(strcmp(fo->items[0].why, "a day late; Alice needs it for the Q4 plan") == 0);
    CHECK(fo->items[1].kind == FEED_REF_TODO && fo->items[1].note == demo);
    CHECK(fo->items[2].kind == FEED_REF_EVENT && strcmp(event_id(&feed, &fo->items[2]), "ev_demo_lunch") == 0);
    CHECK(strcmp(feed.events[fo->items[2].event].title, "Lunch with Maria") == 0);
    CHECK(strcmp(event_id(&feed, &fo->items[3]), "ev_demo_call_tom") == 0);

    /* coming up: a missing `why` is NULL; the todo ref uses the hub's `i` */
    const feed_insight_list_t *cu = &feed.insight[FEED_INSIGHT_COMING_UP];
    CHECK_EQ(cu->n, 3);
    CHECK(strcmp(event_id(&feed, &cu->items[0]), "ev_demo_2") == 0);
    CHECK(strcmp(event_id(&feed, &cu->items[1]), "ev_demo_3") == 0);
    const int mom = note_idx(&feed, "note_demo_mom70");
    CHECK(cu->items[2].kind == FEED_REF_TODO && cu->items[2].note == mom && cu->items[2].item == 1);
    CHECK(cu->items[2].why == NULL);

    /* also: an object, not a list: ignored */
    CHECK_EQ(feed.insight[FEED_INSIGHT_ALSO].n, 0);
    feed_free(&feed);
}

/* `weights` (§4.1, §5): only list: keys with integers, clamped, 32 at most, repeats once;
 * a bad table or version is ignored, never fatal. */
static void test_weights(void)
{
    char id150[151], id151[152], js[8192];
    memset(id150, 'a', 150);
    id150[150] = 0;
    memset(id151, 'b', 151);
    id151[151] = 0;
    snprintf(js, sizeof(js),
             "{\"v\":1,\"version\":1,\"weights\":{\"version\":1,\"date\":\"2026-10-20\",\"weights\":{"
             "\"list:n1\":2,\"list:n2\":-1,\"list:big\":9,\"list:small\":-12,\"list:huge\":99999999999999,"
             "\"list:frac\":1.5,\"list:exp\":1e1,\"list:str\":\"3\",\"list:t\":true,\"list:nul\":null,"
             "\"list:obj\":{\"w\":1},\"note_type:checklist\":2,\"event_type:work\":1,\"importance:high\":1,"
             "\"LIST:n3\":1,\"list:\":4,\"list:%s\":3,\"list:%s\":3,\"list:n1\":5,\"list:a:b\":-2,"
             "\"list:\\u00fc\":1}}}",
             id150, id151);
    CHECK_EQ(feed_parse(js, strlen(js), &feed), 0);
    CHECK_EQ(feed.n_weights, 8);
    CHECK_EQ(feed_note_weight(&feed, "n1"), 2); /* the first n1, not the repeat */
    CHECK_EQ(feed_note_weight(&feed, "n2"), -1);
    CHECK_EQ(feed_note_weight(&feed, "big"), 6);
    CHECK_EQ(feed_note_weight(&feed, "small"), -6);
    CHECK_EQ(feed_note_weight(&feed, "huge"), 6);
    CHECK_EQ(feed_note_weight(&feed, id150), 3);
    CHECK_EQ(feed_note_weight(&feed, id151), 0);
    CHECK_EQ(feed_note_weight(&feed, "a:b"), -2);
    CHECK_EQ(feed_note_weight(&feed, "\xc3\xbc"), 1);
    CHECK_EQ(feed_note_weight(&feed, "frac"), 0);
    CHECK_EQ(feed_note_weight(&feed, "str"), 0);
    CHECK_EQ(feed_note_weight(&feed, "checklist"), 0);
    CHECK_EQ(feed_note_weight(&feed, "n3"), 0);
    CHECK_EQ(feed_note_weight(&feed, ""), 0);
    CHECK_EQ(feed_note_weight(&feed, NULL), 0);
    feed_free(&feed);

    /* at most 32 */
    size_t n = (size_t)snprintf(js, sizeof(js), "{\"v\":1,\"version\":1,\"weights\":{\"weights\":{");
    for (int i = 0; i < 40; i++) {
        n += (size_t)snprintf(js + n, sizeof(js) - n, "%s\"list:n%d\":%d", i ? "," : "", i, i % 13 - 6);
    }
    snprintf(js + n, sizeof(js) - n, "}}}");
    CHECK_EQ(feed_parse(js, strlen(js), &feed), 0);
    CHECK_EQ(feed.n_weights, FEED_MAX_WEIGHTS);
    CHECK_EQ(feed_note_weight(&feed, "n31"), 31 % 13 - 6);
    CHECK_EQ(feed_note_weight(&feed, "n32"), 0);
    feed_free(&feed);

    /* ignored: another version, weights not an object, no weights key at all */
    static const char *const bad[] = {
        "{\"v\":1,\"version\":1,\"weights\":{\"version\":2,\"weights\":{\"list:n1\":2}}}",
        "{\"v\":1,\"version\":1,\"weights\":{\"version\":\"1\",\"weights\":{\"list:n1\":2}}}",
        "{\"v\":1,\"version\":1,\"weights\":{\"weights\":[\"list:n1\",2]}}",
        "{\"v\":1,\"version\":1,\"weights\":[{\"list:n1\":2}]}",
        "{\"v\":1,\"version\":1,\"weights\":{\"list:n1\":2}}",
        "{\"v\":1,\"version\":1}",
    };
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        CHECK_EQ(feed_parse(bad[i], strlen(bad[i]), &feed), 0);
        CHECK_EQ(feed.n_weights, 0);
        CHECK_EQ(feed_note_weight(&feed, "n1"), 0);
        feed_free(&feed);
    }
}

static void test_insight_tolerance(void)
{
    /* refs by the hub's `i` (with gaps), note ids with ':', caps, bad refs and items */
    const char *js =
        "{\"v\":1,\"version\":1,"
        "\"calendar\":[{\"id\":\"b\",\"title\":\"B\",\"start\":\"2026-10-20T12:00:00Z\"},"
        "{\"id\":\"a\",\"title\":\"A\",\"start\":\"2026-10-20T08:00:00Z\"},"
        "{\"title\":\"no id\",\"start\":\"2026-10-20T09:00:00Z\"}],"
        "\"todos\":[{\"id\":\"n:1\",\"items\":[{\"i\":0,\"text\":\"x\"},{\"i\":7,\"text\":\"y\"}]}],"
        "\"insight\":{\"date\":\"2026-10-20\",\"headline\":\"\",\"extra\":[1,{\"a\":2}],"
        "\"focus\":[{\"ref\":\"todo:n:1:7\",\"why\":\"\"},{\"ref\":\"todo:n:1:1\"},{\"ref\":\"todo:n:1:\"},"
        "{\"ref\":\"todo:n:1:x7\"},{\"ref\":\"todo::0\"},{\"ref\":\"todo:n:1:-7\"},"
        "{\"ref\":\"todo:n:1:99999999999999\"},{\"ref\":\"event:\"},{\"ref\":\"event:zz\"},"
        "{\"ref\":7},\"event:a\",{\"why\":\"no ref\"},{\"ref\":\"note:n:1:0\"},"
        "{\"ref\":\"event:a\",\"why\":\"early\",\"new\":true}],"
        "\"coming_up\":[{\"ref\":\"event:b\"},{\"ref\":\"event:b\"},{\"ref\":\"event:b\"},"
        "{\"ref\":\"event:b\"},{\"ref\":\"event:b\"},{\"ref\":\"event:a\"},{\"ref\":\"event:a\"}],"
        "\"also\":\"not a list\"}}";
    CHECK_EQ(feed_parse(js, strlen(js), &feed), 0);
    CHECK(feed.has_insight && feed.insight_headline == NULL);
    const feed_insight_list_t *fo = &feed.insight[FEED_INSIGHT_FOCUS];
    CHECK_EQ(fo->n, 2);
    CHECK(fo->items[0].kind == FEED_REF_TODO && fo->items[0].note == 0 && fo->items[0].item == 1);
    CHECK(fo->items[0].why == NULL); /* "" reads as missing */
    /* events are sorted before refs resolve: "a" (08:00) is index 0 */
    CHECK(fo->items[1].kind == FEED_REF_EVENT && fo->items[1].event == 0);
    CHECK(strcmp(fo->items[1].why, "early") == 0);
    /* at most 5 per section */
    const feed_insight_list_t *cu = &feed.insight[FEED_INSIGHT_COMING_UP];
    CHECK_EQ(cu->n, FEED_MAX_INSIGHT_ITEMS);
    for (int i = 0; i < cu->n; i++) {
        CHECK(cu->items[i].kind == FEED_REF_EVENT && strcmp(feed.events[cu->items[i].event].id, "b") == 0);
    }
    CHECK_EQ(feed.insight[FEED_INSIGHT_ALSO].n, 0);
    /* the rest of the feed as without the key */
    CHECK_EQ(feed.n_events, 3);
    CHECK(feed.has_todos && feed.n_todos == 1 && feed.todos[0].n_items == 2);
    feed_free(&feed);

    /* no usable date: no insight (the T5 couldn't tell whether it is today's) */
    const char *bad[] = {
        "{\"v\":1,\"version\":1,\"insight\":{\"headline\":\"h\"}}",
        "{\"v\":1,\"version\":1,\"insight\":{\"date\":\"2026-10-20T00:00:00Z\",\"headline\":\"h\"}}",
        "{\"v\":1,\"version\":1,\"insight\":{\"date\":\"2026-13-20\"}}",
        "{\"v\":1,\"version\":1,\"insight\":{\"date\":20261020}}",
        "{\"v\":1,\"version\":1,\"insight\":[{\"date\":\"2026-10-20\"}]}",
        "{\"v\":1,\"version\":1,\"insight\":null}",
    };
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        CHECK_EQ(feed_parse(bad[i], strlen(bad[i]), &feed), 0);
        CHECK(!feed.has_insight);
        CHECK(feed.insight_headline == NULL);
        feed_free(&feed);
    }

    /* refs into an empty feed resolve to nothing, without a crash */
    const char *empty = "{\"v\":1,\"version\":1,\"insight\":{\"date\":\"2026-10-20\",\"headline\":\"Quiet day\","
                        "\"focus\":[{\"ref\":\"todo:n:0\"},{\"ref\":\"event:a\"}]}}";
    CHECK_EQ(feed_parse(empty, strlen(empty), &feed), 0);
    CHECK(feed.has_insight && strcmp(feed.insight_headline, "Quiet day") == 0);
    CHECK_EQ(feed.insight[FEED_INSIGHT_FOCUS].n, 0);
    feed_free(&feed);
}

static void test_tolerance(void)
{
    /* unknown keys, items missing required fields, wrong types: skipped, not fatal */
    const char *js =
        "{\"v\":1,\"version\":5,\"extra\":{\"x\":[1,2]},"
        "\"conversations\":[{\"title\":\"no id\"},{\"id\":\"c1\",\"title\":\"ok\",\"new\":1},7],"
        "\"actions\":[{\"id\":\"42\",\"text\":\"string id\"},{\"id\":3,\"text\":\"ok\",\"due\":\"bad\","
        "\"owner\":\"\"}],"
        "\"calendar\":[{\"title\":\"no start\"},{\"title\":\"t\",\"start\":\"2026-10-20T10:00:00Z\"}]}";
    CHECK_EQ(feed_parse(js, strlen(js), &feed), 0);
    CHECK_EQ(feed.version, 5u);
    CHECK(!feed.has_briefing);
    CHECK(!feed.has_todos); /* no key: Odysseus not set up */
    CHECK_EQ(feed.n_conv, 1);
    CHECK_EQ(feed.conv[0].at, 0);
    CHECK_EQ(feed.n_actions, 1);
    CHECK_EQ(feed.actions[0].due_day, FEED_NO_DAY);
    CHECK(feed.actions[0].owner == NULL);
    CHECK_EQ(feed.n_events, 1);
    feed_free(&feed);

    /* todos: bad notes and items are skipped; an empty list is "set up, nothing there" */
    const char *td = "{\"v\":1,\"version\":1,\"todos\":[{\"title\":\"no id\"},"
                     "{\"id\":\"n1\",\"title\":\"\",\"items\":[{\"i\":-1,\"text\":\"neg\"},"
                     "{\"text\":\"no index\"},{\"i\":5,\"text\":\"ok\"},{\"i\":6}]}]}";
    CHECK_EQ(feed_parse(td, strlen(td), &feed), 0);
    CHECK(feed.has_todos && feed.n_todos == 1);
    CHECK(feed.todos[0].title == NULL && feed.todos[0].n_items == 1);
    CHECK(feed.todos[0].items[0].i == 5 && !feed.todos[0].items[0].done);
    feed_free(&feed);
    const char *te = "{\"v\":1,\"version\":1,\"todos\":[]}";
    CHECK_EQ(feed_parse(te, strlen(te), &feed), 0);
    CHECK(feed.has_todos && feed.n_todos == 0);
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
    CHECK(d->from == 0 && d->to == 600);
    CHECK(d->next == NULL);
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
    CHECK_EQ(d->n_people, 1);
    CHECK_EQ(d->at, 1791990000);
    CHECK_EQ(d->n_actions, 1);
    CHECK(d->actions[0].id == 17 && strcmp(d->actions[0].status, "open") == 0);
    CHECK(d->actions[0].due == NULL); /* "" reads as missing */
    detail_free(d);
    free(js);

    const char *paged = "{\"v\":1,\"ref\":\"transcript:c@0-600\",\"from\":0,\"to\":600,"
                        "\"next\":\"transcript:c@600-1200\",\"segments\":[]}";
    CHECK_EQ(detail_parse(paged, strlen(paged), d), 0);
    CHECK(d->next && strcmp(d->next, "transcript:c@600-1200") == 0);
    detail_free(d);
    const char *noref = "{\"v\":1,\"title\":\"x\"}";
    CHECK_EQ(detail_parse(noref, strlen(noref), d), FEED_E_FIELD);
    free(d);
}

int main(void)
{
    test_times();
    test_fixture();
    test_insight_fixture();
    test_insight_tolerance();
    test_weights();
    test_tolerance();
    test_details();
    return checks_done("test_feed");
}
