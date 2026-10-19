#include "mic.h"

#include "audio_dsp.h"
#include "driver/i2s_pdm.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "nvs.h"

static const char *TAG = "mic";
static i2s_chan_handle_t s_rx;
static bool s_running;
static audio_dsp_t s_dsp;
static int s_gain_db = MIC_DEFAULT_GAIN_DB;
static bool s_eq_on = true;
static audio_dsp_band_t s_eq[AUDIO_DSP_MAX_EQ_BANDS];
static int s_eq_count;

/* Which PDM slot the T3902 answers on depends on how its L/R select pin is wired. LilyGoLib
 * reads the left slot on this watch. Checked on the watch (2026-10-19): the right slot, both
 * slots in stereo, the inverted clock edge and a 2.048 MHz PDM clock all gave the same level
 * as this setting, so the slot, edge and clock are fine. */
#define MIC_PDM_SLOT I2S_PDM_SLOT_LEFT

#define MIC_SETTLE_SAMPLES (MIC_SAMPLE_RATE / 4) /* 250 ms, discarded by mic_start() */

#define NVS_NAMESPACE "mic"
#define NVS_KEY_GAIN "gain_db"
#define NVS_KEY_EQ "eq_on"

static void load_settings(void)
{
    nvs_handle_t nvs;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &nvs) == ESP_OK) {
        int8_t v;
        if (nvs_get_i8(nvs, NVS_KEY_GAIN, &v) == ESP_OK && v >= AUDIO_DSP_GAIN_MIN_DB &&
            v <= AUDIO_DSP_GAIN_MAX_DB) {
            s_gain_db = v;
        }
        int8_t eq;
        if (nvs_get_i8(nvs, NVS_KEY_EQ, &eq) == ESP_OK) {
            s_eq_on = eq != 0;
        }
        nvs_close(nvs);
    }
    const board_mic_eq_band_t *bands = NULL;
    const int n = board_mic_eq(&bands);
    for (s_eq_count = 0; bands && s_eq_count < n && s_eq_count < AUDIO_DSP_MAX_EQ_BANDS; s_eq_count++) {
        s_eq[s_eq_count] = (audio_dsp_band_t){.hz = bands[s_eq_count].hz,
                                              .gain_db = bands[s_eq_count].gain_db,
                                              .q = bands[s_eq_count].q};
    }
}

static esp_err_t save_i8(const char *key, int8_t value)
{
    nvs_handle_t nvs;
    ESP_RETURN_ON_ERROR(nvs_open(NVS_NAMESPACE, NVS_READWRITE, &nvs), TAG, "nvs open");
    esp_err_t err = nvs_set_i8(nvs, key, value);
    if (err == ESP_OK) {
        err = nvs_commit(nvs);
    }
    nvs_close(nvs);
    ESP_RETURN_ON_ERROR(err, TAG, "nvs write %s", key);
    return ESP_OK;
}

esp_err_t mic_init(board_mic_pins_t pins)
{
    if (s_rx) {
        return ESP_OK;
    }
    load_settings();
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
    ESP_LOGI(TAG, "PDM RX ready: CLK GPIO%d, DATA GPIO%d, %d Hz mono 16-bit; high-pass %d Hz, "
                  "EQ %s (%d bands), gain %d dB", pins.clk_gpio, pins.data_gpio, MIC_SAMPLE_RATE,
             MIC_HIGHPASS_HZ, s_eq_on ? "on" : "off", s_eq_count, s_gain_db);
    return ESP_OK;
}

int mic_gain_db(void)
{
    return s_gain_db;
}

esp_err_t mic_set_gain_db(int gain_db)
{
    ESP_RETURN_ON_FALSE(gain_db >= AUDIO_DSP_GAIN_MIN_DB && gain_db <= AUDIO_DSP_GAIN_MAX_DB,
                        ESP_ERR_INVALID_ARG, TAG, "gain %d..%d dB", AUDIO_DSP_GAIN_MIN_DB,
                        AUDIO_DSP_GAIN_MAX_DB);
    ESP_RETURN_ON_FALSE(!s_running, ESP_ERR_INVALID_STATE, TAG, "mic in use");
    ESP_RETURN_ON_ERROR(save_i8(NVS_KEY_GAIN, (int8_t)gain_db), TAG, "save");
    s_gain_db = gain_db;
    ESP_LOGI(TAG, "gain %d dB (saved)", gain_db);
    return ESP_OK;
}

bool mic_eq_enabled(void)
{
    return s_eq_on;
}

int mic_eq_band_count(void)
{
    return s_eq_count;
}

esp_err_t mic_set_eq_enabled(bool on)
{
    ESP_RETURN_ON_FALSE(!s_running, ESP_ERR_INVALID_STATE, TAG, "mic in use");
    ESP_RETURN_ON_ERROR(save_i8(NVS_KEY_EQ, on ? 1 : 0), TAG, "save");
    s_eq_on = on;
    ESP_LOGI(TAG, "EQ %s (saved)", on ? "on" : "off");
    return ESP_OK;
}

uint64_t mic_limited_samples(void)
{
    return s_dsp.limited;
}

uint64_t mic_clipped_samples(void)
{
    return s_dsp.clipped;
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
    /* Drop the start-up transient, raw (it would only make the filter ring). Measured on the
     * T-Watch Ultra (2026-10-19): the first ~16 ms are near full scale (a click at the start
     * of every file), then the DC offset settles from ~+2600 to ~+600 over ~250 ms. */
    static int16_t settle[1024];
    for (size_t dropped = 0; dropped < MIC_SETTLE_SAMPLES;) {
        size_t bytes = 0;
        i2s_channel_read(s_rx, settle, sizeof(settle), &bytes, 500);
        if (bytes == 0) {
            break; /* let the caller's reads report the problem */
        }
        dropped += bytes / sizeof(int16_t);
    }
    audio_dsp_init(&s_dsp, MIC_SAMPLE_RATE, MIC_HIGHPASS_HZ, s_eq, s_eq_on ? s_eq_count : 0,
                   s_gain_db);
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
    audio_dsp_process(&s_dsp, buf, *got);
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
