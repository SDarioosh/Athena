/* The Today tab's plan (§5): the rules without an insight, the insight mapped onto the same
 * sections, against the fictional fixtures and small inline feeds. */
#include <stdlib.h>
#include <string.h>

#include "check.h"
#include "feed.h"
#include "pda_time.h"
#include "today_plan.h"

#define NOW 1792479600 /* 2026-10-20 07:00 UTC, 09:00 in Berlin (the fixtures' `generated`) */
#define TZ 120
#define TODAY 20746 /* Tue 20 Oct 2026 */

static feed_t feed;
static today_plan_t plan;

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

static void parse_fixture(const char *name)
{
    size_t len;
    char *js = load(name, &len);
    CHECK_EQ(feed_parse(js, len, &feed), 0);
    free(js);
}

static void parse(const char *js)
{
    CHECK_EQ(feed_parse(js, strlen(js), &feed), 0);
}

/* What an item names: the to-do text or the event title. */
static const char *text(const today_item_t *it)
{
    if (it->ref.kind == FEED_REF_TODO) {
        return feed.todos[it->ref.note].items[it->ref.item].text;
    }
    return feed.events[it->ref.event].title;
}

/* Section s holds exactly these items (texts), in this order. */
static void check_sec(int line, int s, const char *const *want, int n)
{
    const today_section_t *sec = &plan.sec[s];
    if (sec->n != n) {
        fprintf(stderr, "line %d: section %d has %d items, want %d\n", line, s, sec->n, n);
        for (int i = 0; i < sec->n; i++) {
            fprintf(stderr, "   %s (%s)\n", text(&sec->items[i]), sec->items[i].reason);
        }
        checks_failed++;
        return;
    }
    for (int i = 0; i < n; i++) {
        if (strcmp(text(&sec->items[i]), want[i]) != 0) {
            fprintf(stderr, "line %d: section %d item %d is \"%s\", want \"%s\"\n", line, s, i,
                    text(&sec->items[i]), want[i]);
            checks_failed++;
        }
    }
}
#define SEC(s, ...)                                                                         \
    do {                                                                                    \
        static const char *const w_[] = {__VA_ARGS__};                                      \
        check_sec(__LINE__, s, w_, (int)(sizeof(w_) / sizeof(w_[0])));                     \
    } while (0)
#define SEC_EMPTY(s) check_sec(__LINE__, s, NULL, 0)
#define REASON(s, i, r) CHECK(strcmp(plan.sec[s].items[i].reason, r) == 0)

static void test_shopping(void)
{
    CHECK(today_is_shopping_list("Shopping"));
    CHECK(today_is_shopping_list("groceries"));
    CHECK(today_is_shopping_list("Grocery List"));
    CHECK(today_is_shopping_list("  GROCERY list "));
    CHECK(today_is_shopping_list("Shopping  list"));
    CHECK(!today_is_shopping_list("Shopping lists"));
    CHECK(!today_is_shopping_list("My shopping"));
    CHECK(!today_is_shopping_list("Groceries for Friday"));
    CHECK(!today_is_shopping_list("list"));
    CHECK(!today_is_shopping_list(" list"));
    CHECK(!today_is_shopping_list(""));
    CHECK(!today_is_shopping_list(NULL));
}

