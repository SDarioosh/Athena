#include "i2c_bus.h"

#include "esp_check.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static const char *TAG = "i2c";
static i2c_master_bus_handle_t s_bus;
static SemaphoreHandle_t s_lock;

#define I2C_TIMEOUT_MS 50

esp_err_t i2c_bus_init(int sda, int scl)
{
    s_lock = xSemaphoreCreateRecursiveMutex();
    ESP_RETURN_ON_FALSE(s_lock, ESP_ERR_NO_MEM, TAG, "mutex");
    i2c_master_bus_config_t cfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = sda,
        .scl_io_num = scl,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true, /* the board has its own pull-ups; harmless */
    };
    return i2c_new_master_bus(&cfg, &s_bus);
}

esp_err_t i2c_bus_add(uint8_t addr, i2c_dev_t *out)
{
    i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = addr,
        .scl_speed_hz = 400000,
    };
    out->addr = addr;
    return i2c_master_bus_add_device(s_bus, &cfg, &out->dev);
}

bool i2c_bus_probe(uint8_t addr)
{
    return i2c_master_probe(s_bus, addr, I2C_TIMEOUT_MS) == ESP_OK;
}

void i2c_bus_lock(void)
{
    xSemaphoreTakeRecursive(s_lock, portMAX_DELAY);
}

void i2c_bus_unlock(void)
{
    xSemaphoreGiveRecursive(s_lock);
}

esp_err_t i2c_reg_read(const i2c_dev_t *d, uint8_t reg, uint8_t *buf, size_t len)
{
    i2c_bus_lock();
    esp_err_t err = i2c_master_transmit_receive(d->dev, &reg, 1, buf, len, I2C_TIMEOUT_MS);
    i2c_bus_unlock();
    return err;
}

esp_err_t i2c_reg_write(const i2c_dev_t *d, uint8_t reg, const uint8_t *buf, size_t len)
{
    uint8_t tmp[1 + 16];
    if (len > sizeof(tmp) - 1) {
        return ESP_ERR_INVALID_SIZE;
    }
    tmp[0] = reg;
    for (size_t i = 0; i < len; i++) {
        tmp[1 + i] = buf[i];
    }
    i2c_bus_lock();
    esp_err_t err = i2c_master_transmit(d->dev, tmp, 1 + len, I2C_TIMEOUT_MS);
    i2c_bus_unlock();
    return err;
}

esp_err_t i2c_reg_read8(const i2c_dev_t *d, uint8_t reg, uint8_t *val)
{
    return i2c_reg_read(d, reg, val, 1);
}

esp_err_t i2c_reg_write8(const i2c_dev_t *d, uint8_t reg, uint8_t val)
{
    return i2c_reg_write(d, reg, &val, 1);
}

esp_err_t i2c_reg_update(const i2c_dev_t *d, uint8_t reg, uint8_t mask, uint8_t value)
{
    uint8_t cur;
    i2c_bus_lock();
    esp_err_t err = i2c_reg_read8(d, reg, &cur);
    if (err == ESP_OK) {
        const uint8_t next = (uint8_t)((cur & ~mask) | (value & mask));
        if (next != cur) {
            err = i2c_reg_write8(d, reg, next);
        }
    }
    i2c_bus_unlock();
    return err;
}
