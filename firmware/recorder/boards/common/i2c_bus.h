/* Small register-access helper over the ESP-IDF I2C master driver, with a bus mutex so
 * read-modify-write sequences from different tasks don't interleave. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "driver/i2c_master.h"
#include "esp_err.h"

typedef struct {
    i2c_master_dev_handle_t dev;
    uint8_t addr;
} i2c_dev_t;

esp_err_t i2c_bus_init(int sda, int scl);
esp_err_t i2c_bus_add(uint8_t addr, i2c_dev_t *out);
bool i2c_bus_probe(uint8_t addr);

esp_err_t i2c_reg_read(const i2c_dev_t *d, uint8_t reg, uint8_t *buf, size_t len);
esp_err_t i2c_reg_write(const i2c_dev_t *d, uint8_t reg, const uint8_t *buf, size_t len);
esp_err_t i2c_reg_read8(const i2c_dev_t *d, uint8_t reg, uint8_t *val);
esp_err_t i2c_reg_write8(const i2c_dev_t *d, uint8_t reg, uint8_t val);
/* reg = (reg & ~mask) | (value & mask), atomically with respect to the bus mutex. */
esp_err_t i2c_reg_update(const i2c_dev_t *d, uint8_t reg, uint8_t mask, uint8_t value);

/* Hold the bus across several transfers (e.g. a multi-byte ROM read). */
void i2c_bus_lock(void);
void i2c_bus_unlock(void);
