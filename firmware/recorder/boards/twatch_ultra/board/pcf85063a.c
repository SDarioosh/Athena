#include "pcf85063a.h"

#include "esp_check.h"
#include "i2c_bus.h"

static const char *TAG = "pcf85063a";
static i2c_dev_t s_dev;

#define REG_CONTROL1 0x00 /* bit5 STOP, bit4 12/24 h (0 = 24 h), bit0 CAP_SEL */
#define REG_CONTROL2 0x01 /* bits[2:0] COF clock-out: 111 = off */

esp_err_t pcf85063a_init(uint8_t i2c_addr)
{
    ESP_RETURN_ON_ERROR(i2c_bus_add(i2c_addr, &s_dev), TAG, "add");
    /* 24 h mode, clock running; leave CAP_SEL as the board was shipped. */
    ESP_RETURN_ON_ERROR(i2c_reg_update(&s_dev, REG_CONTROL1, 0x30, 0x00), TAG, "ctrl1");
    /* CLKOUT off: saves the 32 kHz output driver (LilyGoLib does the same). */
    return i2c_reg_update(&s_dev, REG_CONTROL2, 0x07, 0x07);
}

esp_err_t pcf85063a_read(pcf85063a_datetime_t *dt, bool *osc_stopped)
{
    uint8_t regs[PCF85063A_TIME_REG_COUNT];
    ESP_RETURN_ON_ERROR(i2c_reg_read(&s_dev, PCF85063A_TIME_REG_FIRST, regs, sizeof(regs)), TAG,
                        "read");
    pcf85063a_decode(regs, dt, osc_stopped);
    return ESP_OK;
}

esp_err_t pcf85063a_write(const pcf85063a_datetime_t *dt)
{
    uint8_t regs[PCF85063A_TIME_REG_COUNT];
    ESP_RETURN_ON_FALSE(pcf85063a_encode(dt, regs), ESP_ERR_INVALID_ARG, TAG, "range");
    return i2c_reg_write(&s_dev, PCF85063A_TIME_REG_FIRST, regs, sizeof(regs));
}
