/* Why the chip last restarted, as a short name for the log, `status` and BLE `info`
 * (protocol §4.1 `reset=`). */
#pragma once

#ifdef __cplusplus
extern "C" {
#endif

/* e.g. "poweron", "sw", "panic", "task_wdt", "brownout"; "unknown" if ESP-IDF doesn't say. */
const char *reset_reason_name(void);

#ifdef __cplusplus
}
#endif
