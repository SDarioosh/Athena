/* Power log for milestone 2 (plan §7.3): every N minutes a battery sample with the watch's
 * state goes into a RAM ring (PSRAM, 7 days at 5 min) and from there to /sdcard/power.csv.
 * Samples are written once an hour (or when the RAM ring is 3/4 full), only while nothing
 * records or syncs: on the owner's watch every card access from a second task during a
 * recording failed (sdspi 0x107), so the log never touches the card then. Powering the card
 * for it costs ~0.2 s an hour. A power cut loses at most that hour of the log.
 * Console: `power`, `power log <minutes>|off`, `power flush`, `power locks`. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "power_samples.h"

#ifdef __cplusplus
extern "C" {
#endif

#define POWER_LOG_FILE "power.csv"
#define POWER_LOG_DEFAULT_MIN 5
#define POWER_LOG_FLUSH_S 3600
#define POWER_LOG_RETRY_S 600 /* after a failed write */

/* Fills a sample with the battery and the watch's state now. */
typedef void (*power_sample_fn_t)(ps_sample_t *out);
/* True while the card must be left alone (a recording or a BLE sync is using it). */
typedef bool (*power_card_busy_fn_t)(void);

esp_err_t power_log_init(power_sample_fn_t sample, power_card_busy_fn_t card_busy);
/* Call about once a second (app main loop): takes a sample when due, writes when due. */
void power_log_poll(void);
/* Minutes between samples, 0 = off; saved in NVS. */
esp_err_t power_log_set_period(int minutes);
/* Write every held sample to the card now; ESP_ERR_INVALID_STATE while the card is busy. */
esp_err_t power_log_flush(void);
/* Console `power`: the battery and state now, the drain rate, the log's state. */
void power_log_print(void);

#ifdef __cplusplus
}
#endif
