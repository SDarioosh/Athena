#include "mic.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "audio_dsp.h"
#include "driver/gpio.h"
#include "driver/i2s_pdm.h"
#include "driver/i2s_std.h"
#include "esp_attr.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_rom_gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"
#include "nvs.h"
#include "soc/i2s_periph.h"

static const char *TAG = "mic";
static i2s_chan_handle_t s_rx;  /* PDM, or the first I2S pair (I2S0, clock master) */
static i2s_chan_handle_t s_rx2; /* second I2S pair (I2S1, clock slave), or NULL */
static board_mic_pins_t s_pins;
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
#define NVS_KEY_MASK "mask"

/* ---- I2S mic array (BOARD_MIC_I2S) ----
 * Stereo frames of 32-bit slots: 8 bytes. 480 frames per DMA buffer = 30 ms and 3840 bytes
 * (a DMA buffer holds at most 4092); eight per port give the writer 240 ms of slack. */
#define I2S_FRAME_BYTES 8
#define I2S_DMA_FRAMES 480
#define I2S_DMA_DESC 8
#define I2S_READ_FRAMES 256
#define PDM_DMA_FRAMES 1024 /* 64 ms */
#define PDM_DMA_DESC 6
static int s_nports;      /* ports configured: 1 or 2 */
static int s_nports_live; /* ports delivering audio since mic_start() */
static uint8_t s_mask_setting = MIC_MASK_AUTO; /* NVS */
static uint8_t s_alive;   /* mics that answered at mic_start() */
static uint8_t s_mask;    /* mics mixed since mic_start() */
static int32_t *s_raw[MIC_MIX_MAX_PORTS]; /* I2S_READ_FRAMES stereo frames each */
static bool s_stats_on;
static mic_ch_stats_t s_stats[MIC_MIX_MAX_CH];
static uint32_t s_b_short; /* reads where pair B delivered less than pair A */
static volatile uint32_t s_overflows[MIC_MIX_MAX_PORTS]; /* DMA buffers dropped (reader late) */

/* The array's reader task empties the DMA buffers into a ring of mixed mono samples in PSRAM;
 * mic_read() takes from the ring. The recorder reads, encodes and writes to flash in one
 * loop, and a flash write (wear-levelled FAT: sector erases) can block it for longer than
 * the DMA buffers last (measured on the T4-S3, 2026-10-21: ~1 s lost at every session start,
 * then ~30 ms a second). The reader only waits for the I2S driver, so a slow writer now
 * delays the recorder, not the audio. The recorder fell more than 2 s behind now and then
 * (closing a 30 s chunk); 8 s of ring (256 KB of the 8 MB PSRAM) covers it. */
#define RING_SAMPLES (8 * MIC_SAMPLE_RATE)
#define READER_PRIORITY 10 /* above the session task (8) */
#define READER_STACK 3072
static StreamBufferHandle_t s_ring;
static StaticStreamBuffer_t s_ring_struct;
static TaskHandle_t s_reader;
static SemaphoreHandle_t s_reader_idle;
static volatile bool s_reader_run;
static volatile uint32_t s_ring_drops; /* blocks dropped: the ring was full */
static volatile uint64_t s_produced;    /* samples into the ring since mic_start() */
static uint64_t s_consumed;             /* samples mic_read() handed out */
static size_t s_max_backlog;            /* most bytes waiting in the ring */