static void test_event_when(void)
{
    const char *js = "{\"v\":1,\"version\":1,\"calendar\":["
                     "{\"id\":\"a\",\"title\":\"A\",\"start\":\"2026-10-20T12:00:00Z\"},"
                     "{\"id\":\"b\",\"title\":\"B\",\"start\":\"2026-10-20\",\"all_day\":true},"
                     "{\"id\":\"c\",\"title\":\"C\",\"start\":\"2026-10-23T07:05:00Z\"},"
                     "{\"id\":\"d\",\"title\":\"D\",\"start\":\"2026-10-25\"},"
                     "{\"id\":\"e\",\"title\":\"E\",\"start\":\"2026-10-20T22:30:00Z\"}]}";
    parse(js);
    char b[40];
    /* sorted: B (all-day 20th), A, E (00:30 on the 21st locally), C, D */
    today_event_when(&feed.events[0], TODAY, TZ, b, sizeof(b));
    CHECK(strcmp(b, "today, all day") == 0);
    today_event_when(&feed.events[1], TODAY, TZ, b, sizeof(b));
    CHECK(strcmp(b, "today 14:00") == 0);
    today_event_when(&feed.events[2], TODAY, TZ, b, sizeof(b));
    CHECK(strcmp(b, "Wed 21 Oct 00:30") == 0);
    today_event_when(&feed.events[3], TODAY, TZ, b, sizeof(b));
    CHECK(strcmp(b, "Fri 23 Oct 09:05") == 0);
    today_event_when(&feed.events[4], TODAY, TZ, b, sizeof(b));
    CHECK(strcmp(b, "Sun 25 Oct") == 0);
    CHECK_EQ(today_event_day(&feed.events[2], TZ), TODAY + 1);
    CHECK_EQ(today_event_day(&feed.events[2], 0), TODAY);
    /* over: timed without an end once it has started; all-day only from the next day */
    CHECK(!today_event_over(&feed.events[1], TODAY, 1792497600));
    CHECK(today_event_over(&feed.events[1], TODAY, 1792497600 + 1));
    CHECK(!today_event_over(&feed.events[0], TODAY, NOW + 86400));
    CHECK(today_event_over(&feed.events[0], TODAY + 1, NOW + 86400));
    feed_free(&feed);
}

/* The old demo feed (no insight) at 09:00 on Tuesday. */
static void test_rules_demo(void)
{
    parse_fixture("feed_demo.json");
    CHECK(!today_plan_from_insight(&feed, TODAY, NOW, TZ, &plan));
    today_plan_for_today(&feed, TODAY, NOW, TZ, &plan);
    CHECK(!plan.from_insight && plan.headline == NULL);
    SEC(TODAY_DO_FIRST, "Team stand-up");
    REASON(TODAY_DO_FIRST, 0, "today 09:30");
    SEC(TODAY_COMING_UP, "Dentist follow-up (Dr. Patel)", "Ben\xe2\x80\x99s PR review session", "Client demo",
        "Jonas: long run (cheer at km 20)", "Lease renewal reminder");
    REASON(TODAY_COMING_UP, 0, "Wed 21 Oct 09:00");
    REASON(TODAY_COMING_UP, 1, "Thu 22 Oct 15:00");
    REASON(TODAY_COMING_UP, 3, "Sun 25 Oct");
    CHECK_EQ(plan.sec[TODAY_COMING_UP].more, 0);
    /* Groceries is pinned but a shopping list without a date; Mom's 70th is due in 19
     * days and not pinned: neither is on Today */
    SEC(TODAY_ALSO, "Return the library book");
    REASON(TODAY_ALSO, 0, "no date");
    for (int s = 0; s < TODAY_SECTIONS; s++) {
        for (int i = 0; i < plan.sec[s].n; i++) {
            CHECK(plan.sec[s].items[i].ref.why == NULL);
        }
    }

    /* an hour later the stand-up (until 09:45) is over */
    today_plan_build(&feed, TODAY, NOW + 3600, TZ, &plan);
    SEC_EMPTY(TODAY_DO_FIRST);
    /* a day later: the dentist is today, the rest still coming up */
    today_plan_build(&feed, TODAY + 1, NOW + 86400, TZ, &plan);
    SEC(TODAY_DO_FIRST, "Dentist follow-up (Dr. Patel)");
    SEC(TODAY_COMING_UP, "Ben\xe2\x80\x99s PR review session", "Client demo", "Jonas: long run (cheer at km 20)",
        "Lease renewal reminder");
    feed_free(&feed);
}

