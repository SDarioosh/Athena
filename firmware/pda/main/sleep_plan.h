/* Timed sync wakes from deep sleep (docs/t5-pda-plan.md §2 "Sleep"): when to wake, how long
 * to stay awake for the phone, when to go back to sleep. Pure C, no ESP-IDF; host-tested
 * (test/test_sleep_plan.c). pda_sleep.c runs it on the T5.
 *
 * Clock times are seconds on the T5's clock (gettimeofday, set from the PCF8563 at boot, the
 * same clock as ble_pda_last_sync()); times within one wake are milliseconds since it.
 *
 * How the numbers follow from android-relay (PdaSchedule, PdaConnector, RelayService,
 * Scanner), default settings, automatic sync:
 * - After a sync that ends in BYE the phone is due again pdaSyncMinutes (default 15) later;
 *   while due it scans for the T5 without a break (10-minute scans, restarted), in
 *   SCAN_MODE_LOW_POWER (512 ms of every 5.12 s). So a due phone is waiting for us: we only
 *   have to be awake when it is due. The T5 aims its wake at "BYE + 15 min", the moment the
 *   phone becomes due, and stays a little past it.
 * - The phone isn't told our schedule. With a longer pdaSyncMinutes the first wake misses
 *   and the next one (15 min later) finds a phone that is overdue and scanning; with a
 *   shorter one the phone waits, scanning, for our next wake. Either way it syncs, every 15
 *   min or every pdaSyncMinutes, whichever is longer (plus one wake).
 * - A link lost without BYE: the phone retries 60 s later (linkRetryMs), so we wait for it.
 * - Server down: the phone still sends BYE (backoff, retry in 1-15 min); it is due before
 *   our next wake and finds us at once then.
 * - The deep-sleep timer runs on the S3's internal RC slow clock (no 32 kHz crystal on the
 *   ESP32; the PCF8563 has its own and corrects the clock at every boot). Its error after
 *   ESP-IDF's calibration is around 1 %, more with temperature: we wake early by a budget,
 *   and if the PCF8563 says we are far too early we sleep again for the rest. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Assumed phone interval: android-relay's pdaSyncMinutes default. */
#define SLEEP_SYNC_INTERVAL_S (15 * 60)
/* Awake this long before the phone is due: boot (~1.5 s), NimBLE up, and drift beyond the
 * budget below. */
#define SLEEP_EARLY_S 10
/* ... and this long after it: the phone's next scan starts when it is due (its scan window
 * is cut to the due time), Android may hold a scan start back (5 starts / 30 s), and with
 * fast advertising (~180 ms) every 512 ms scan window hears us. 45 s is several scan
 * periods of margin. */
#define SLEEP_AFTER_S 45
/* Awake at least this long, even if we woke late. */
#define SLEEP_MIN_WINDOW_S 30
/* PdaSchedule.linkRetryMs (60 s) plus a scan's worth. */
#define SLEEP_LINK_RETRY_S 75
/* Hard cap on one timer wake, connected or not: a window (~1 min), a slow sync (feed PUT,
 * server calls) and one link-loss retry. A stuck link can't keep the T5 up longer. */
#define SLEEP_MAX_AWAKE_S 240
/* Timer error budget: sleep this much shorter than planned. */
#define SLEEP_DRIFT_PCT 2
/* Woke more than this before the planned wake (the PCF8563 says so): sleep again for the
 * rest. The drift budget alone would keep the T5 up 18 s early on every wake; a second,
 * short sleep is cheaper (the decision is made ~0.5 s after the wake, before BLE). */
#define SLEEP_RESLEEP_S 15
#define SLEEP_MIN_SLEEP_S 10
/* After BYE the T5 drops the link in 1 s; go to sleep at the latest this long after BYE. */
#define SLEEP_BYE_GRACE_MS 3000
/* Plans further out than this are a clock jump, not a plan (`sleep <min>` allows 24 h). */
#define SLEEP_MAX_PLAN_S (24 * 3600)

typedef struct {
    int interval_s;
    int early_s;
    int after_s;
    int min_window_s;
    int link_retry_s;
    int max_awake_s;
    int drift_pct;
    int resleep_s;
    int min_sleep_s;
} sleep_cfg_t;

#define SLEEP_CFG_DEFAULT                                                                       \
    {                                                                                           \
        SLEEP_SYNC_INTERVAL_S, SLEEP_EARLY_S, SLEEP_AFTER_S, SLEEP_MIN_WINDOW_S,                \
            SLEEP_LINK_RETRY_S, SLEEP_MAX_AWAKE_S, SLEEP_DRIFT_PCT, SLEEP_RESLEEP_S,            \
            SLEEP_MIN_SLEEP_S                                                                   \
    }

/* When the phone is next expected. last_sync_s: the last BYE (0 none); override_min > 0:
 * `sleep <min>`, the next wake in that many minutes instead. */
int64_t sleep_plan_target(const sleep_cfg_t *c, int64_t now_s, int64_t last_sync_s,
                          int override_min);

/* The deep-sleep timer for a target: wake early_s before it, shortened by the drift budget;
 * at least min_sleep_s. Microseconds. */
uint64_t sleep_plan_sleep_us(const sleep_cfg_t *c, int64_t now_s, int64_t target_s);

typedef enum {
    SLEEP_WAKE_STAY,  /* stay awake for the phone: *arg = the window in ms */
    SLEEP_WAKE_AGAIN, /* far too early: sleep again, *arg = the timer in us */
} sleep_wake_t;

/* Just woke by the timer, the clock read from the PCF8563. */
sleep_wake_t sleep_plan_on_wake(const sleep_cfg_t *c, int64_t now_s, int64_t target_s,
                                uint64_t *arg);

typedef struct {
    int64_t elapsed_ms;   /* since the wake */
    int64_t window_ms;    /* from sleep_plan_on_wake */
    bool connected;       /* a relay is connected (and authenticated) */
    int64_t bye_ms;       /* elapsed_ms when BYE came, -1 none */
    int64_t link_down_ms; /* elapsed_ms when a link went down without BYE, -1 none */
    bool boot_pressed;
} sleep_wake_state_t;

typedef enum {
    SLEEP_STEP_WAIT,
    SLEEP_STEP_SLEEP,   /* back to deep sleep */
    SLEEP_STEP_WAKE_UI, /* BOOT pressed: wake fully */
} sleep_step_t;

/* Called every ~100 ms during a timer wake. */
sleep_step_t sleep_plan_step(const sleep_cfg_t *c, const sleep_wake_state_t *st);

#ifdef __cplusplus
}
#endif
