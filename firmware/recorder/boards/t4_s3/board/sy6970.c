#include "sy6970.h"

#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "i2c_bus.h"

static const char *TAG = "sy6970";
static i2c_dev_t s_dev;
static bool s_ok;
static uint8_t s_id;

static void start_conversion(void)
{
    /* One-shot: CONV_START set, CONV_RATE clear. The bit clears itself when done. */
    i2c_reg_update(&s_dev, SY6970_REG_ADC, SY6970_ADC_CONV_START | SY6970_ADC_CONV_RATE,
                   SY6970_ADC_CONV_START);
}

esp_err_t sy6970_init(uint8_t addr, int charge_ma, bool stat_led)
{
    ESP_RETURN_ON_ERROR(i2c_bus_add(addr, &s_dev), TAG, "add");
    ESP_RETURN_ON_ERROR(i2c_reg_read8(&s_dev, SY6970_REG_ID, &s_id), TAG, "id");
    /* Watchdog off: otherwise the chip falls back to its defaults (2 A charge current) 40 s
     * after the last I2C write. */
    ESP_RETURN_ON_ERROR(i2c_reg_update(&s_dev, SY6970_REG_TIMER,
                                       SY6970_TIMER_WATCHDOG_MASK | SY6970_TIMER_STAT_DIS,
                                       stat_led ? 0 : SY6970_TIMER_STAT_DIS),
                        TAG, "watchdog");
    uint8_t ichg = 0;
    ESP_RETURN_ON_ERROR(i2c_reg_read8(&s_dev, SY6970_REG_ICHG, &ichg), TAG, "ichg");
    ESP_RETURN_ON_ERROR(i2c_reg_write8(&s_dev, SY6970_REG_ICHG, sy6970_encode_ichg(charge_ma, ichg)),
                        TAG, "ichg");
    /* Charge on, boost (5 V out on USB) off. */
    ESP_RETURN_ON_ERROR(i2c_reg_update(&s_dev, SY6970_REG_CONFIG,
                                       SY6970_CONFIG_CHG | SY6970_CONFIG_OTG, SY6970_CONFIG_CHG),
                        TAG, "config");
    s_ok = true;
    start_conversion();
    ESP_LOGI(TAG, "ready: id 0x%02X, charge current %d mA, charge LED %s", s_id,
             sy6970_charge_current_ma(), stat_led ? "on" : "off");
    return ESP_OK;
}

void sy6970_wait_adc(int timeout_ms)
{
    for (int t = 0; s_ok && t < timeout_ms; t += 20) {
        uint8_t v = 0;
        if (i2c_reg_read8(&s_dev, SY6970_REG_ADC, &v) != ESP_OK || !(v & SY6970_ADC_CONV_START)) {
            return;
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

esp_err_t sy6970_read(sy6970_reading_t *out)
{
    ESP_RETURN_ON_FALSE(s_ok, ESP_ERR_INVALID_STATE, TAG, "not found");
    uint8_t r[0x14 - SY6970_REG_STATUS + 1];
    ESP_RETURN_ON_ERROR(i2c_reg_read(&s_dev, SY6970_REG_STATUS, r, sizeof(r)), TAG, "read");
    /* REG0C latches faults: the read above cleared them; this is what was latched. */
    out->status = sy6970_decode_status(r[SY6970_REG_STATUS - SY6970_REG_STATUS]);
    out->fault = r[SY6970_REG_FAULT - SY6970_REG_STATUS];
    out->batv_mv = sy6970_decode_batv_mv(r[SY6970_REG_BATV - SY6970_REG_STATUS]);
    out->sysv_mv = sy6970_decode_sysv_mv(r[SY6970_REG_SYSV - SY6970_REG_STATUS]);
    out->vbus_mv = sy6970_decode_vbus_mv(r[SY6970_REG_VBUSV - SY6970_REG_STATUS], &out->vbus_good);
    out->ichg_now_ma = sy6970_decode_ichgr_ma(r[SY6970_REG_ICHGR - SY6970_REG_STATUS]);
    start_conversion();
    return ESP_OK;
}

int sy6970_charge_current_ma(void)
{
    uint8_t v = 0;
    if (!s_ok || i2c_reg_read8(&s_dev, SY6970_REG_ICHG, &v) != ESP_OK) {
        return -1;
    }
    return sy6970_decode_ichg_ma(v);
}

uint8_t sy6970_id(void)
{
    return s_id;
}

void sy6970_log_registers(void)
{
    if (!s_ok) {
        return;
    }
    uint8_t r[0x15];
    if (i2c_reg_read(&s_dev, 0x00, r, sizeof(r)) != ESP_OK) {
        ESP_LOGW(TAG, "register read failed");
        return;
    }
    ESP_LOGI(TAG, "regs 00-0A: %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X", r[0], r[1],
             r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10]);
    ESP_LOGI(TAG, "regs 0B-14: %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X", r[11], r[12],
             r[13], r[14], r[15], r[16], r[17], r[18], r[19], r[20]);
}