/* The insight fixture, without using the insight: the rules and their caps. */
static void test_rules_insight_fixture(void)
{
    parse_fixture("feed_insight_demo.json");
    today_plan_build(&feed, TODAY, NOW, TZ, &plan);
    CHECK(!plan.from_insight);
    /* overdue (Work, due yesterday), due today (Client demo prep), then today's events;
     * the 08:00 call is over; capped at 5, lunch and the 1:1 counted in `more` */
    SEC(TODAY_DO_FIRST, "Send the Q4 budget sheet to Alice", "Book the Berlin trip for January",
        "Rehearse the demo end to end", "Check database load with Ben\xe2\x80\x99s new queries", "Team stand-up");
    REASON(TODAY_DO_FIRST, 0, "overdue since 2026-10-19");
    REASON(TODAY_DO_FIRST, 2, "due today");
    REASON(TODAY_DO_FIRST, 4, "today 09:30");
    CHECK_EQ(plan.sec[TODAY_DO_FIRST].more, 2);
    SEC(TODAY_COMING_UP, "Dentist follow-up (Dr. Patel)", "Ben\xe2\x80\x99s PR review session", "Client demo",
        "Jonas: long run (cheer at km 20)", "Lease renewal reminder");
    /* pinned House first (pinned Groceries is a shopping list), then undated; the
     * "Shopping list" note is left out; the done expense report too */
    SEC(TODAY_ALSO, "Ask Tom when he fixes the bathroom fan", "Water the plants", "Return the library book");
    REASON(TODAY_ALSO, 0, "pinned");
    REASON(TODAY_ALSO, 2, "no date");

    /* the insight is dated the 20th: not used on the 21st */
    CHECK(!today_plan_from_insight(&feed, TODAY + 1, NOW + 86400, TZ, &plan));
    today_plan_for_today(&feed, TODAY + 1, NOW + 86400, TZ, &plan);
    CHECK(!plan.from_insight && plan.headline == NULL);
    feed_free(&feed);
}

/* The insight fixture as the Today tab shows it. */
static void test_insight(void)
{
    parse_fixture("feed_insight_demo.json");
    today_plan_for_today(&feed, TODAY, NOW, TZ, &plan);
    CHECK(plan.from_insight);
    CHECK(plan.headline == feed.insight_headline && plan.headline != NULL);
    /* the hub's two to-dos, then today's timed events still to come in time order: the
     * stand-up (not named by the hub), lunch (named, with its why), the 1:1; the 08:00 call
     * the hub named is over and dropped */
    SEC(TODAY_DO_FIRST, "Send the Q4 budget sheet to Alice", "Rehearse the demo end to end", "Team stand-up",
        "Lunch with Maria", "1:1 with Alice");
    const today_section_t *d = &plan.sec[TODAY_DO_FIRST];
    CHECK(strcmp(d->items[0].ref.why, "a day late; Alice needs it for the Q4 plan") == 0);
    REASON(TODAY_DO_FIRST, 0, "");
    CHECK(d->items[2].ref.why == NULL);
    REASON(TODAY_DO_FIRST, 2, "today 09:30");
    CHECK(strcmp(d->items[3].ref.why, "Maria wants the seating plan settled") == 0);
    REASON(TODAY_DO_FIRST, 3, "today 12:30");
    REASON(TODAY_DO_FIRST, 4, "today 16:00");
    CHECK_EQ(d->more, 0);
    /* coming up in the hub's order, events with their time, the item without a why */
    SEC(TODAY_COMING_UP, "Dentist follow-up (Dr. Patel)", "Client demo",
        "Pick up Aunt Rosa from the station (afternoon)");
    REASON(TODAY_COMING_UP, 0, "Wed 21 Oct 09:00");
    CHECK(strcmp(plan.sec[TODAY_COMING_UP].items[0].ref.why, "bring the insurance card") == 0);
    REASON(TODAY_COMING_UP, 2, "");
    CHECK(plan.sec[TODAY_COMING_UP].items[2].ref.why == NULL);
    /* "also" wasn't a list */
    SEC_EMPTY(TODAY_ALSO);

    /* at 13:00 lunch (until 13:30 local) is still on; at 14:00 it is gone */
    today_plan_for_today(&feed, TODAY, NOW + 4 * 3600, TZ, &plan);
    SEC(TODAY_DO_FIRST, "Send the Q4 budget sheet to Alice", "Rehearse the demo end to end", "Lunch with Maria",
        "1:1 with Alice");
    today_plan_for_today(&feed, TODAY, NOW + 5 * 3600, TZ, &plan);
    SEC(TODAY_DO_FIRST, "Send the Q4 budget sheet to Alice", "Rehearse the demo end to end", "1:1 with Alice");
    feed_free(&feed);
}

