/* The event log in NVS (event_log.h has the entries). The ring is kept as EVENT_LOG_BLOCKS
 * blobs of EVENT_LOG_BLOCK entries ("eb00".."eb15") plus the entry counter ("next"): an
 * event rewrites one 128-byte blob, so ~100 entries of the 24 KB NVS partition hold the
 * whole log, and a few dozen events a day wear it by about one page erase a day. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "event_log.h"

#ifdef __cplusplus
extern "C" {
#endif

/* After nvs_flash_init(). Loads the log into RAM. */
void event_log_init(void);

/* Add an entry stamped with the system clock and uptime. batt: state of charge in %, or -1
 * (the caller reads it, so tasks that must not touch I2C pass -1). Thread-safe; writes
 * flash (a few ms, more when NVS frees a page), so not from an ISR or a timer callback. A
 * failure only logs a warning. */
void event_log_add(el_kind_t kind, int why, int32_t arg, uint8_t aux, int batt);

/* The boot entry: esp_reset_reason(). arg: 1 = woken from standby, 2 = recording resumed. */
void event_log_boot(int32_t arg, int batt);

/* Console: print the entries, oldest first (UTC). */
void event_log_print(void);

void event_log_clear(void);

#ifdef __cplusplus
}
#endif
