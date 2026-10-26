#include "pcf8563.h"

#include "esp_check.h"
#include "i2c_bus.h"

static const char *TAG = "pcf8563";
static i2c_dev_t s_dev;

#define REG_CONTROL1 0x00 /* bit5 STOP, bit3 TESTC */
#define REG_CONTROL2 0x01 /* bit1 AIE, bit0 TIE: alarm and timer interrupts */
#define REG_CLKOUT 0x0D   /* bit7 FE: clock output enable */
#define REG_TIMER_CTRL 0x0E /* bit7 TE: countdown timer enable */

esp_err_t pcf8563_init(uint8_t i2c_addr)
{
    ESP_RETURN_ON_ERROR(i2c_bus_add(i2c_addr, &s_dev), TAG, "add");
    /* Clock running, normal mode. */
    ESP_RETURN_ON_ERROR(i2c_reg_update(&s_dev, REG_CONTROL1, 0x28, 0x00), TAG, "ctrl1");
    /* No alarm or timer interrupts (its INT line is unused). */
    ESP_RETURN_ON_ERROR(i2c_reg_update(&s_dev, REG_CONTROL2, 0x03, 0x00), TAG, "ctrl2");
    ESP_RETURN_ON_ERROR(i2c_reg_update(&s_dev, REG_TIMER_CTRL, 0x80, 0x00), TAG, "timer");
    /* CLKOUT off: saves the backup cell (LilyGoLib does the same). */
    return i2c_reg_update(&s_dev, REG_CLKOUT, 0x80, 0x00);
}

esp_err_t pcf8563_read(pcf8563_datetime_t *dt, bool *voltage_low)
{
    uint8_t regs[PCF8563_TIME_REG_COUNT];
    ESP_RETURN_ON_ERROR(i2c_reg_read(&s_dev, PCF8563_TIME_REG_FIRST, regs, sizeof(regs)), TAG,
                        "read");
    pcf8563_decode(regs, dt, voltage_low);
    return ESP_OK;
}

esp_err_t pcf8563_write(const pcf8563_datetime_t *dt)
{
    uint8_t regs[PCF8563_TIME_REG_COUNT];
    ESP_RETURN_ON_FALSE(pcf8563_encode(dt, regs), ESP_ERR_INVALID_ARG, TAG, "range");
    return i2c_reg_write(&s_dev, PCF8563_TIME_REG_FIRST, regs, sizeof(regs));
}