static bool IRAM_ATTR on_overflow(i2s_chan_handle_t handle, i2s_event_data_t *event, void *ctx)
{
    (void)handle;
    (void)event;
    (*(volatile uint32_t *)ctx)++; /* ctx: its s_overflows[] entry (internal RAM, as IRAM-safe I2S wants) */
    return false;
}

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
        uint8_t mask;
        if (nvs_get_u8(nvs, NVS_KEY_MASK, &mask) == ESP_OK) {
            s_mask_setting = mask;
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

static esp_err_t save_u8(const char *key, uint8_t value)
{
    nvs_handle_t nvs;
    ESP_RETURN_ON_ERROR(nvs_open(NVS_NAMESPACE, NVS_READWRITE, &nvs), TAG, "nvs open");
    esp_err_t err = nvs_set_u8(nvs, key, value);
    if (err == ESP_OK) {
        err = nvs_commit(nvs);
    }
    nvs_close(nvs);
    ESP_RETURN_ON_ERROR(err, TAG, "nvs write %s", key);
    return ESP_OK;
}

static esp_err_t init_pdm(board_mic_pins_t pins)
{
    /* PDM RX exists on I2S0 only (ESP32-S3). 1024 frames per DMA buffer = 64 ms; six of
     * them give the writer ~380 ms of slack during an SD card write stall. */
    i2s_chan_config_t chan = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan.dma_desc_num = PDM_DMA_DESC;
    chan.dma_frame_num = PDM_DMA_FRAMES;
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
    /* Count DMA buffers lost when the writer falls behind (flash or card stalls), as the
     * array does: mic_stop() logs them and mic_overflows() reports them. */
    const i2s_event_callbacks_t cbs = {.on_recv_q_ovf = on_overflow};
    i2s_channel_register_event_callback(s_rx, &cbs, (void *)&s_overflows[0]);
    ESP_LOGI(TAG, "PDM RX ready: CLK GPIO%d, DATA GPIO%d, %d Hz mono 16-bit; high-pass %d Hz, "
                  "EQ %s (%d bands), gain %d dB", pins.clk_gpio, pins.data_gpio, MIC_SAMPLE_RATE,
             MIC_HIGHPASS_HZ, s_eq_on ? "on" : "off", s_eq_count, s_gain_db);
    return ESP_OK;
}

/* Philips I2S, 32-bit stereo slots, 16 kHz: BCLK = 64 x 16 kHz = 1.024 MHz (an INMP441 takes
 * 0.5..3.2 MHz at 64 clocks per frame). */
static i2s_std_config_t std_config(int bclk, int ws, int din)
{
    i2s_std_config_t cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(MIC_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = bclk,
            .ws = ws,
            .dout = I2S_GPIO_UNUSED,
            .din = din,
        },
    };
    return cfg;
}

static esp_err_t new_rx(i2s_port_t port, i2s_role_t role, i2s_chan_handle_t *out)
{
    i2s_chan_config_t chan = I2S_CHANNEL_DEFAULT_CONFIG(port, role);
    chan.dma_desc_num = I2S_DMA_DESC;
    chan.dma_frame_num = I2S_DMA_FRAMES;
    chan.auto_clear = false;
    return i2s_new_channel(&chan, NULL, out);
}

static esp_err_t init_reader(void);