/* Inline feeds: repeats, done items, events moved to DO FIRST, all-day events, room. */
static void test_insight_edges(void)
{
    const char *js =
        "{\"v\":1,\"version\":1,"
        "\"calendar\":["
        "{\"id\":\"t1\",\"title\":\"Today 11\",\"start\":\"2026-10-20T09:00:00Z\",\"end\":\"2026-10-20T10:00:00Z\"},"
        "{\"id\":\"t2\",\"title\":\"Today 15\",\"start\":\"2026-10-20T13:00:00Z\"},"
        "{\"id\":\"t3\",\"title\":\"Today 17\",\"start\":\"2026-10-20T15:00:00Z\"},"
        "{\"id\":\"t4\",\"title\":\"Today 18\",\"start\":\"2026-10-20T16:00:00Z\"},"
        "{\"id\":\"ad\",\"title\":\"Birthday\",\"start\":\"2026-10-20\",\"all_day\":true},"
        "{\"id\":\"old\",\"title\":\"Yesterday\",\"start\":\"2026-10-19\",\"all_day\":true}],"
        "\"todos\":[{\"id\":\"n\",\"title\":\"Groceries\",\"items\":["
        "{\"i\":0,\"text\":\"a\"},{\"i\":1,\"text\":\"b\",\"done\":true},{\"i\":2,\"text\":\"c\"},"
        "{\"i\":3,\"text\":\"d\"},{\"i\":4,\"text\":\"e\"},{\"i\":5,\"text\":\"f\"}]}],"
        "\"insight\":{\"date\":\"2026-10-20\","
        "\"focus\":[{\"ref\":\"todo:n:0\",\"why\":\"w0\"},{\"ref\":\"todo:n:1\"},{\"ref\":\"todo:n:2\"},"
        "{\"ref\":\"todo:n:3\"},{\"ref\":\"todo:n:4\"}],"
        "\"coming_up\":[{\"ref\":\"event:t3\",\"why\":\"moved\"},{\"ref\":\"event:ad\",\"why\":\"cake\"},"
        "{\"ref\":\"event:old\"},{\"ref\":\"todo:n:0\",\"why\":\"again\"}],"
        "\"also\":[{\"ref\":\"todo:n:5\"},{\"ref\":\"event:t3\",\"why\":\"again\"}]}}";
    parse(js);
    CHECK(today_plan_from_insight(&feed, TODAY, NOW, TZ, &plan));
    /* the done item is dropped; the hub's items keep their place even in a shopping list
     * (the hub chose them); today's 4 timed events follow, up to the section's room of 8 */
    SEC(TODAY_DO_FIRST, "a", "c", "d", "e", "Today 11", "Today 15", "Today 17", "Today 18");
    CHECK_EQ(plan.sec[TODAY_DO_FIRST].more, 0);
    CHECK(strcmp(plan.sec[TODAY_DO_FIRST].items[0].ref.why, "w0") == 0);
    CHECK(strcmp(plan.sec[TODAY_DO_FIRST].items[6].ref.why, "moved") == 0); /* from coming_up */
    CHECK(plan.sec[TODAY_DO_FIRST].items[5].ref.why == NULL);
    /* the all-day event of today stays where the hub put it; yesterday's is over; repeats go */
    SEC(TODAY_COMING_UP, "Birthday");
    REASON(TODAY_COMING_UP, 0, "today, all day");
    SEC(TODAY_ALSO, "f");
    CHECK(plan.headline == NULL);

    /* a fifth hub to-do: the section is full at 8 and counts the rest */
    feed.insight[FEED_INSIGHT_FOCUS].items[1] = feed.insight[FEED_INSIGHT_ALSO].items[0]; /* f instead of done b */
    today_plan_from_insight(&feed, TODAY, NOW, TZ, &plan);
    SEC(TODAY_DO_FIRST, "a", "f", "c", "d", "e", "Today 11", "Today 15", "Today 17");
    CHECK_EQ(plan.sec[TODAY_DO_FIRST].more, 1);
    SEC_EMPTY(TODAY_ALSO); /* f was already shown */

    /* the rules on the same feed: the shopping list without a date is left out */
    today_plan_build(&feed, TODAY, NOW, TZ, &plan);
    SEC(TODAY_DO_FIRST, "Birthday", "Today 11", "Today 15", "Today 17", "Today 18");
    REASON(TODAY_DO_FIRST, 0, "today, all day");
    SEC_EMPTY(TODAY_ALSO);
    feed_free(&feed);

    /* a shopping list with a due date is on Today; overdue lists, oldest first */
    const char *due = "{\"v\":1,\"version\":1,\"todos\":["
                      "{\"id\":\"s\",\"title\":\"Shopping list\",\"due\":\"2026-10-20\",\"items\":[{\"i\":0,\"text\":\"milk\"}]},"
                      "{\"id\":\"x\",\"title\":\"X\",\"due\":\"2026-10-19\",\"items\":[{\"i\":0,\"text\":\"x1\"}]},"
                      "{\"id\":\"y\",\"title\":\"Y\",\"due\":\"2026-10-12\",\"items\":[{\"i\":0,\"text\":\"y1\"},"
                      "{\"i\":1,\"text\":\"y2\"}]},"
                      "{\"id\":\"z\",\"title\":\"Z\",\"due\":\"2026-10-27\",\"items\":[{\"i\":0,\"text\":\"z1\"}]},"
                      "{\"id\":\"far\",\"title\":\"Far\",\"due\":\"2026-10-28\",\"items\":[{\"i\":0,\"text\":\"far1\"}]},"
                      "{\"id\":\"pf\",\"title\":\"Pinned far\",\"pinned\":true,\"due\":\"2026-12-01\","
                      "\"items\":[{\"i\":0,\"text\":\"pf1\"}]}]}";
    parse(due);
    today_plan_build(&feed, TODAY, NOW, TZ, &plan);
    SEC(TODAY_DO_FIRST, "y1", "y2", "x1", "milk");
    REASON(TODAY_DO_FIRST, 0, "overdue since 2026-10-12");
    REASON(TODAY_DO_FIRST, 3, "due today");
    SEC(TODAY_COMING_UP, "z1"); /* today + 7; the 28th is too far */
    REASON(TODAY_COMING_UP, 0, "due Tue 27 Oct");
    SEC(TODAY_ALSO, "pf1");
    REASON(TODAY_ALSO, 0, "pinned");
    feed_free(&feed);

    /* an empty feed: empty sections */
    parse("{\"v\":1,\"version\":1}");
    today_plan_for_today(&feed, TODAY, NOW, TZ, &plan);
    CHECK(!plan.from_insight);
    for (int s = 0; s < TODAY_SECTIONS; s++) {
        CHECK_EQ(plan.sec[s].n + plan.sec[s].more, 0);
    }
    feed_free(&feed);
}

