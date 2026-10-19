#include "xl9555.h"

#include "esp_check.h"
#include "i2c_bus.h"

static const char *TAG = "xl9555";
static i2c_dev_t s_dev;

#define REG_INPUT0 0x00
#define REG_OUTPUT0 0x02
#define REG_CONFIG0 0x06 /* 1 = input */

esp_err_t xl9555_init(uint8_t i2c_addr)
{
    ESP_RETURN_ON_ERROR(i2c_bus_add(i2c_addr, &s_dev), TAG, "add");
    uint8_t probe;
    return i2c_reg_read8(&s_dev, REG_INPUT0, &probe);
}

static esp_err_t update_bit(uint8_t base, int pin, bool set)
{
    ESP_RETURN_ON_FALSE(pin >= 0 && pin < 16, ESP_ERR_INVALID_ARG, TAG, "pin");
    const uint8_t reg = (uint8_t)(base + (pin >> 3));
    const uint8_t mask = (uint8_t)(1u << (pin & 7));
    return i2c_reg_update(&s_dev, reg, mask, set ? mask : 0);
}

esp_err_t xl9555_set_direction(int pin, bool input)
{
    return update_bit(REG_CONFIG0, pin, input);
}

esp_err_t xl9555_write(int pin, bool high)
{
    return update_bit(REG_OUTPUT0, pin, high);
}

esp_err_t xl9555_read(int pin, bool *high)
{
    ESP_RETURN_ON_FALSE(pin >= 0 && pin < 16, ESP_ERR_INVALID_ARG, TAG, "pin");
    uint8_t val;
    ESP_RETURN_ON_ERROR(i2c_reg_read8(&s_dev, (uint8_t)(REG_INPUT0 + (pin >> 3)), &val), TAG, "rd");
    *high = (val >> (pin & 7)) & 1u;
    return ESP_OK;
}

esp_err_t xl9555_read_all(uint16_t *inputs, uint16_t *outputs, uint16_t *directions)
{
    uint8_t in[2], out[2], cfg[2];
    ESP_RETURN_ON_ERROR(i2c_reg_read(&s_dev, REG_INPUT0, in, 2), TAG, "in");
    ESP_RETURN_ON_ERROR(i2c_reg_read(&s_dev, REG_OUTPUT0, out, 2), TAG, "out");
    ESP_RETURN_ON_ERROR(i2c_reg_read(&s_dev, REG_CONFIG0, cfg, 2), TAG, "cfg");
    *inputs = (uint16_t)(in[0] | (in[1] << 8));
    *outputs = (uint16_t)(out[0] | (out[1] << 8));
    *directions = (uint16_t)(cfg[0] | (cfg[1] << 8));
    return ESP_OK;
}
