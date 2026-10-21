/* RM690B0 450x600 AMOLED over QSPI, through esp_lcd_co5300 (same QSPI framing). The panel
 * is powered only between display_init() and display_deinit(); the caller switches its
 * power IC (PIN_DISP_EN). */
#pragma once

#include <stdint.h>

#include "esp_err.h"

esp_err_t display_init(void);
/* Blocks until the pixels are sent: the caller reuses its buffer at once. */
esp_err_t display_draw(int x, int y, int w, int h, const uint16_t *pixels);
/* 0..100 */
esp_err_t display_set_brightness(int percent);
esp_err_t display_deinit(void);
