#include "mic.h"

#include "driver/i2s_pdm.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"

static const char *TAG = "mic";
static i2s_chan_handle_t s_rx;
static bool s_running;

/* Which PDM slot the T3902 answers on depends on how its L/R select pin is wired. LilyGoLib
 * reads the left slot on this watch. If a recording is silent, try I2S_PDM_SLOT_RIGHT. */
#define MIC_PDM_SLOT I2S_PDM_SLOT_LEFT

esp_err_t mic_init(board_mic_pins_t pins)
{
    if (s_rx) {
        return ESP_OK;
    }
    /* PDM RX exists on I2S0 only (ESP32-S3). 1024 frames per DMA buffer = 64 ms; six of
     * them give the writer ~380 ms of slack during an SD card write stall. */
    i2s_chan_config_t chan = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan.dma_desc_num = 6;
    chan.dma_frame_num = 1024;
    chan.auto_clear = false;
    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan, NULL, &s_rx), TAG, "channel");

    i2s_pdm_rx_config_t cfg = {
        .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(MIC_SAMPLE_RATE),
        .slot_cfg = I2S_PDM_RX_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .clk = pins.clk_gpio,
            .din = pins.data_gpio,
            .invert_flags = {.clk_inv = false},
        },
    };
    cfg.slot_cfg.slot_mask = MIC_PDM_SLOT;
    ESP_RETURN_ON_ERROR(i2s_channel_init_pdm_rx_mode(s_rx, &cfg), TAG, "pdm rx");
    ESP_LOGI(TAG, "PDM RX ready: CLK GPIO%d, DATA GPIO%d, %d Hz mono 16-bit", pins.clk_gpio,
             pins.data_gpio, MIC_SAMPLE_RATE);
    return ESP_OK;
}

esp_err_t mic_start(void)
{
    ESP_RETURN_ON_FALSE(s_rx, ESP_ERR_INVALID_STATE, TAG, "init first");
    if (s_running) {
        return ESP_OK;
    }
    ESP_RETURN_ON_ERROR(i2s_channel_enable(s_rx), TAG, "enable");
    s_running = true;
    return ESP_OK;
}

esp_err_t mic_read(int16_t *buf, size_t max_samples, size_t *got, int timeout_ms)
{
    ESP_RETURN_ON_FALSE(s_running, ESP_ERR_INVALID_STATE, TAG, "not running");
    size_t bytes = 0;
    const esp_err_t err = i2s_channel_read(s_rx, buf, max_samples * sizeof(int16_t), &bytes,
                                           pdMS_TO_TICKS(timeout_ms));
    *got = bytes / sizeof(int16_t);
    return err;
}

esp_err_t mic_stop(void)
{
    if (!s_running) {
        return ESP_OK;
    }
    s_running = false;
    return i2s_channel_disable(s_rx);
}
