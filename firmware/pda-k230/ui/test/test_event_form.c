/* event_form: the new-event form's defaults, steps and limits, and the cal_add it makes
 * (the relay's strict format, T5 plan §3.5). */
#include "check.h"
#include "event_form.h"
#include "json.h"
#include "pda_actions.h"
#include "pda_time.h"
#include "test_util.h"

static void str_is(const char *got, const char *want)
{
    if (strcmp(got, want) != 0) {
        fprintf(stderr, "got  %s\nwant %s\n", got, want);
        checks_failed++;
    }
}

/* the relay's checks: exactly these fields, ISO times with an offset, end after start */
static void strict_ok(const char *js, size_t n, const char *want_title)
{
    CHECK(n > 0 && n <= PDA_PROTO_OB_JSON_MAX);
    CHECK_EQ(strlen(js), n);
    json_tok_t t[32];
    const int nt = json_parse(js, n, t, 32);
    CHECK(nt > 0);
    if (nt <= 0) {
        return;
    }
    const json_doc_t d = {js, t, nt};
    CHECK(json_str_eq(&d, json_get(&d, 0, "t"), "cal_add"));
    char title[EVENT_TITLE_BUF];
    CHECK(json_str(&d, json_get(&d, 0, "title"), title, sizeof(title)) > 0);
    CHECK(utf8_chars(title) >= 1 && utf8_chars(title) <= EVENT_TITLE_MAX_CHARS);
    if (want_title) {
        str_is(title, want_title);
    }
    char start[40], end[40];
    CHECK(json_str(&d, json_get(&d, 0, "start"), start, sizeof(start)) > 0);
    bool all_day = false;
    const int ad = json_get(&d, 0, "all_day");
    if (ad >= 0) {
        CHECK(json_bool(&d, ad, &all_day) && all_day);
    }
    int64_t a, b;
    bool date_only;
    CHECK(pda_time_parse(start, &a, &date_only));
    if (all_day) {
        CHECK_EQ(strlen(start), 10);
        CHECK(date_only);
        CHECK(json_get(&d, 0, "end") < 0);
        CHECK_EQ(t[0].size, 4); /* t, title, start, all_day */
    } else {
        CHECK(json_str(&d, json_get(&d, 0, "end"), end, sizeof(end)) > 0);
        CHECK(pda_time_parse(end, &b, &date_only));
        CHECK(b > a);
        /* an offset, never a bare local time */
        CHECK_EQ(strlen(start), 25);
        CHECK(start[19] == '+' || start[19] == '-');
        CHECK_EQ(t[0].size, 4); /* t, title, start, end */
    }
}

