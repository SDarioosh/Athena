#include "agenda.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "pda_time.h"
#include "today_plan.h"

typedef struct {
    agenda_row_t r;
    int order; /* input order, for a stable sort */
} entry_t;

static int cmp_entry(const void *a, const void *b)
{
    const entry_t *x = a, *y = b;
    if (x->r.day != y->r.day) {
        return x->r.day < y->r.day ? -1 : 1;
    }
    if (x->r.all_day != y->r.all_day) {
        return x->r.all_day ? -1 : 1;
    }
    if (!x->r.all_day && x->r.start != y->r.start) {
        return x->r.start < y->r.start ? -1 : 1;
    }
    if (x->r.kind != y->r.kind) {
        return x->r.kind == AGENDA_EVENT ? -1 : 1;
    }
    return x->order - y->order;
}

int agenda_build(const feed_t *f, const app_pending_t *pend, int n_pend, int32_t today, int64_t now,
                 int tz_min, agenda_row_t *rows, int max)
{
    static entry_t ent[FEED_MAX_EVENTS + OUTBOX_MAX];
    int n = 0;
    const int cap = (int)(sizeof(ent) / sizeof(ent[0]));
    for (int i = 0; f && i < f->n_events && n < cap; i++) {
        const feed_event_t *e = &f->events[i];
        if (today_event_over(e, today, now)) {
            continue;
        }
        int32_t day = today_event_day(e, tz_min);
        if (day < today) {
            day = today; /* started earlier, still on */
        }
        if (day >= today + AGENDA_DAYS) {
            continue;
        }
        const bool on = !e->all_day && e->start <= now && now < (e->end > e->start ? e->end : e->start + 1);
        ent[n] = (entry_t){.r = {.kind = AGENDA_EVENT, .day = day, .idx = i, .all_day = e->all_day,
                                 .start = e->start, .end = e->end, .now = on},
                           .order = n};
        n++;
    }
    for (int i = 0; pend && i < n_pend && n < cap; i++) {
        const pda_action_info_t *a = &pend[i].info;
        if (a->type != PDA_ACT_CAL_ADD) {
            continue;
        }
        int64_t t, t_end;
        bool date_only, end_date_only;
        if (!pda_time_parse(a->start, &t, &date_only)) {
            continue;
        }
        const bool all_day = a->all_day || date_only;
        const int32_t day = all_day ? pda_local_day(t, 0) : pda_local_day(t, tz_min);
        if (day < today || day >= today + AGENDA_PENDING_DAYS) {
            continue;
        }
        if (all_day || !a->end[0] || !pda_time_parse(a->end, &t_end, &end_date_only)) {
            t_end = t;
        }
        if (!all_day && t_end <= now && t_end > t) {
            continue; /* a queued event that is already over */
        }
        ent[n] = (entry_t){.r = {.kind = AGENDA_PENDING, .day = day, .idx = i, .all_day = all_day,
                                 .start = t, .end = t_end},
                           .order = n};
        n++;
    }
    qsort(ent, (size_t)n, sizeof(ent[0]), cmp_entry);
    int out = 0;
    int32_t last = FEED_NO_DAY;
    for (int i = 0; i < n; i++) {
        if (ent[i].r.day != last) {
            if (out + 2 > max) {
                break; /* a heading needs a row under it */
            }
            rows[out++] = (agenda_row_t){.kind = AGENDA_DAY, .day = ent[i].r.day, .idx = -1};
            last = ent[i].r.day;
        }
        if (out >= max) {
            break;
        }
        rows[out++] = ent[i].r;
    }
    return out;
}

void agenda_day_name(int32_t day, int32_t today, char *out, size_t cap)
{
    if (day == today) {
        snprintf(out, cap, "Today");
    } else if (day == today + 1) {
        snprintf(out, cap, "Tomorrow");
    } else {
        pda_fmt_day(day, out, cap);
    }
}

int agenda_count_items(const agenda_row_t *rows, int n)
{
    int k = 0;
    for (int i = 0; i < n; i++) {
        k += rows[i].kind != AGENDA_DAY;
    }
    return k;
}
