/* SY6970 battery charger on the T4-S3 (I2C 0x6A). Register map: sy6970_codec.h. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "sy6970_codec.h"

typedef struct {
    int batv_mv;
    int sysv_mv;
    int vbus_mv;
    bool vbus_good;
    int ichg_now_ma;
    sy6970_status_t status;
    uint8_t fault; /* REG0C */
} sy6970_reading_t;

/* Watchdog off (it would reset the settings every 40 s), charging on with `charge_ma`, boost
 * off, the charge LED as `stat_led`, one ADC conversion started. */
esp_err_t sy6970_init(uint8_t addr, int charge_ma, bool stat_led);
/* The last ADC results (at most ~1 s old), then starts the next one-shot conversion. Cheap
 * on the battery: the ADC runs only once per read, not continuously. */
esp_err_t sy6970_read(sy6970_reading_t *out);
/* Waits up to timeout_ms for a conversion to finish (boot: the first reading). */
void sy6970_wait_adc(int timeout_ms);
int sy6970_charge_current_ma(void);
/* REG14 (part number and revision) */
uint8_t sy6970_id(void);
void sy6970_log_registers(void);