static esp_err_t init_i2s(board_mic_pins_t pins)
{
    s_nports = pins.data2_gpio >= 0 ? 2 : 1;
    for (int p = 0; p < s_nports; p++) {
        s_raw[p] = heap_caps_malloc(I2S_READ_FRAMES * I2S_FRAME_BYTES,
                                    MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
        ESP_RETURN_ON_FALSE(s_raw[p], ESP_ERR_NO_MEM, TAG, "buffer");
    }
    /* The second pair first, as a slave on the same BCLK/WS pins: its init routes the pins
     * into I2S1 as inputs. I2S0's init then drives them as outputs; the GPIO matrix keeps the
     * input path, so I2S1 samples on the very clock edges I2S0 makes. */
    if (s_nports == 2) {
        ESP_RETURN_ON_ERROR(new_rx(I2S_NUM_1, I2S_ROLE_SLAVE, &s_rx2), TAG, "i2s1 channel");
        i2s_std_config_t cfg = std_config(pins.clk_gpio, pins.ws_gpio, pins.data2_gpio);
        ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(s_rx2, &cfg), TAG, "i2s1 std");
    }
    ESP_RETURN_ON_ERROR(new_rx(I2S_NUM_0, I2S_ROLE_MASTER, &s_rx), TAG, "i2s0 channel");
    i2s_std_config_t cfg = std_config(pins.clk_gpio, pins.ws_gpio, pins.data_gpio);
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(s_rx, &cfg), TAG, "i2s0 std");
    const i2s_event_callbacks_t cbs0 = {.on_recv_q_ovf = on_overflow};
    i2s_channel_register_event_callback(s_rx, &cbs0, (void *)&s_overflows[0]);
    if (s_nports == 2) {
        i2s_channel_register_event_callback(s_rx2, &cbs0, (void *)&s_overflows[1]);
    }
    if (s_nports == 2) {
        /* Belt and braces: the clock pins stay inputs of I2S1 as well. */
        gpio_input_enable(pins.clk_gpio);
        gpio_input_enable(pins.ws_gpio);
        esp_rom_gpio_connect_in_signal(pins.clk_gpio, i2s_periph_signal[1].s_rx_bck_sig, false);
        esp_rom_gpio_connect_in_signal(pins.ws_gpio, i2s_periph_signal[1].s_rx_ws_sig, false);
    }
    /* A slot no mic drives then reads 0 (mic_mix_alive). INMP441 data sheet: 100k to GND. */
    gpio_pulldown_en(pins.data_gpio);
    if (s_nports == 2) {
        gpio_pulldown_en(pins.data2_gpio);
    }
    ESP_RETURN_ON_ERROR(init_reader(), TAG, "reader");
    ESP_LOGI(TAG, "I2S mic array ready: BCLK GPIO%d, WS GPIO%d, data GPIO%d (pair A) and GPIO%d "
                  "(pair B), %d Hz; high-pass %d Hz, EQ %s (%d bands), gain %d dB, mics %s",
             pins.clk_gpio, pins.ws_gpio, pins.data_gpio, s_nports == 2 ? pins.data2_gpio : -1,
             MIC_SAMPLE_RATE, MIC_HIGHPASS_HZ, s_eq_on ? "on" : "off", s_eq_count, s_gain_db,
             s_mask_setting == MIC_MASK_AUTO ? "auto" : "fixed mask");
    return ESP_OK;
}

static size_t read_ports(size_t frames, int timeout_ms, esp_err_t *err);

static void reader_task(void *arg)
{
    (void)arg;
    static int16_t mono[I2S_READ_FRAMES];
    for (;;) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY); /* mic_start() */
        while (s_reader_run) {
            esp_err_t err;
            const size_t n = read_ports(I2S_READ_FRAMES, 100, &err);
            if (n == 0) {
                continue; /* no clock: mic_read() times out and the caller reports it */
            }
            if (s_stats_on) {
                mic_mix_stats_add(s_stats, (const int32_t *const *)s_raw, s_nports_live, n);
            }
            mic_mix_frames((const int32_t *const *)s_raw, s_nports_live, n, s_mask, mono);
            /* Whole blocks only: a partial write would split a sample. */
            if (xStreamBufferSpacesAvailable(s_ring) >= n * sizeof(int16_t)) {
                xStreamBufferSend(s_ring, mono, n * sizeof(int16_t), 0);
                s_produced += n;
                const size_t waiting = xStreamBufferBytesAvailable(s_ring);
                if (waiting > s_max_backlog) {
                    s_max_backlog = waiting;
                }
            } else {
                s_ring_drops++;
            }
        }
        xSemaphoreGive(s_reader_idle);
    }
}

