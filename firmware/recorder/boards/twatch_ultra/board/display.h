/* CO5300 AMOLED over QSPI, through the esp_lcd_co5300 component. The panel is powered only
 * between display_init() and display_deinit(); the caller switches the rail and enable. */
#pragma once

#include <stdint.h>

#include "esp_err.h"

esp_err_t display_init(void);
esp_err_t display_draw(int x, int y, int w, int h, const uint16_t *pixels);
esp_err_t display_set_brightness(int percent);
esp_err_t display_deinit(void);
