/* Timed sync wakes (sleep_plan.c): the plan's rules, then a day against a model of
 * android-relay's schedule (due interval after BYE, scanning without a break while due). */
#include "check.h"
#include "sleep_plan.h"

static const sleep_cfg_t C = SLEEP_CFG_DEFAULT;

static sleep_step_t step(int64_t elapsed, int64_t window, bool conn, int64_t bye, int64_t down,
                         bool boot)
{
    const sleep_wake_state_t st = {elapsed, window, conn, bye, down, boot};
    return sleep_plan_step(&C, &st);
}

/* The phone (PdaSchedule, automatic mode): due `interval` after a BYE; while due it scans,
 * hears the T5 within detect_s of it being up, and a sync takes sync_s. */
typedef struct {
    int interval_s, detect_s, sync_s;
} phone_t;

typedef struct {
    int syncs, wakes, max_gap_s;
    int64_t awake_s;
} day_t;

/* clock_err: the deep-sleep timer runs this many per mille slow (+) or fast (-). */
static day_t simulate_day(phone_t ph, int clock_err_pm)
{
    day_t d = {0};
    int64_t now = 1000000, last_bye = 0, phone_due = now + 120; /* due 2 min after sleep */
    int64_t target = sleep_plan_target(&C, now, 0, 0);
    const int64_t end = now + 24 * 3600;
    int64_t prev_sync = now;
    while (now < end) {
        const uint64_t us = sleep_plan_sleep_us(&C, now, target);
        now += (int64_t)(us / 1000000) * (1000 + clock_err_pm) / 1000;
        uint64_t arg = 0;
        if (sleep_plan_on_wake(&C, now, target, &arg) == SLEEP_WAKE_AGAIN) {
            CHECK(arg / 1000000 < (uint64_t)C.interval_s);
            now += (int64_t)(arg / 1000000) * (1000 + clock_err_pm) / 1000;
            CHECK(sleep_plan_on_wake(&C, now, target, &arg) == SLEEP_WAKE_STAY);
        }
        d.wakes++;
        const int64_t wake = now, window_ms = (int64_t)arg;
        const int64_t up = wake + 2; /* boot and NimBLE */
        const int64_t found = (phone_due > up ? phone_due : up) + ph.detect_s;
        int64_t bye_ms = -1, elapsed_ms = 0;
        for (;; elapsed_ms += 100) {
            const int64_t t_ms = elapsed_ms;
            const bool conn = t_ms >= (found - wake) * 1000 && t_ms < (found - wake + ph.sync_s) * 1000 + 1000;
            if (bye_ms < 0 && t_ms >= (found - wake + ph.sync_s) * 1000 && conn) {
                bye_ms = t_ms;
            }
            if (step(t_ms, window_ms, conn, bye_ms, -1, false) ==
                SLEEP_STEP_SLEEP) {
                break;
            }
        }
        now = wake + elapsed_ms / 1000;
        d.awake_s += elapsed_ms / 1000;
        CHECK(elapsed_ms <= (int64_t)C.max_awake_s * 1000);
        if (bye_ms >= 0) {
            last_bye = wake + bye_ms / 1000;
            phone_due = last_bye + 1 + ph.interval_s;
            d.syncs++;
            if (last_bye - prev_sync > d.max_gap_s) {
                d.max_gap_s = (int)(last_bye - prev_sync);
            }
            prev_sync = last_bye;
        }
        target = sleep_plan_target(&C, now, last_bye, 0);
    }
    return d;
}