static esp_err_t init_reader(void)
{
    const size_t bytes = RING_SAMPLES * sizeof(int16_t);
    /* +1: a stream buffer's storage is one byte larger than its capacity. */
    uint8_t *storage = heap_caps_malloc(bytes + 1, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    ESP_RETURN_ON_FALSE(storage, ESP_ERR_NO_MEM, TAG, "ring");
    s_ring = xStreamBufferCreateStatic(bytes, 1, storage, &s_ring_struct);
    s_reader_idle = xSemaphoreCreateBinary();
    ESP_RETURN_ON_FALSE(s_ring && s_reader_idle, ESP_ERR_NO_MEM, TAG, "ring");
    ESP_RETURN_ON_FALSE(xTaskCreate(reader_task, "mic_reader", READER_STACK, NULL, READER_PRIORITY,
                                    &s_reader) == pdPASS,
                        ESP_ERR_NO_MEM, TAG, "reader task");
    return ESP_OK;
}

esp_err_t mic_init(board_mic_pins_t pins)
{
    if (s_rx) {
        return ESP_OK;
    }
    load_settings();
    s_pins = pins;
    return pins.kind == BOARD_MIC_I2S ? init_i2s(pins) : init_pdm(pins);
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

/* ---- I2S mic array ---- */

int mic_array_channels(void)
{
    return s_pins.kind == BOARD_MIC_I2S ? s_nports * 2 : 0;
}

uint8_t mic_array_alive_mask(void)
{
    return s_alive;
}

uint8_t mic_array_mask(void)
{
    return s_mask;
}

int mic_array_pairs_live(void)
{
    return s_pins.kind == BOARD_MIC_I2S && s_running ? s_nports_live : 0;
}

uint32_t mic_overflows(void)
{
    return s_overflows[0] + s_overflows[1] + s_ring_drops;
}

uint32_t mic_array_pair_b_short_reads(void)
{
    return s_b_short;
}

uint8_t mic_array_mask_setting(void)
{
    return s_mask_setting;
}

esp_err_t mic_array_set_mask(uint8_t mask)
{
    ESP_RETURN_ON_FALSE(s_pins.kind == BOARD_MIC_I2S, ESP_ERR_NOT_SUPPORTED, TAG, "no mic array");
    ESP_RETURN_ON_FALSE(!s_running, ESP_ERR_INVALID_STATE, TAG, "mic in use");
    ESP_RETURN_ON_ERROR(save_u8(NVS_KEY_MASK, mask), TAG, "save");
    s_mask_setting = mask;
    ESP_LOGI(TAG, "mics: %s (saved)", mask == MIC_MASK_AUTO ? "auto" : "fixed mask");
    return ESP_OK;
}

void mic_array_stats_enable(bool on)
{
    if (on) {
        mic_mix_stats_reset(s_stats);
    }
    s_stats_on = on;
}

void mic_array_stats(mic_ch_stats_t out[MIC_MIX_MAX_CH])
{
    memcpy(out, s_stats, sizeof(s_stats));
}

/* Read up to `frames` stereo frames from every live port into s_raw. Returns the frames the
 * first pair delivered; a second pair that falls short is padded with silence. */
static size_t read_ports(size_t frames, int timeout_ms, esp_err_t *err)
{
    size_t bytes = 0;
    *err = i2s_channel_read(s_rx, s_raw[0], frames * I2S_FRAME_BYTES, &bytes, timeout_ms);
    const size_t got = bytes / I2S_FRAME_BYTES;
    if (s_nports_live == 2 && got > 0) {
        size_t b2 = 0;
        i2s_channel_read(s_rx2, s_raw[1], got * I2S_FRAME_BYTES, &b2, timeout_ms);
        if (b2 < got * I2S_FRAME_BYTES) {
            s_b_short++;
            memset((uint8_t *)s_raw[1] + b2, 0, got * I2S_FRAME_BYTES - b2);
        }
    }
    return got;
}

static const char *const CH_NAME[MIC_MIX_MAX_CH] = {"A-L", "A-R", "B-L", "B-R"};

const char *mic_array_channel_name(int ch)
{
    return ch >= 0 && ch < MIC_MIX_MAX_CH ? CH_NAME[ch] : "?";
}

/* Drop the start-up transient (an INMP441 needs 2^18 clocks, ~260 ms, to wake up) while
 * finding out which mics answer: 500 ms in all, stats over the second half. */
static void i2s_settle(void)
{
    static mic_ch_stats_t st[MIC_MIX_MAX_CH];
    mic_mix_stats_reset(st);
    size_t total = 0;
    while (total < 2 * MIC_SETTLE_SAMPLES) {
        esp_err_t err;
        const size_t got = read_ports(I2S_READ_FRAMES, 500, &err);
        if (got == 0) {
            ESP_LOGE(TAG, "no I2S clock or data: %s", esp_err_to_name(err));
            break;
        }
        if (s_nports_live == 2 && s_b_short) {
            /* I2S1 fell behind I2S0: the shared BCLK/WS doesn't reach it. Drop it now (every
             * further read would wait out its timeout). */
            ESP_LOGE(TAG, "pair B (I2S1) gets no clock: recording pair A only");
            i2s_channel_disable(s_rx2);
            s_nports_live = 1;
        }
        total += got;
        if (total > MIC_SETTLE_SAMPLES) {
            mic_mix_stats_add(st, (const int32_t *const *)s_raw, s_nports_live, got);
        }
    }
    s_alive = mic_mix_alive_mask(st, s_nports_live);
    const uint8_t present = (uint8_t)((1u << (s_nports_live * 2)) - 1u);
    s_mask = (s_mask_setting == MIC_MASK_AUTO ? s_alive : s_mask_setting) & present;
    char found[48] = "";
    for (int ch = 0; ch < s_nports_live * 2; ch++) {
        char one[12];
        snprintf(one, sizeof(one), "%s%s %s", ch ? ", " : "", CH_NAME[ch],
                 (s_alive >> ch) & 1 ? "ok" : "--");
        strncat(found, one, sizeof(found) - strlen(found) - 1);
    }
    if (s_mask == 0) {
        ESP_LOGW(TAG, "mics: %s; none to mix, recording silence (wiring: boards/<board>/pins.h)",
                 found);
    } else {
        ESP_LOGI(TAG, "mics: %s; mixing %d", found, mic_mix_count(s_mask));
    }
}

esp_err_t mic_start(void)
{
    ESP_RETURN_ON_FALSE(s_rx, ESP_ERR_INVALID_STATE, TAG, "init first");
    /* One user at a time: the recorder or the console's `level`. */
    bool expected = false;
    ESP_RETURN_ON_FALSE(__atomic_compare_exchange_n(&s_running, &expected, true, false,
                                                    __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE),
                        ESP_ERR_INVALID_STATE, TAG, "mic in use");
    /* The clock slave first, so it is listening when the master's first frame starts. */
    s_nports_live = 1;
    s_b_short = 0;
    s_overflows[0] = s_overflows[1] = 0;
    s_ring_drops = 0;
    s_produced = 0;
    s_consumed = 0;
    s_max_backlog = 0;
    if (s_rx2) {
        const esp_err_t err2 = i2s_channel_enable(s_rx2);
        if (err2 == ESP_OK) {
            s_nports_live = 2;
        } else {
            ESP_LOGE(TAG, "enable I2S1 (pair B): %s; pair A only", esp_err_to_name(err2));
        }
    }
    const esp_err_t err = i2s_channel_enable(s_rx);
    if (err != ESP_OK) {
        if (s_nports_live == 2) {
            i2s_channel_disable(s_rx2);
        }
        __atomic_store_n(&s_running, false, __ATOMIC_RELEASE);
        ESP_LOGE(TAG, "enable: %s", esp_err_to_name(err));
        return err;
    }
    if (s_pins.kind == BOARD_MIC_I2S) {
        i2s_settle();
        xStreamBufferReset(s_ring);
        s_reader_run = true;
        xTaskNotifyGive(s_reader);
    } else {
        /* Drop the start-up transient, raw (it would only make the filter ring). Measured on
         * the T-Watch Ultra (2026-10-19): the first ~16 ms are near full scale (a click at the
         * start of every file), then the DC offset settles from ~+2600 to ~+600 over ~250 ms. */
        static int16_t settle[1024];
        for (size_t dropped = 0; dropped < MIC_SETTLE_SAMPLES;) {
            size_t bytes = 0;
            i2s_channel_read(s_rx, settle, sizeof(settle), &bytes, 500);
            if (bytes == 0) {
                break; /* let the caller's reads report the problem */
            }
            dropped += bytes / sizeof(int16_t);
        }
    }
    audio_dsp_init(&s_dsp, MIC_SAMPLE_RATE, MIC_HIGHPASS_HZ, s_eq, s_eq_on ? s_eq_count : 0,
                   s_gain_db);
    return ESP_OK;
}

static esp_err_t read_array(int16_t *buf, size_t max_samples, size_t *got, int timeout_ms)
{
    const size_t bytes = xStreamBufferReceive(s_ring, buf, max_samples * sizeof(int16_t),
                                              pdMS_TO_TICKS(timeout_ms));
    *got = bytes / sizeof(int16_t);
    s_consumed += *got;
    return *got ? ESP_OK : ESP_ERR_TIMEOUT;
}

esp_err_t mic_read(int16_t *buf, size_t max_samples, size_t *got, int timeout_ms)
{
    ESP_RETURN_ON_FALSE(s_running, ESP_ERR_INVALID_STATE, TAG, "not running");
    esp_err_t err;
    if (s_pins.kind == BOARD_MIC_I2S) {
        err = read_array(buf, max_samples, got, timeout_ms);
    } else {
        size_t bytes = 0;
        /* i2s_channel_read() takes milliseconds, not ticks. */
        err = i2s_channel_read(s_rx, buf, max_samples * sizeof(int16_t), &bytes, timeout_ms);
        *got = bytes / sizeof(int16_t);
    }
    audio_dsp_process(&s_dsp, buf, *got);
    return err;
}

esp_err_t mic_stop(void)
{
    if (!__atomic_load_n(&s_running, __ATOMIC_ACQUIRE)) {
        return ESP_OK;
    }
    /* s_running stays true until the channels are off: a mic_start() from another task (the
     * console's `level` while a session stops) gets "mic in use" instead of resetting the
     * counters and s_nports_live under this stop, which then left I2S1 enabled. */
    if (s_pins.kind == BOARD_MIC_I2S && s_reader_run) {
        s_reader_run = false;
        xSemaphoreTake(s_reader_idle, pdMS_TO_TICKS(1000)); /* its read waits <= 100 ms */
        ESP_LOGI(TAG, "array: %" PRIu64 " ms read, %" PRIu64 " ms used, %u ms left in the ring "
                      "(most ever waiting: %u ms of %u)",
                 s_produced * 1000 / MIC_SAMPLE_RATE, s_consumed * 1000 / MIC_SAMPLE_RATE,
                 (unsigned)(xStreamBufferBytesAvailable(s_ring) / sizeof(int16_t) * 1000 /
                            MIC_SAMPLE_RATE),
                 (unsigned)(s_max_backlog / sizeof(int16_t) * 1000 / MIC_SAMPLE_RATE),
                 (unsigned)(RING_SAMPLES * 1000 / MIC_SAMPLE_RATE));
    }
    if (s_overflows[0] || s_overflows[1] || s_ring_drops) {
        const unsigned dma_ms =
            (s_pins.kind == BOARD_MIC_I2S ? I2S_DMA_FRAMES : PDM_DMA_FRAMES) * 1000 / MIC_SAMPLE_RATE;
        ESP_LOGW(TAG, "audio lost: %" PRIu32 " + %" PRIu32 " DMA buffers (%u ms each), %" PRIu32
                      " ring blocks (16 ms each)", s_overflows[0], s_overflows[1], dma_ms,
                 s_ring_drops);
    }
    const esp_err_t err = i2s_channel_disable(s_rx); /* the clock master first */
    if (s_nports_live == 2) {
        i2s_channel_disable(s_rx2);
    }
    __atomic_store_n(&s_running, false, __ATOMIC_RELEASE);
    return err;
}
