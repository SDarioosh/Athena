/* NXP PCF8563 real-time clock over I2C. The BCD codec is in pcf8563_codec.[ch]. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "pcf8563_codec.h"

esp_err_t pcf8563_init(uint8_t i2c_addr);
/* *voltage_low: the VL flag, set when the clock lost power since it was last written. */
esp_err_t pcf8563_read(pcf8563_datetime_t *dt, bool *voltage_low);
esp_err_t pcf8563_write(const pcf8563_datetime_t *dt);
