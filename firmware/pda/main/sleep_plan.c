#include "sleep_plan.h"

int64_t sleep_plan_target(const sleep_cfg_t *c, int64_t now_s, int64_t last_sync_s,
                          int override_min)
{
    if (override_min > 0) {
        return now_s + (int64_t)override_min * 60;
    }
    /* The phone is due interval_s after the BYE that ended its last sync. */
    if (last_sync_s > 0 && last_sync_s <= now_s && now_s - last_sync_s < c->interval_s) {
        return last_sync_s + c->interval_s;
    }
    /* No sync to go by: the phone, if it was due, would have found us while we were up. */
    return now_s + c->interval_s;
}

uint64_t sleep_plan_sleep_us(const sleep_cfg_t *c, int64_t now_s, int64_t target_s)
{
    int64_t d = target_s - c->early_s - now_s;
    if (d > SLEEP_MAX_PLAN_S) {
        d = SLEEP_MAX_PLAN_S;
    }
    d -= d * c->drift_pct / 100;
    if (d < c->min_sleep_s) {
        d = c->min_sleep_s;
    }
    return (uint64_t)d * 1000000u;
}

sleep_wake_t sleep_plan_on_wake(const sleep_cfg_t *c, int64_t now_s, int64_t target_s,
                                uint64_t *arg)
{
    const int64_t early_by = target_s - now_s;
    if (early_by > c->early_s + c->resleep_s && early_by <= SLEEP_MAX_PLAN_S + c->early_s) {
        *arg = sleep_plan_sleep_us(c, now_s, target_s);
        return SLEEP_WAKE_AGAIN;
    }
    /* On time, late, or a target that makes no sense (a clock jump): stay for a while. */
    int64_t window_s = early_by <= SLEEP_MAX_PLAN_S ? early_by + c->after_s : c->min_window_s;
    if (window_s < c->min_window_s) {
        window_s = c->min_window_s;
    }
    if (window_s > c->max_awake_s) {
        window_s = c->max_awake_s;
    }
    *arg = (uint64_t)window_s * 1000u;
    return SLEEP_WAKE_STAY;
}

sleep_step_t sleep_plan_step(const sleep_cfg_t *c, const sleep_wake_state_t *st)
{
    if (st->boot_pressed) {
        return SLEEP_STEP_WAKE_UI;
    }
    if (st->elapsed_ms >= (int64_t)c->max_awake_s * 1000) {
        return SLEEP_STEP_SLEEP; /* hard cap, even with a link up */
    }
    if (st->bye_ms >= 0) {
        /* Synced. The T5 drops the link 1 s after BYE; don't wait for the phone to. */
        return !st->connected || st->elapsed_ms - st->bye_ms >= SLEEP_BYE_GRACE_MS
                   ? SLEEP_STEP_SLEEP
                   : SLEEP_STEP_WAIT;
    }
    if (st->connected) {
        return SLEEP_STEP_WAIT; /* a sync in progress: up to the hard cap */
    }
    int64_t limit = st->window_ms;
    if (st->link_down_ms >= 0) {
        const int64_t retry = st->link_down_ms + (int64_t)c->link_retry_s * 1000;
        if (retry > limit) {
            limit = retry; /* the phone comes back after a lost link */
        }
    }
    return st->elapsed_ms >= limit ? SLEEP_STEP_SLEEP : SLEEP_STEP_WAIT;
}