int main(void)
{
    /* targets */
    CHECK_EQ(sleep_plan_target(&C, 10000, 0, 0), 10000 + 900);
    CHECK_EQ(sleep_plan_target(&C, 10000, 9700, 0), 9700 + 900);  /* synced 5 min ago */
    CHECK_EQ(sleep_plan_target(&C, 10000, 9000, 0), 10000 + 900); /* longer ago than 15 min */
    CHECK_EQ(sleep_plan_target(&C, 10000, 20000, 0), 10000 + 900); /* BYE "in the future" */
    CHECK_EQ(sleep_plan_target(&C, 10000, 9700, 2), 10000 + 120);  /* `sleep 2` */

    /* the timer: early, shortened by the drift budget, never below the minimum */
    CHECK_EQ(sleep_plan_sleep_us(&C, 0, 900), (uint64_t)(890 - 890 * 2 / 100) * 1000000u);
    CHECK_EQ(sleep_plan_sleep_us(&C, 0, 5), (uint64_t)C.min_sleep_s * 1000000u);
    CHECK_EQ(sleep_plan_sleep_us(&C, 0, -500), (uint64_t)C.min_sleep_s * 1000000u);
    CHECK(sleep_plan_sleep_us(&C, 0, 10 * 24 * 3600) <= (uint64_t)SLEEP_MAX_PLAN_S * 1000000u);

    /* waking: on time, a bit early, far too early, late, clock jumps */
    uint64_t arg = 0;
    CHECK(sleep_plan_on_wake(&C, 1000, 1010, &arg) == SLEEP_WAKE_STAY);
    CHECK_EQ(arg, (10 + C.after_s) * 1000);
    CHECK(sleep_plan_on_wake(&C, 1000, 1024, &arg) == SLEEP_WAKE_STAY); /* a little early */
    CHECK_EQ(arg, (24 + C.after_s) * 1000);
    CHECK(sleep_plan_on_wake(&C, 1000, 1028, &arg) == SLEEP_WAKE_AGAIN); /* the rest asleep */
    CHECK(arg >= (uint64_t)C.min_sleep_s * 1000000u && arg <= 18000000u);
    CHECK(sleep_plan_on_wake(&C, 1000, 1300, &arg) == SLEEP_WAKE_AGAIN);
    CHECK(arg >= (uint64_t)(290 - 6) * 1000000u && arg <= 290u * 1000000u);
    CHECK(sleep_plan_on_wake(&C, 1000, 990, &arg) == SLEEP_WAKE_STAY);
    CHECK_EQ(arg, (C.after_s - 10) * 1000);
    CHECK(sleep_plan_on_wake(&C, 1000, 500, &arg) == SLEEP_WAKE_STAY); /* clock jumped ahead */
    CHECK_EQ(arg, C.min_window_s * 1000);
    CHECK(sleep_plan_on_wake(&C, 1000, 1000 + 3 * 24 * 3600, &arg) == SLEEP_WAKE_STAY);
    CHECK_EQ(arg, C.min_window_s * 1000); /* clock jumped back: don't sleep for days */
    CHECK(sleep_plan_on_wake(&C, 1000, 1000 + C.early_s + C.resleep_s + C.max_awake_s, &arg) ==
          SLEEP_WAKE_AGAIN);

    /* the window */
    const int64_t win = 55000;
    CHECK(step(0, win, false, -1, -1, false) == SLEEP_STEP_WAIT);
    CHECK(step(54900, win, false, -1, -1, false) == SLEEP_STEP_WAIT);
    CHECK(step(55000, win, false, -1, -1, false) == SLEEP_STEP_SLEEP);
    CHECK(step(1000, win, false, -1, -1, true) == SLEEP_STEP_WAKE_UI);
    /* a sync in progress keeps it up past the window, not past the cap */
    CHECK(step(90000, win, true, -1, -1, false) == SLEEP_STEP_WAIT);
    CHECK(step(C.max_awake_s * 1000LL, win, true, -1, -1, false) == SLEEP_STEP_SLEEP);
    /* BYE: sleep once the link is down, or after the grace */
    CHECK(step(20000, win, true, 19000, -1, false) == SLEEP_STEP_WAIT);
    CHECK(step(20000, win, false, 19000, -1, false) == SLEEP_STEP_SLEEP);
    CHECK(step(19000 + SLEEP_BYE_GRACE_MS, win, true, 19000, -1, false) == SLEEP_STEP_SLEEP);
    /* a link lost without BYE: wait for the phone's retry, within the cap */
    CHECK(step(60000, win, false, -1, 40000, false) == SLEEP_STEP_WAIT);
    CHECK(step(40000 + C.link_retry_s * 1000LL, win, false, -1, 40000, false) == SLEEP_STEP_SLEEP);
    CHECK(step(C.max_awake_s * 1000LL, win, false, -1, C.max_awake_s * 1000LL - 1000, false) ==
          SLEEP_STEP_SLEEP);

    /* A day with the phone's defaults: a sync at every wake, about every 15 min. */
    const int errs[] = {0, -20, 20, 30, -30};
    for (unsigned i = 0; i < sizeof(errs) / sizeof(errs[0]); i++) {
        const day_t d = simulate_day((phone_t){900, 8, 20}, errs[i]);
        CHECK(d.syncs >= 24 * 60 / 16);
        CHECK(d.max_gap_s <= 16 * 60 + 120);
        CHECK(d.syncs >= d.wakes - 1); /* (the first wake can be before the phone is due) */
        CHECK(d.awake_s <= (int64_t)d.wakes * 45); /* ~40 s per wake */
    }
    /* pdaSyncMinutes 30: every other wake finds an overdue phone. */
    day_t d = simulate_day((phone_t){1800, 8, 20}, 0);
    CHECK(d.syncs >= 24 * 60 / 32);
    CHECK(d.max_gap_s <= 31 * 60 + 120);
    /* pdaSyncMinutes 5: the phone waits for us; a sync every wake. */
    d = simulate_day((phone_t){300, 8, 20}, 0);
    CHECK(d.syncs >= d.wakes - 1);
    CHECK(d.max_gap_s <= 16 * 60 + 120);
    /* a slow phone (40 s to find us, a 60 s sync) still makes it */
    d = simulate_day((phone_t){900, 40, 60}, 20);
    CHECK(d.syncs >= 24 * 60 / 17);

    return checks_done("test_sleep_plan");
}
