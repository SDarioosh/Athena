/* XINLUDA XL9555 16-bit I2C I/O expander (TCA9555-compatible register map).
 * Pins are linear indices 0..15: port 0 = P00..P07 -> 0..7, port 1 = P10..P17 -> 8..15. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

esp_err_t xl9555_init(uint8_t i2c_addr);
esp_err_t xl9555_set_direction(int pin, bool input);
esp_err_t xl9555_write(int pin, bool high);
/* Reads the input register (the real pin level, for inputs and outputs alike). */
esp_err_t xl9555_read(int pin, bool *high);
/* Raw 16-bit snapshots for logging. */
esp_err_t xl9555_read_all(uint16_t *inputs, uint16_t *outputs, uint16_t *directions);
