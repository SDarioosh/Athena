/* NXP PCF85063A real-time clock over I2C. The BCD codec is in pcf85063a_codec.[ch]. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "pcf85063a_codec.h"

esp_err_t pcf85063a_init(uint8_t i2c_addr);
/* *osc_stopped: the OS flag, set when the clock lost power since it was last written. */
esp_err_t pcf85063a_read(pcf85063a_datetime_t *dt, bool *osc_stopped);
esp_err_t pcf85063a_write(const pcf85063a_datetime_t *dt);
