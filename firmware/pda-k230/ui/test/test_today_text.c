/* today_text: the T5's second-line rules (ui_today.c) on the demo feeds. */
#include "check.h"
#include "pda_time.h"
#include "test_util.h"
#include "today_plan.h"
#include "today_text.h"

static const today_item_t *find(const today_plan_t *p, const feed_t *f, const char *title)
{
    for (int s = 0; s < TODAY_SECTIONS; s++) {
        for (int i = 0; i < p->sec[s].n; i++) {
            const today_item_t *it = &p->sec[s].items[i];
            const char *t = it->ref.kind == FEED_REF_TODO ? f->todos[it->ref.note].items[it->ref.item].text
                                                         : f->events[it->ref.event].title;
            if (strcmp(t, title) == 0) {
                return it;
            }
        }
    }
    fprintf(stderr, "not on Today: %s\n", title);
    return NULL;
}

static void line_is(const feed_t *f, const today_plan_t *p, const char *title, const char *want)
{
    const today_item_t *it = find(p, f, title);
    CHECK(it != NULL);
    if (!it) {
        return;
    }
    char got[TODAY_LINE_MAX];
    today_second_line(f, it, false, DEMO_TZ, got, sizeof(got));
    if (strcmp(got, want) != 0) {
        fprintf(stderr, "%s: got \"%s\", want \"%s\"\n", title, got, want);
        checks_failed++;
    }
}

int main(void)
{
    CHECK(today_restates("Film demo video for the RokTalk meeting", "Film a demo video for the meeting"));
    CHECK(today_restates("Baba's Birthday on 11/01", "Baba's Birthday"));
    CHECK(!today_restates("bring the insurance card", "Dentist follow-up (Dr. Patel)"));
    CHECK(today_repeats("Milk", "milk"));
    CHECK(today_repeats("Photo album: pick 40 old\xe2\x80\xa6", "Photo album: pick 40 old family pictures"));
    CHECK(!today_repeats("Groceries", "Milk"));

    size_t len;
    char *json = read_fixture("feed_insight_demo.json", &len);
    static feed_t f;
    CHECK_EQ(feed_parse(json, len, &f), 0);
    const int32_t today = pda_local_day(DEMO_NOW, DEMO_TZ);
    static today_plan_t p;
    today_plan_for_today(&f, today, DEMO_NOW, DEMO_TZ, &p);
    CHECK(p.from_insight);
    line_is(&f, &p, "Send the Q4 budget sheet to Alice", "a day late; Alice needs it for the Q4 plan");
    line_is(&f, &p, "Lunch with Maria", "Maria wants the seating plan settled \xc2\xb7 Caf\xc3\xa9 Mira");
    line_is(&f, &p, "Dentist follow-up (Dr. Patel)", "bring the insurance card \xc2\xb7 Praxis Patel");
    line_is(&f, &p, "Pick up Aunt Rosa from the station (afternoon)",
            "Mom\xe2\x80\x99s 70th \xe2\x80\x94 dinner at Casa Lupo"); /* no why: the list */
    /* the call at 08:00 local is over at 09:05: not on Today */
    for (int s = 0; s < TODAY_SECTIONS; s++) {
        for (int i = 0; i < p.sec[s].n; i++) {
            const today_item_t *it = &p.sec[s].items[i];
            CHECK(!(it->ref.kind == FEED_REF_EVENT && strcmp(f.events[it->ref.event].id, "ev_demo_call_tom") == 0));
        }
    }
    /* failed */
    const today_item_t *it = find(&p, &f, "Send the Q4 budget sheet to Alice");
    char got[TODAY_LINE_MAX];
    today_second_line(&f, it, true, DEMO_TZ, got, sizeof(got));
    CHECK(strncmp(got, "Couldn't sync", 13) == 0);
    /* the time column */
    char hm[16], wd[16];
    const today_item_t *den = find(&p, &f, "Dentist follow-up (Dr. Patel)");
    today_event_column(&f.events[den->ref.event], today, DEMO_TZ, hm, sizeof(hm), wd, sizeof(wd));
    CHECK(strcmp(hm, "09:00") == 0);
    CHECK(strcmp(wd, "Wed") == 0);
    const today_item_t *lunch = find(&p, &f, "Lunch with Maria");
    today_event_column(&f.events[lunch->ref.event], today, DEMO_TZ, hm, sizeof(hm), wd, sizeof(wd));
    CHECK(strcmp(hm, "12:30") == 0);
    CHECK_EQ(wd[0], 0);
    feed_free(&f);
    free(json);

    /* rules (no insight) */
    json = read_fixture("feed_demo.json", &len);
    CHECK_EQ(feed_parse(json, len, &f), 0);
    today_plan_for_today(&f, today, DEMO_NOW, DEMO_TZ, &p);
    CHECK(!p.from_insight);
    line_is(&f, &p, "Return the library book", "no date");
    line_is(&f, &p, "Team stand-up", "Room 2.14");
    feed_free(&f);
    free(json);
    return checks_done("test_today_text");
}
