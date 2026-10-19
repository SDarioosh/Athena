#include "mic.h"

#include "driver/i2s_pdm.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"

static const char *TAG = "mic";
static i2s_chan_handle_t s_rx;
static bool s_running;

/* Which PDM slot the T3902 answers on depends on how its L/R select pin is wired. LilyGoLib
 * reads the left slot on this watch, and `level` on the console shows real audio with it
 * (2026-10-19; switching to the right slot at runtime gave the same data in mono mode). */
#define MIC_PDM_SLOT I2S_PDM_SLOT_LEFT

#define MIC_SETTLE_SAMPLES (MIC_SAMPLE_RATE / 4) /* 250 ms, discarded by mic_start() */

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
    /* One user at a time: the recorder or the console's `level`. */
    bool expected = false;
    ESP_RETURN_ON_FALSE(__atomic_compare_exchange_n(&s_running, &expected, true, false,
                                                    __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE),
                        ESP_ERR_INVALID_STATE, TAG, "mic in use");
    const esp_err_t err = i2s_channel_enable(s_rx);
    if (err != ESP_OK) {
        __atomic_store_n(&s_running, false, __ATOMIC_RELEASE);
        ESP_LOGE(TAG, "enable: %s", esp_err_to_name(err));
        return err;
    }
    /* Drop the start-up transient. Measured on the T-Watch Ultra (2026-10-19): the first
     * ~16 ms are near full scale (a click at the start of every file), then the DC offset
     * settles from ~+2600 to ~+540 over ~250 ms. The S3 has no PDM RX high-pass filter, so
     * the remaining offset stays in the data. */
    static int16_t settle[1024];
    for (size_t dropped = 0; dropped < MIC_SETTLE_SAMPLES;) {
        size_t got = 0;
        mic_read(settle, sizeof(settle) / sizeof(settle[0]), &got, 500);
        if (got == 0) {
            break; /* let the caller's reads report the problem */
        }
        dropped += got;
    }
    return ESP_OK;
}

esp_err_t mic_read(int16_t *buf, size_t max_samples, size_t *got, int timeout_ms)
{
    ESP_RETURN_ON_FALSE(s_running, ESP_ERR_INVALID_STATE, TAG, "not running");
    size_t bytes = 0;
    /* i2s_channel_read() takes milliseconds, not ticks. */
    const esp_err_t err =
        i2s_channel_read(s_rx, buf, max_samples * sizeof(int16_t), &bytes, timeout_ms);
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
