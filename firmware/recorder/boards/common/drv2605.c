#include "drv2605.h"

#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "i2c_bus.h"

static const char *TAG = "drv2605";
static i2c_dev_t s_dev;

#define REG_STATUS 0x00
#define REG_MODE 0x01 /* 0x00 internal trigger, 0x40 standby */
#define REG_RTPIN 0x02
#define REG_LIBRARY 0x03
#define REG_WAVESEQ1 0x04
#define REG_WAVESEQ2 0x05
#define REG_GO 0x0C
#define REG_OVERDRIVE 0x0D
#define REG_SUSTAINPOS 0x0E
#define REG_SUSTAINNEG 0x0F
#define REG_BREAK 0x10
#define REG_AUDIOMAX 0x13
#define REG_FEEDBACK 0x1A /* bit7: 0 = ERM, 1 = LRA */
#define REG_CONTROL3 0x1D

esp_err_t drv2605_init(uint8_t i2c_addr)
{
    /* Called before every buzz (EN low drops the registers): add the device once. */
    if (!s_dev.dev) {
        ESP_RETURN_ON_ERROR(i2c_bus_add(i2c_addr, &s_dev), TAG, "add");
    }
    uint8_t status;
    ESP_RETURN_ON_ERROR(i2c_reg_read8(&s_dev, REG_STATUS, &status), TAG, "status");
    ESP_LOGD(TAG, "chip id %d (3 = DRV2605, 7 = DRV2605L)", status >> 5);
    /* Same bring-up as LilyGoLib/SensorLib for this watch: ERM, library 1, open loop. */
    ESP_RETURN_ON_ERROR(i2c_reg_write8(&s_dev, REG_MODE, 0x00), TAG, "mode");
    ESP_RETURN_ON_ERROR(i2c_reg_write8(&s_dev, REG_RTPIN, 0x00), TAG, "rtp");
    ESP_RETURN_ON_ERROR(i2c_reg_write8(&s_dev, REG_WAVESEQ1, 1), TAG, "seq1");
    ESP_RETURN_ON_ERROR(i2c_reg_write8(&s_dev, REG_WAVESEQ2, 0), TAG, "seq2");
    ESP_RETURN_ON_ERROR(i2c_reg_write8(&s_dev, REG_OVERDRIVE, 0), TAG, "od");
    ESP_RETURN_ON_ERROR(i2c_reg_write8(&s_dev, REG_SUSTAINPOS, 0), TAG, "sp");
    ESP_RETURN_ON_ERROR(i2c_reg_write8(&s_dev, REG_SUSTAINNEG, 0), TAG, "sn");
    ESP_RETURN_ON_ERROR(i2c_reg_write8(&s_dev, REG_BREAK, 0), TAG, "brk");
    ESP_RETURN_ON_ERROR(i2c_reg_write8(&s_dev, REG_AUDIOMAX, 0x64), TAG, "amax");
    ESP_RETURN_ON_ERROR(i2c_reg_update(&s_dev, REG_FEEDBACK, 0x80, 0x00), TAG, "erm");
    ESP_RETURN_ON_ERROR(i2c_reg_update(&s_dev, REG_CONTROL3, 0x20, 0x20), TAG, "open loop");
    return i2c_reg_write8(&s_dev, REG_LIBRARY, 1);
}

esp_err_t drv2605_play(uint8_t effect, int timeout_ms)
{
    ESP_RETURN_ON_ERROR(i2c_reg_write8(&s_dev, REG_MODE, 0x00), TAG, "mode");
    ESP_RETURN_ON_ERROR(i2c_reg_write8(&s_dev, REG_WAVESEQ1, effect), TAG, "seq1");
    ESP_RETURN_ON_ERROR(i2c_reg_write8(&s_dev, REG_WAVESEQ2, 0), TAG, "seq2");
    ESP_RETURN_ON_ERROR(i2c_reg_write8(&s_dev, REG_GO, 1), TAG, "go");
    for (int t = 0; t < timeout_ms; t += 10) {
        vTaskDelay(pdMS_TO_TICKS(10));
        uint8_t go = 1;
        if (i2c_reg_read8(&s_dev, REG_GO, &go) == ESP_OK && (go & 1) == 0) {
            return ESP_OK;
        }
    }
    return ESP_ERR_TIMEOUT;
}

esp_err_t drv2605_standby(void)
{
    return i2c_reg_write8(&s_dev, REG_MODE, 0x40);
}
