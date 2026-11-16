#include "ambient.h"

#include <stdio.h>

#include "pda_time.h"
#include "today_plan.h"

static bool set(amb_t *a, amb_stage_t s, int64_t now_ms)
{
    if (a->stage == s) {
        return false;
    }
    a->stage = s;
    a->since_ms = now_ms;
    return true;
}

void amb_reset(amb_t *a, int64_t now_ms)
{
    a->stage = AMB_AWAKE;
    a->since_ms = now_ms;
}

bool amb_wake(amb_t *a, int64_t now_ms)
{
    return set(a, AMB_AWAKE, now_ms);
}

bool amb_dim(amb_t *a, int64_t now_ms)
{
    return a->stage == AMB_AWAKE && set(a, AMB_AMBIENT, now_ms);
}

bool amb_tick(amb_t *a, int64_t now_ms, int64_t last_activity_ms, const amb_cfg_t *cfg)
{
    switch (a->stage) {
    case AMB_AWAKE: {
        if (cfg->dim_after_s <= 0 || cfg->hold) {
            return false;
        }
        const int64_t from = last_activity_ms > a->since_ms ? last_activity_ms : a->since_ms;
        return now_ms - from >= (int64_t)cfg->dim_after_s * 1000 && set(a, AMB_AMBIENT, now_ms);
    }
    case AMB_AMBIENT:
        return cfg->off_after_s > 0 && now_ms - a->since_ms >= (int64_t)cfg->off_after_s * 1000 &&
               set(a, AMB_OFF, now_ms);
    case AMB_OFF: return false;
    }
    return false;
}

void amb_shift(int64_t minute, int32_t *dx, int32_t *dy)
{
    /* a boustrophedon walk over the 5 x 5 grid: neighbours one step apart, so the picture
     * only ever moves a few pixels at a time */
    const int k = (int)(((minute % 25) + 25) % 25);
    const int row = k / 5;
    const int col = row % 2 == 0 ? k % 5 : 4 - k % 5;
    *dx = (col - 2) * AMB_SHIFT_PX / 2;
    *dy = (row - 2) * AMB_SHIFT_PX / 2;
}

int amb_next_event(const feed_t *f, int64_t now, int tz_min)
{
    if (!f) {
        return -1;
    }
    const int32_t today = pda_local_day(now, tz_min);
    int best = -1;
    int64_t best_key = INT64_MAX;
    for (int i = 0; i < f->n_events; i++) {
        const feed_event_t *e = &f->events[i];
        if (today_event_over(e, today, now)) {
            continue;
        }
        const int64_t key = e->all_day ? (int64_t)e->start_day * 86400 - tz_min * 60 : e->start;
        if (key < best_key) {
            best_key = key;
            best = i;
        }
    }
    return best;
}

void amb_fmt_date_long(int64_t epoch, int tz_min, char *out, size_t cap)
{
    static const char *const WD[] = {"Thursday", "Friday", "Saturday", "Sunday", "Monday", "Tuesday", "Wednesday"};
    static const char *const MO[] = {"January", "February", "March",     "April",   "May",      "June",
                                     "July",    "August",   "September", "October", "November", "December"};
    const int32_t day = pda_local_day(epoch, tz_min);
    /* civil from days (H. Hinnant) */
    const int32_t z = day + 719468;
    const int32_t era = (z >= 0 ? z : z - 146096) / 146097;
    const int32_t doe = z - era * 146097;
    const int32_t yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    const int32_t doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    const int32_t mp = (5 * doy + 2) / 153;
    const int32_t d = doy - (153 * mp + 2) / 5 + 1;
    const int32_t m = mp < 10 ? mp + 3 : mp - 9;
    snprintf(out, cap, "%s %d %s", WD[((day % 7) + 7) % 7], (int)d, MO[m - 1]);
}

void amb_fmt_when(const feed_event_t *e, int tz_min, char *out, size_t cap)
{
    char d[24];
    pda_fmt_day(today_event_day(e, tz_min), d, sizeof(d));
    if (e->all_day) {
        snprintf(out, cap, "%s \xc2\xb7 all day", d);
        return;
    }
    char a[12], b[12];
    pda_fmt_hm(e->start, tz_min, a, sizeof(a));
    if (e->end > e->start) {
        pda_fmt_hm(e->end, tz_min, b, sizeof(b));
        snprintf(out, cap, "%s \xc2\xb7 %s \xe2\x80\x93 %s", d, a, b);
    } else {
        snprintf(out, cap, "%s \xc2\xb7 %s", d, a);
    }
}
