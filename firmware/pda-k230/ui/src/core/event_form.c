#include "event_form.h"

#include <stdio.h>
#include <string.h>

#include "agenda.h"
#include "pda_actions.h"
#include "pda_time.h"

size_t utf8_chars(const char *s)
{
    size_t n = 0;
    for (; s && *s; s++) {
        n += ((unsigned char)*s & 0xC0) != 0x80;
    }
    return n;
}

/* bytes of the first `chars` characters of s */
static size_t utf8_prefix(const char *s, size_t chars)
{
    size_t i = 0, k = 0;
    while (s[i]) {
        if (((unsigned char)s[i] & 0xC0) != 0x80) {
            if (k == chars) {
                break;
            }
            k++;
        }
        i++;
    }
    return i;
}

void event_form_init(event_form_t *f, int32_t today, int64_t now, int tz_min)
{
    memset(f, 0, sizeof(*f));
    f->day = today;
    const int64_t local = now + (int64_t)tz_min * 60;
    const int local_min = (int)(((local % 86400) + 86400) % 86400 / 60);
    int m = (local_min / 60 + 1) * 60;
    if (m < EVENT_FIRST_SLOT * 60) {
        m = EVENT_FIRST_SLOT * 60;
    }
    if (m > 23 * 60) {
        m = 23 * 60;
    }
    f->min = m;
    f->dur = 30;
    f->from_item = -1;
}

bool event_form_set_day(event_form_t *f, int32_t day, int32_t today)
{
    if (day < today || day >= today + EVENT_FORM_DAYS) {
        return false;
    }
    f->day = day;
    return true;
}

void event_form_set_hour(event_form_t *f, int hour)
{
    if (hour < 0 || hour > 23) {
        return;
    }
    f->min = hour * 60;
    if (f->dur == 0) {
        f->dur = 30;
    }
}

void event_form_step(event_form_t *f, int delta_min)
{
    int m = f->min + delta_min;
    if (m < 0) {
        m = 0;
    }
    if (m > 24 * 60 - EVENT_STEP_MIN) {
        m = 24 * 60 - EVENT_STEP_MIN;
    }
    f->min = m - m % EVENT_STEP_MIN;
    if (f->dur == 0) {
        f->dur = 30;
    }
}

void event_form_set_duration(event_form_t *f, int dur_min)
{
    if (dur_min == 0 || dur_min == 15 || dur_min == 30 || dur_min == 60) {
        f->dur = dur_min;
    }
}

void event_form_set_title(event_form_t *f, const char *title)
{
    const char *s = title ? title : "";
    size_t n = utf8_prefix(s, EVENT_TITLE_MAX_CHARS);
    if (n >= sizeof(f->title)) {
        n = sizeof(f->title) - 1;
        while (n > 0 && ((unsigned char)s[n] & 0xC0) == 0x80) {
            n--; /* not in the middle of a character */
        }
    }
    memcpy(f->title, s, n);
    f->title[n] = 0;
}

/* the title as sent: line breaks and tabs as spaces, trimmed */
static void clean_title(const char *in, char *out, size_t cap)
{
    size_t w = 0;
    for (const char *p = in; *p && w + 1 < cap; p++) {
        const char c = *p;
        out[w++] = c == '\n' || c == '\r' || c == '\t' ? ' ' : c;
    }
    out[w] = 0;
    size_t a = 0;
    while (out[a] == ' ') {
        a++;
    }
    size_t b = strlen(out);
    while (b > a && out[b - 1] == ' ') {
        b--;
    }
    memmove(out, out + a, b - a);
    out[b - a] = 0;
}

event_form_err_t event_form_validate(const event_form_t *f, int32_t today)
{
    char t[EVENT_TITLE_BUF];
    clean_title(f->title, t, sizeof(t));
    if (!t[0]) {
        return EVENT_FORM_NO_TITLE;
    }
    if (utf8_chars(t) > EVENT_TITLE_MAX_CHARS) {
        return EVENT_FORM_TITLE_LONG;
    }
    if (f->day < today || f->day >= today + EVENT_FORM_DAYS) {
        return EVENT_FORM_BAD_DAY;
    }
    if (f->dur != 0) {
        if (f->dur < 0 || f->min < 0 || f->min >= 24 * 60 || f->min % EVENT_STEP_MIN != 0) {
            return EVENT_FORM_BAD_TIME;
        }
    }
    return EVENT_FORM_OK;
}

const char *event_form_err_text(event_form_err_t e)
{
    switch (e) {
    case EVENT_FORM_OK: return "";
    case EVENT_FORM_NO_TITLE: return "Give it a title";
    case EVENT_FORM_TITLE_LONG: return "The title is too long (200 characters at most)";
    case EVENT_FORM_BAD_DAY: return "Pick a day in the next two weeks";
    case EVENT_FORM_BAD_TIME: return "Pick a time";
    }
    return "";
}

size_t event_form_cal_add(const event_form_t *f, int32_t today, int tz_min, char *out)
{
    out[0] = 0;
    if (event_form_validate(f, today) != EVENT_FORM_OK) {
        return 0;
    }
    char t[EVENT_TITLE_BUF];
    clean_title(f->title, t, sizeof(t));
    char start[32], end[32];
    if (f->dur == 0) {
        pda_fmt_ymd(f->day, start, sizeof(start));
        return pda_action_cal_add(out, t, start, NULL, true, f->from_item);
    }
    /* the local wall time of that day in the display offset (T5 A_ADD) */
    const int64_t t0 = (int64_t)f->day * 86400 + (int64_t)f->min * 60 - (int64_t)tz_min * 60;
    pda_fmt_iso_local(t0, tz_min, start, sizeof(start));
    pda_fmt_iso_local(t0 + (int64_t)f->dur * 60, tz_min, end, sizeof(end));
    return pda_action_cal_add(out, t, start, end, false, f->from_item);
}

void event_form_time_text(const event_form_t *f, char *out, size_t cap)
{
    if (f->dur == 0) {
        snprintf(out, cap, "All day");
        return;
    }
    const int e = (f->min + f->dur) % (24 * 60);
    snprintf(out, cap, "%02d:%02d \xe2\x80\x93 %02d:%02d", f->min / 60, f->min % 60, e / 60, e % 60);
}

void event_form_when_text(const event_form_t *f, int32_t today, char *out, size_t cap)
{
    char d[24], t[32];
    agenda_day_name(f->day, today, d, sizeof(d));
    event_form_time_text(f, t, sizeof(t));
    snprintf(out, cap, "%s \xc2\xb7 %s", d, t);
}
