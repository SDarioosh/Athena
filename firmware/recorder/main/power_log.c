#include "power_log.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "chunk_store.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_private/esp_clk.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"

static const char *TAG = "power";

#define RING_CAP (7 * 24 * 12) /* 7 days at 5 min: 2016 x 16 B in PSRAM */
#define DRAIN_MIN_SPAN_S 1800  /* drain rate over at least 30 min on battery */
#define WRITE_BATCH 64          /* lines per card write */

static power_sample_fn_t s_sample;
static power_card_busy_fn_t s_card_busy;
static int64_t s_next_try_us; /* no write attempt before this (after a failure) */
static ps_ring_t s_ring;
static SemaphoreHandle_t s_lock;
static int s_period_min = POWER_LOG_DEFAULT_MIN;
static int64_t s_last_sample_us = INT64_MIN / 2;
static int64_t s_last_flush_us;
static uint32_t s_written, s_write_errors;

static int64_t now_us(void)
{
    return esp_timer_get_time();
}

esp_err_t power_log_init(power_sample_fn_t sample, power_card_busy_fn_t card_busy)
{
    s_sample = sample;
    s_card_busy = card_busy;
    s_lock = xSemaphoreCreateMutex();
    ps_sample_t *buf = heap_caps_calloc(RING_CAP, sizeof(ps_sample_t), MALLOC_CAP_SPIRAM);
    size_t cap = RING_CAP;
    if (!buf) {
        cap = 288; /* a day, in internal RAM */
        buf = heap_caps_calloc(cap, sizeof(ps_sample_t), MALLOC_CAP_INTERNAL);
    }
    if (!s_lock || !buf) {
        return ESP_ERR_NO_MEM;
    }
    ps_ring_init(&s_ring, buf, cap);
    nvs_handle_t h;
    if (nvs_open("power", NVS_READONLY, &h) == ESP_OK) {
        int32_t v;
        if (nvs_get_i32(h, "period_min", &v) == ESP_OK && v >= 0 && v <= 1440) {
            s_period_min = (int)v;
        }
        nvs_close(h);
    }
    s_last_flush_us = now_us();
    ESP_LOGI(TAG, "log every %d min (%s), %u samples held in RAM, written to /%s", s_period_min,
             s_period_min ? "on" : "off", (unsigned)cap, POWER_LOG_FILE);
    return ESP_OK;
}

esp_err_t power_log_set_period(int minutes)
{
    if (minutes < 0 || minutes > 1440) {
        return ESP_ERR_INVALID_ARG;
    }
    s_period_min = minutes;
    s_last_sample_us = INT64_MIN / 2; /* sample at the next poll */
    nvs_handle_t h;
    esp_err_t err = nvs_open("power", NVS_READWRITE, &h);
    if (err == ESP_OK) {
        err = nvs_set_i32(h, "period_min", minutes);
        if (err == ESP_OK) {
            err = nvs_commit(h);
        }
        nvs_close(h);
    }
    return err;
}

/* Write held samples, WRITE_BATCH lines per card access. */
static esp_err_t flush_locked(void)
{
    static char text[WRITE_BATCH * 64];
    esp_err_t err = ESP_OK;
    while (s_ring.unwritten && err == ESP_OK) {
        size_t len = 0, lines = 0;
        for (size_t i = s_ring.count - s_ring.unwritten; i < s_ring.count && lines < WRITE_BATCH;
             i++, lines++) {
            ps_sample_t s;
            ps_ring_get(&s_ring, i, &s);
            const size_t n = ps_csv_line(&s, text + len, sizeof(text) - len);
            if (!n) {
                break;
            }
            len += n;
        }
        if (!lines) {
            break;
        }
        err = chunk_store_append_file(POWER_LOG_FILE, PS_CSV_HEADER, text, len);
        if (err == ESP_OK) {
            for (size_t i = 0; i < lines; i++) {
                ps_ring_mark_written(&s_ring);
            }
            s_written += lines;
        } else {
            s_write_errors++;
            s_next_try_us = now_us() + (int64_t)POWER_LOG_RETRY_S * 1000000;
            ESP_LOGW(TAG, "writing /%s: %s (kept in RAM)", POWER_LOG_FILE, esp_err_to_name(err));
        }
    }
    s_last_flush_us = now_us();
    return err;
}

esp_err_t power_log_flush(void)
{
    if (s_card_busy && s_card_busy()) {
        return ESP_ERR_INVALID_STATE; /* recording or syncing: written later */
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    const esp_err_t err = flush_locked();
    xSemaphoreGive(s_lock);
    return err;
}

void power_log_poll(void)
{
    if (!s_lock) {
        return;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    const int64_t now = now_us();
    if (s_period_min && now - s_last_sample_us >= (int64_t)s_period_min * 60 * 1000000) {
        s_last_sample_us = now;
        ps_sample_t s;
        s_sample(&s);
        ps_ring_push(&s_ring, &s);
    }
    /* Once an hour, or before the ring wraps; never while the card is in use. */
    if (s_ring.unwritten && now >= s_next_try_us && !(s_card_busy && s_card_busy()) &&
        (now - s_last_flush_us >= (int64_t)POWER_LOG_FLUSH_S * 1000000 ||
         s_ring.unwritten >= s_ring.cap * 3 / 4)) {
        flush_locked();
    }
    xSemaphoreGive(s_lock);
}

void power_log_print(void)
{
    ps_sample_t s;
    s_sample(&s);
    char state[48];
    ps_state_name(s.flags, state, sizeof(state));
    printf("battery: %d%%, %u mV, %s, USB %s\n", s.percent, s.mv,
           (s.flags & PS_CHARGING) ? "charging" : "not charging", (s.flags & PS_USB) ? "in" : "out");
    printf("state: %s; CPU %u MHz now; up %" PRIu32 " s\n", state,
           (unsigned)(esp_clk_cpu_freq() / 1000000), s.uptime_s);
    xSemaphoreTake(s_lock, portMAX_DELAY);
    double rate = 0;
    uint32_t span = 0;
    if (ps_drain_rate(&s_ring, DRAIN_MIN_SPAN_S, &rate, &span)) {
        printf("drain: %.2f %%/h over the last %" PRIu32 " min on battery (%s)\n", rate, span / 60,
               rate > 0.01 ? "" : "no change yet");
        if (rate > 0.01) {
            printf("  -> about %.0f h from full\n", 100.0 / rate);
        }
    } else {
        printf("drain: not enough samples on battery yet (%" PRIu32 " of %d min)\n", span / 60,
               DRAIN_MIN_SPAN_S / 60);
    }
    const int64_t next_s =
        s_period_min ? ((s_last_sample_us + (int64_t)s_period_min * 60 * 1000000) - now_us()) / 1000000 : -1;
    printf("log: every %d min%s; %u samples in RAM, %u not on the card yet; %" PRIu32
           " written, %" PRIu32 " write errors; next sample in %lld s; file /%s\n",
           s_period_min, s_period_min ? "" : " (off)", (unsigned)s_ring.count,
           (unsigned)s_ring.unwritten, s_written, s_write_errors, (long long)(next_s > 0 ? next_s : 0),
           POWER_LOG_FILE);
    xSemaphoreGive(s_lock);
}
