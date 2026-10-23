/* Sleep/wake log: what the T5 did across deep sleeps and resets, kept in flash (NVS), so it
 * survives a power cut. The RTC-memory counters of pda_sleep.c don't: a power-on wipes them,
 * and with them any trace of what went wrong before it.
 *
 * One entry per boot (reset reason, wake cause) and one per deep sleep (why, how long until
 * the sync timer). A boot that follows another boot with no sleep in between means the
 * earlier one never went back to sleep: it hung or lost power while awake.
 *
 * This file is the pure part (entries, names, the text form; host-tested in
 * test/test_wake_log.c). wake_log_store.c keeps the entries in NVS. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define WAKE_LOG_CAP 128 /* about 16 h of sync wakes (two entries every 15 min) */
#define WAKE_LOG_LINE 128

typedef enum {
    WL_BOOT = 1,       /* why: wl_reset_t; arg: wl_wake_t */
    WL_SLEEP = 2,      /* why: wl_sleep_t; arg: seconds until the sync timer, 0 = BOOT only */
    WL_UI_RESTART = 3, /* BOOT pressed during a sync wake: restarting into the UI */
} wl_kind_t;

/* The reset reason, as esp_reset_reason() gives it (mapped in wake_log_store.c). */
typedef enum {
    WL_RST_UNKNOWN,
    WL_RST_POWERON,   /* power came on, or the RST button */
    WL_RST_EXT,
    WL_RST_SW,        /* esp_restart() */
    WL_RST_PANIC,
    WL_RST_INT_WDT,
    WL_RST_TASK_WDT,
    WL_RST_WDT,
    WL_RST_DEEPSLEEP, /* a wake from deep sleep */
    WL_RST_BROWNOUT,
    WL_RST_USB,
    WL_RST_JTAG,
    WL_RST_OTHER,
    WL_RST_COUNT_,
} wl_reset_t;

/* t5_wake_t, kept here as numbers so the log stays readable if that enum changes. */
typedef enum {
    WL_WAKE_RESET = 0, /* not from our sleep */
    WL_WAKE_BUTTON = 1,
    WL_WAKE_TIMER = 2,
} wl_wake_t;

typedef enum {
    WL_SLEEP_USER = 1,     /* from the UI: BOOT held, or the console's sleep */
    WL_SLEEP_SYNCED,       /* sync wake: the phone synced, nothing new */
    WL_SLEEP_SYNCED_FEED,  /* sync wake: the phone synced and sent a new feed */
    WL_SLEEP_NO_SYNC,      /* sync wake: the phone didn't come */
    WL_SLEEP_EARLY,        /* sync wake too early: straight back to sleep */
    WL_SLEEP_AFTER_CRASH,  /* a crash during a sync wake: back to sleep */
    WL_SLEEP_STUCK,        /* the backstop timer: the sync wake loop was stuck */
    WL_SLEEP_NO_STORAGE,   /* sync wake: LittleFS/NVS unavailable */
    WL_SLEEP_NO_BLE,       /* sync wake: BLE didn't start */
    WL_SLEEP_COUNT_,
} wl_sleep_t;

typedef struct {
    uint32_t t;    /* UTC epoch seconds; 0 = clock unknown */
    uint16_t up_s; /* seconds since this boot, saturating */
    uint8_t kind;  /* wl_kind_t */
    uint8_t why;   /* wl_reset_t or wl_sleep_t */
    int32_t arg;
    int8_t batt;   /* state of charge in %, -1 = not read */
    uint8_t pad[3];
} wl_entry_t;

const char *wl_reset_name(int why);
const char *wl_sleep_name(int why);

/* One line, local time: "Thu 23 Oct 09:17:25 +1s  boot: power-on or RST, not from sleep;
 * batt 97%". */
void wl_format(const wl_entry_t *e, int tz_min, char *out, size_t cap);

/* True if `cur` is a boot right after `prev`, another boot, with no sleep logged between:
 * the boot of `prev` never went back to deep sleep (hung, or lost power while awake). A
 * restart into the UI (WL_UI_RESTART) or a boot into the UI (prev not a timer wake, i.e. the
 * T5 was in use) isn't flagged. */
bool wl_missed_sleep(const wl_entry_t *prev, const wl_entry_t *cur);

#ifdef __cplusplus
}
#endif
