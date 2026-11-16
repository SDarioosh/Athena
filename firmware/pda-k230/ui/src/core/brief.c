#include "brief.h"

#include <stdio.h>
#include <string.h>

#include "pda_actions.h"
#include "pda_time.h"

void brief_split(const char *text, char *head, size_t head_cap, const char **body)
{
    head[0] = 0;
    const char *s = text ? text : "";
    while (*s == '\n' || *s == ' ') {
        s++;
    }
    *body = s;
    const char *gap = strstr(s, "\n\n");
    if (!gap) {
        return;
    }
    const size_t n = (size_t)(gap - s);
    if (n == 0 || n > BRIEF_HEADLINE_MAX || n + 1 > head_cap || memchr(s, '\n', n)) {
        return;
    }
    memcpy(head, s, n);
    head[n] = 0;
    const char *b = gap;
    while (*b == '\n' || *b == ' ') {
        b++;
    }
    *body = b;
}

int32_t brief_day(const feed_t *f)
{
    if (!f || !f->has_briefing || !f->brief_date) {
        return FEED_NO_DAY;
    }
    int64_t t;
    bool date_only;
    if (!pda_time_parse(f->brief_date, &t, &date_only)) {
        return FEED_NO_DAY;
    }
    return pda_local_day(t, 0);
}

void brief_day_name(int32_t day, int32_t today, char *out, size_t cap)
{
    if (day == today) {
        snprintf(out, cap, "Today");
    } else if (day == today - 1) {
        snprintf(out, cap, "Yesterday");
    } else {
        pda_fmt_day(day, out, cap);
    }
}

void brief_refresh_date(const feed_t *f, int32_t today, char *out, size_t cap)
{
    const int32_t d = brief_day(f);
    pda_fmt_ymd(d != FEED_NO_DAY ? d : today, out, cap);
}

size_t brief_refresh_action(const feed_t *f, int32_t today, char *out)
{
    char date[16];
    brief_refresh_date(f, today, date, sizeof(date));
    return pda_action_digest_refresh(out, date);
}

const app_pending_t *brief_find_pending(const app_pending_t *p, int n, const char *date)
{
    const app_pending_t *found = NULL;
    for (int i = 0; i < n; i++) {
        if (p[i].info.type == PDA_ACT_DIGEST_REFRESH && strcmp(p[i].info.ref, date) == 0) {
            found = &p[i]; /* the newest */
        }
    }
    return found;
}

bool brief_find_failed(const app_failed_t *fl, int n, const char *date)
{
    for (int i = 0; i < n; i++) {
        if (fl[i].type == PDA_ACT_DIGEST_REFRESH && strcmp(fl[i].what, date) == 0) {
            return true;
        }
    }
    return false;
}
