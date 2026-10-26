/* ST7789 240x240 LCD over SPI, through esp_lcd's built-in ST7789 driver. The panel is powered
 * only between display_init() and display_deinit(); the caller switches the rails. */
#pragma once

#include <stdint.h>

#include "esp_err.h"

esp_err_t display_init(void);
/* Blocks until the pixels are sent: the caller reuses its buffer at once. */
esp_err_t display_draw(int x, int y, int w, int h, const uint16_t *pixels);
/* 0 = backlight off, anything else = on (the backlight is switched, not dimmed). */
esp_err_t display_set_brightness(int percent);
esp_err_t display_deinit(void);
