#include "today_text.h"

#include <stdio.h>
#include <string.h>

#include "pda_time.h"

/* The rules below are firmware/pda/main/ui_today.c's (trimmed, repeats, words, restates,
 * second_line), without its drawing. */

static const char *trimmed(const char *s, size_t *len)
{
    while (*s == ' ') {
        s++;
    }
    size_t n = strlen(s);
    for (;;) {
        if (n > 0 && (s[n - 1] == ' ' || s[n - 1] == '.')) {
            n--;
        } else if (n >= 3 && memcmp(s + n - 3, "\xe2\x80\xa6", 3) == 0) {
            n -= 3;
        } else {
            break;
        }
    }
    *len = n;
    return s;
}

bool today_repeats(const char *line, const char *first)
{
    size_t nl, nf;
    line = trimmed(line, &nl);
    first = trimmed(first, &nf);
    if (nl == 0 || nl > nf) {
        return false;
    }
    for (size_t i = 0; i < nl; i++) {
        char a = line[i], b = first[i];
        a = (char)(a >= 'A' && a <= 'Z' ? a + 32 : a);
        b = (char)(b >= 'A' && b <= 'Z' ? b + 32 : b);
        if (a != b) {
            return false;
        }
    }
    return nl == nf || nl >= 20;
}

#define WORDS_MAX 24
#define WORD_LEN 24

static bool is_word_char(const char *s)
{
    const unsigned char d = (unsigned char)*s;
    return (d >= 'a' && d <= 'z') || (d >= 'A' && d <= 'Z') || (d >= '0' && d <= '9') ||
           (d >= 0x80 && strncmp(s, "\xe2\x80\xa6", 3) != 0);
}

static int words(const char *s, char out[WORDS_MAX][WORD_LEN])
{
    int n = 0;
    while (*s && n < WORDS_MAX) {
        if (!is_word_char(s)) {
            s += strncmp(s, "\xe2\x80\xa6", 3) == 0 ? 3 : 1;
            continue;
        }
        int k = 0;
        while (*s && is_word_char(s)) {
            const unsigned char d = (unsigned char)*s;
            if (k < WORD_LEN - 1) {
                out[n][k++] = (char)(d >= 'A' && d <= 'Z' ? d + 32 : d);
            }
            s++;
        }
        out[n++][k] = 0;
    }
    return n;
}

bool today_restates(const char *why, const char *title)
{
    static char a[WORDS_MAX][WORD_LEN], b[WORDS_MAX][WORD_LEN];
    const int na = words(why, a), nb = words(title, b);
    if (na == 0 || nb == 0) {
        return false;
    }
    if (strlen(b[0]) >= 3 && strcmp(a[0], b[0]) == 0) {
        return true;
    }
    int big = 0, found = 0;
    for (int j = 0; j < nb; j++) {
        if (strlen(b[j]) < 3) {
            continue;
        }
        big++;
        for (int i = 0; i < na; i++) {
            if (strcmp(a[i], b[j]) == 0) {
                found++;
                break;
            }
        }
    }
    return big >= 2 && found * 10 >= big * 6;
}

void today_second_line(const feed_t *f, const today_item_t *it, bool failed, int tz_min, char *why,
                       size_t cap)
{
    const char *w = it->ref.why;
    why[0] = 0;
    if (it->ref.kind == FEED_REF_TODO) {
        const feed_todo_item_t *ti = &f->todos[it->ref.note].items[it->ref.item];
        if (failed) {
            snprintf(why, cap, "Couldn't sync \xe2\x80\x93 changed on the website?");
            return;
        }
        const char *r = it->reason;
        if (w && today_restates(w, ti->text)) {
            w = NULL; /* the to-do shows once */
        }
        if (r[0] && w) {
            snprintf(why, cap, "%s \xc2\xb7 %s", r, w); /* · */
        } else {
            snprintf(why, cap, "%s", r[0] ? r : w ? w : "");
        }
        if (!why[0] && f->todos[it->ref.note].n_items > 1) {
            /* no reason given: the list it is on (a one-item note's title is only the
             * to-do again, often reworded) */
            const char *title = f->todos[it->ref.note].title;
            snprintf(why, cap, "%s", title ? title : "Untitled checklist");
        }
        if (today_repeats(why, ti->text)) {
            why[0] = 0;
        }
        return;
    }
    const feed_event_t *e = &f->events[it->ref.event];
    if (w && e->title && today_restates(w, e->title)) {
        w = NULL; /* the title shows once; the place or end time instead */
    }
    if (w) {
        snprintf(why, cap, "%s", w);
    }
    if (e->where && strlen(why) + strlen(e->where) + 8 < cap) {
        const size_t n = strlen(why);
        snprintf(why + n, cap - n, "%s%s", n ? " \xc2\xb7 " : "", e->where);
    }
    if (!why[0] && !e->all_day && e->end > e->start) {
        char end[12];
        pda_fmt_hm(e->end, tz_min, end, sizeof(end));
        snprintf(why, cap, "until %s", end);
    }
}

void today_event_column(const feed_event_t *e, int32_t today, int tz_min, char *hm, size_t hm_cap,
                        char *weekday, size_t wd_cap)
{
    if (e->all_day) {
        snprintf(hm, hm_cap, "all day");
    } else {
        pda_fmt_hm(e->start, tz_min, hm, hm_cap);
    }
    weekday[0] = 0;
    const int32_t day = today_event_day(e, tz_min);
    if (day != today) {
        pda_fmt_day(day, weekday, wd_cap); /* "Wed 21 Oct" -> "Wed" */
        char *sp = strchr(weekday, ' ');
        if (sp) {
            *sp = 0;
        }
    }
}