int main(void)
{
    const int32_t today = pda_local_day(DEMO_NOW, DEMO_TZ); /* Tue 20 Oct, 09:05 */
    event_form_t f;
    char js[PDA_ACTION_BUF], s[96];

    /* defaults: today, the next full hour, 30 min */
    event_form_init(&f, today, DEMO_NOW, DEMO_TZ);
    CHECK_EQ(f.day, today);
    CHECK_EQ(f.min, 10 * 60);
    CHECK_EQ(f.dur, 30);
    CHECK_EQ(f.from_item, -1);
    CHECK_EQ(event_form_validate(&f, today), EVENT_FORM_NO_TITLE);
    CHECK_EQ(event_form_cal_add(&f, today, DEMO_TZ, js), 0);
    event_form_init(&f, today, DEMO_NOW - 5 * 3600, DEMO_TZ); /* 04:05: from 08:00 */
    CHECK_EQ(f.min, 8 * 60);
    event_form_init(&f, today, DEMO_NOW + 14 * 3600 + 30 * 60, DEMO_TZ); /* 23:35: 23:00 */
    CHECK_EQ(f.min, 23 * 60);

    /* day picker: today .. +13 */
    CHECK(event_form_set_day(&f, today + 13, today));
    CHECK(!event_form_set_day(&f, today + 14, today));
    CHECK(!event_form_set_day(&f, today - 1, today));
    CHECK_EQ(f.day, today + 13);

    /* steps stay within the day */
    event_form_set_hour(&f, 0);
    event_form_step(&f, -15);
    CHECK_EQ(f.min, 0);
    event_form_set_hour(&f, 23);
    event_form_step(&f, 15);
    event_form_step(&f, 15);
    event_form_step(&f, 15);
    event_form_step(&f, 15);
    CHECK_EQ(f.min, 23 * 60 + 45);
    event_form_set_duration(&f, 45); /* not offered: unchanged */
    CHECK_EQ(f.dur, 30);
    event_form_set_duration(&f, 0);
    CHECK_EQ(f.dur, 0);
    event_form_step(&f, -15); /* a time again: timed, 30 min */
    CHECK_EQ(f.dur, 30);
    CHECK_EQ(f.min, 23 * 60 + 30);

    /* timed: ISO with the display offset */
    event_form_init(&f, today, DEMO_NOW, DEMO_TZ);
    event_form_set_day(&f, today + 1, today);
    event_form_set_hour(&f, 9);
    event_form_set_title(&f, "Call");
    CHECK_EQ(event_form_validate(&f, today), EVENT_FORM_OK);
    size_t n = event_form_cal_add(&f, today, DEMO_TZ, js);
    str_is(js, "{\"t\":\"cal_add\",\"title\":\"Call\",\"start\":\"2026-10-21T09:00:00+02:00\","
               "\"end\":\"2026-10-21T09:30:00+02:00\"}");
    strict_ok(js, n, "Call");
    event_form_when_text(&f, today, s, sizeof(s));
    str_is(s, "Tomorrow \xc2\xb7 09:00 \xe2\x80\x93 09:30");
    /* the T5 reads it back the same */
    pda_action_info_t info;
    CHECK(pda_action_parse(js, n, &info));
    CHECK_EQ(info.type, PDA_ACT_CAL_ADD);
    str_is(info.start, "2026-10-21T09:00:00+02:00");

    /* 60 min across midnight, west of UTC */
    event_form_set_hour(&f, 23);
    event_form_step(&f, 45);
    event_form_set_duration(&f, 60);
    n = event_form_cal_add(&f, today, -300, js);
    str_is(js, "{\"t\":\"cal_add\",\"title\":\"Call\",\"start\":\"2026-10-21T23:45:00-05:00\","
               "\"end\":\"2026-10-22T00:45:00-05:00\"}");
    strict_ok(js, n, "Call");
    event_form_time_text(&f, s, sizeof(s));
    str_is(s, "23:45 \xe2\x80\x93 00:45");

    /* all day: a date, no end */
    event_form_set_day(&f, today + 2, today);
    event_form_set_duration(&f, 0);
    event_form_set_title(&f, "Meeting");
    n = event_form_cal_add(&f, today, DEMO_TZ, js);
    str_is(js, "{\"t\":\"cal_add\",\"title\":\"Meeting\",\"start\":\"2026-10-22\",\"all_day\":true}");
    strict_ok(js, n, "Meeting");
    event_form_when_text(&f, today, s, sizeof(s));
    str_is(s, "Thu 22 Oct \xc2\xb7 All day");

    /* a typed title: trimmed, line breaks as spaces, quotes and backslashes escaped */
    event_form_set_duration(&f, 15);
    event_form_set_title(&f, "  Lunch \"Caf\xc3\xa9 Mira\" \\ plan\nnext\t ");
    CHECK_EQ(event_form_validate(&f, today), EVENT_FORM_OK);
    n = event_form_cal_add(&f, today, DEMO_TZ, js);
    CHECK(strstr(js, "\"title\":\"Lunch \\\"Caf\xc3\xa9 Mira\\\" \\\\ plan next\"") != NULL);
    strict_ok(js, n, "Lunch \"Caf\xc3\xa9 Mira\" \\ plan next");

    /* only spaces or line breaks: no title */
    event_form_set_title(&f, " \n\t ");
    CHECK_EQ(event_form_validate(&f, today), EVENT_FORM_NO_TITLE);
    CHECK_EQ(event_form_cal_add(&f, today, DEMO_TZ, js), 0);
    CHECK(js[0] == 0);
    CHECK(strcmp(event_form_err_text(EVENT_FORM_NO_TITLE), "Give it a title") == 0);

    /* 200 characters at most: ASCII is cut to 200 and fits the 400 bytes */
    char long_title[1200];
    memset(long_title, 'a', 250);
    long_title[250] = 0;
    event_form_set_title(&f, long_title);
    CHECK_EQ(utf8_chars(f.title), 200);
    n = event_form_cal_add(&f, today, DEMO_TZ, js);
    strict_ok(js, n, NULL);
    CHECK(strstr(js, "\"title\":\"aaaa") != NULL);
    {
        json_tok_t t[16];
        const int nt = json_parse(js, n, t, 16);
        const json_doc_t d = {js, t, nt};
        char got[EVENT_TITLE_BUF];
        json_str(&d, json_get(&d, 0, "title"), got, sizeof(got));
        CHECK_EQ(strlen(got), 200);
    }
    /* two-byte characters: 200 kept in the form, the 400-byte action cuts at a boundary */
    long_title[0] = 0;
    for (int i = 0; i < 250; i++) {
        strcat(long_title, "\xc3\xa9");
    }
    event_form_set_title(&f, long_title);
    CHECK_EQ(utf8_chars(f.title), 200);
    CHECK_EQ(strlen(f.title), 400);
    n = event_form_cal_add(&f, today, DEMO_TZ, js);
    strict_ok(js, n, NULL);
    /* 200 is fine, 201 isn't (a title set directly) */
    memset(f.title, 'b', 201);
    f.title[201] = 0;
    CHECK_EQ(event_form_validate(&f, today), EVENT_FORM_TITLE_LONG);
    f.title[200] = 0;
    CHECK_EQ(event_form_validate(&f, today), EVENT_FORM_OK);

    /* out of range: no action */
    f.day = today + EVENT_FORM_DAYS;
    CHECK_EQ(event_form_validate(&f, today), EVENT_FORM_BAD_DAY);
    f.day = today - 1;
    CHECK_EQ(event_form_cal_add(&f, today, DEMO_TZ, js), 0);
    f.day = today;
    f.min = 9 * 60 + 7;
    CHECK_EQ(event_form_validate(&f, today), EVENT_FORM_BAD_TIME);

    /* from an action item (T5 "→ calendar") */
    event_form_init(&f, today, DEMO_NOW, DEMO_TZ);
    f.from_item = 42;
    event_form_set_title(&f, "Send the Q4 budget");
    n = event_form_cal_add(&f, today, DEMO_TZ, js);
    CHECK(strstr(js, ",\"from_item\":42}") != NULL);

    return checks_done("test_event_form");
}
