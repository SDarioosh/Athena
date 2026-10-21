/* Deep sleep with sync wakes (docs/t5-pda-plan.md §2 "Sleep"; the timing is sleep_plan.h).
 *
 * Asleep, the T5 wakes on BOOT (EXT0) and on a timer aimed at the moment the phone's next
 * sync is due. A timer wake runs no UI and leaves the panel alone: the clock from the
 * PCF8563, NVS and LittleFS (pda_app), then BLE for bonded relays only (no pairing window,
 * fast advertising). It stays up for a bounded window, syncs if the phone comes, and goes
 * back to sleep; a BOOT press restarts into the UI. If a new feed came, only the "synced
 * HH:MM" line of the Asleep screen is redrawn, with a small partial update.
 *
 * The plan and the screen's lines live in RTC memory, which keeps them through deep sleep
 * and resets other than power-on. */
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "ble_pda.h"

#ifdef __cplusplus
extern "C" {
#endif

#define PDA_SLEEP_LINE_MAX 48

/* From the UI, with the Asleep screen on the panel and its lines as drawn: plan the first
 * sync wake and sleep. override_min 0 = the plan (sleep_plan_target), > 0 = the first wake
 * in that many minutes (then the plan again), < 0 = no sync wakes, BOOT only. Doesn't
 * return. */
void pda_sleep_enter(const char *since, const char *synced, int override_min);

/* app_main on a T5_WAKE_TIMER boot, after nvs_flash_init() and t5_board_init(). Doesn't
 * return: back to deep sleep, or a restart into the UI. */
void pda_sleep_sync_wake(const ble_pda_hooks_t *hooks);

/* app_main on every other boot, after t5_board_init(): a crash, watchdog or brownout during
 * a sync wake goes back to sleep (panel untouched) instead of starting the UI, so a fault
 * can't keep the T5 awake. Returns if the boot is a normal one. */
void pda_sleep_check_crash(void);

/* "synced HH:MM" in the display time zone, or "" for t == 0. */
void pda_sleep_synced_line(int64_t t, char *out, size_t cap);

/* Console: the plan and counters (since power-on). */
void pda_sleep_print(void);

#ifdef __cplusplus
}
#endif