/* Many items: the cap keeps the first 5 by key and counts the rest. */
static void test_caps(void)
{
    char js[8192];
    size_t n = (size_t)snprintf(js, sizeof(js), "{\"v\":1,\"version\":1,\"calendar\":[");
    for (int d = 1; d <= 9; d++) {
        n += (size_t)snprintf(js + n, sizeof(js) - n, "%s{\"id\":\"e%d\",\"title\":\"E%d\",\"start\":\"2026-10-%02dT08:00:00Z\"}",
                              d > 1 ? "," : "", d, d, 20 + d);
    }
    n += (size_t)snprintf(js + n, sizeof(js) - n, "],\"todos\":[{\"id\":\"u\",\"title\":\"U\",\"items\":[");
    for (int i = 0; i < 12; i++) {
        n += (size_t)snprintf(js + n, sizeof(js) - n, "%s{\"i\":%d,\"text\":\"u%d\"}", i ? "," : "", i, i);
    }
    snprintf(js + n, sizeof(js) - n, "]}]}");
    parse(js);
    today_plan_build(&feed, TODAY, NOW, TZ, &plan);
    /* events on the 21st..27th are within 7 days (7 of them), capped at 5 */
    SEC(TODAY_COMING_UP, "E1", "E2", "E3", "E4", "E5");
    CHECK_EQ(plan.sec[TODAY_COMING_UP].more, 2);
    SEC(TODAY_ALSO, "u0", "u1", "u2", "u3", "u4");
    CHECK_EQ(plan.sec[TODAY_ALSO].more, 7);
    feed_free(&feed);
}

int main(void)
{
    test_shopping();
    test_event_when();
    test_rules_demo();
    test_rules_insight_fixture();
    test_insight();
    test_insight_edges();
    test_caps();
    return checks_done("test_today_plan");
}
