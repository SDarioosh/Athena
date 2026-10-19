/* TI DRV2605 haptic driver: library effects in internal-trigger mode. */
#pragma once

#include <stdint.h>

#include "esp_err.h"

esp_err_t drv2605_init(uint8_t i2c_addr);
/* Play one library effect (1 = strong click 100 %) and wait for it to finish. */
esp_err_t drv2605_play(uint8_t effect, int timeout_ms);
esp_err_t drv2605_standby(void);
