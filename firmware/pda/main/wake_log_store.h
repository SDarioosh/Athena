/* The sleep/wake log in NVS (wake_log.h has the entries). Each entry is its own small key
 * ("wl000".."wl127") plus the next slot in "wlnext", so an event writes about 100 bytes of
 * flash, not the whole log: two events every 15 min wear the 24 KB NVS partition by less
 * than one page erase a day. */
#pragma once

#include <stdbool.h>

#include "wake_log.h"

#ifdef __cplusplus
extern "C" {
#endif

/* After nvs_flash_init(). */
void wake_log_init(void);

/* Add an entry stamped with the clock (the PCF8563 when the system clock isn't set yet) and
 * the battery. use_i2c false: no I2C at all (no PCF8563, no gauge), for the backstop timer,
 * which may run while the stuck task holds the bus. Never blocks for long; a failure only
 * logs a warning. */
void wake_log_add(wl_kind_t kind, int why, int32_t arg, bool use_i2c);

/* The boot entry: esp_reset_reason() and the wake cause (t5_wake_t). After t5_board_init(). */
void wake_log_boot(int t5_wake);

/* Console: print the entries, oldest first, with a note after a sync wake that never went
 * back to sleep. */
void wake_log_print(void);

void wake_log_clear(void);

#ifdef __cplusplus
}
#endif
