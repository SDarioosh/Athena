#include "recorder.h"

#include <stdio.h>
#include <string.h>
#include <sys/time.h>

#include "board.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "isotime.h"
#include "mic.h"
#include "wav.h"

static const char *TAG = "recorder";

static recorder_state_t s_state;
static recorder_done_cb_t s_done;
static recorder_tick_cb_t s_tick;
static void *s_ctx;
static int s_seconds;

#define READ_SAMPLES 1024              /* 64 ms per mic read */
#define WRITE_CHUNK_BYTES (16 * 1024)  /* one fwrite per 16 KB: large sequential card writes */

static void make_path(char *out, size_t len, const char *mount)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    if (isotime_is_plausible(tv.tv_sec)) {
        isotime_fields_t f;
        isotime_to_fields(tv.tv_sec, &f);
        snprintf(out, len, "%s/rec_%04d%02d%02d_%02d%02d%02d.wav", mount, f.year, f.month, f.day,
                 f.hour, f.minute, f.second);
    } else {
        snprintf(out, len, "%s/rec_unset_%lu.wav", mount, (unsigned long)(tv.tv_sec));
    }
}

static esp_err_t record(const char *path, int seconds, uint8_t *wbuf)
{
    FILE *f = fopen(path, "wb");
    ESP_RETURN_ON_FALSE(f, ESP_FAIL, TAG, "open %s", path);

    uint8_t header[WAV_HEADER_SIZE];
    wav_header_write(header, MIC_SAMPLE_RATE, MIC_CHANNELS, MIC_BITS, 0);
    esp_err_t err = fwrite(header, 1, sizeof(header), f) == sizeof(header) ? ESP_OK : ESP_FAIL;

    const uint32_t total_bytes = wav_data_bytes(MIC_SAMPLE_RATE, MIC_CHANNELS, MIC_BITS, seconds);
    uint32_t written = 0, pending = 0, next_tick = MIC_SAMPLE_RATE * 2;
    size_t dropped_reads = 0;

    if (err == ESP_OK) {
        err = mic_start();
    }
    while (err == ESP_OK && written + pending < total_bytes) {
        size_t got = 0;
        const esp_err_t r = mic_read((int16_t *)(wbuf + pending), READ_SAMPLES, &got, 500);
        if (r != ESP_OK || got == 0) {
            if (++dropped_reads > 20) {
                ESP_LOGE(TAG, "mic read failing: %s", esp_err_to_name(r));
                err = r == ESP_OK ? ESP_ERR_TIMEOUT : r;
            }
            continue;
        }
        pending += got * sizeof(int16_t);
        if (written + pending > total_bytes) {
            pending = total_bytes - written;
        }
        if (pending >= WRITE_CHUNK_BYTES || written + pending >= total_bytes) {
            if (fwrite(wbuf, 1, pending, f) != pending) {
                ESP_LOGE(TAG, "card write failed");
                err = ESP_FAIL;
                break;
            }
            written += pending;
            pending = 0;
        }
        if (written >= next_tick) {
            next_tick += MIC_SAMPLE_RATE * 2;
            s_state.elapsed_s = (int)(written / (MIC_SAMPLE_RATE * 2));
            if (s_tick) {
                s_tick(s_state.elapsed_s, s_ctx);
            }
        }
    }
    mic_stop();

    if (err == ESP_OK) {
        wav_header_write(header, MIC_SAMPLE_RATE, MIC_CHANNELS, MIC_BITS, written);
        if (fseek(f, 0, SEEK_SET) != 0 || fwrite(header, 1, sizeof(header), f) != sizeof(header)) {
            err = ESP_FAIL;
        }
    }
    if (fclose(f) != 0 && err == ESP_OK) {
        err = ESP_FAIL;
    }
    ESP_LOGI(TAG, "%s: %lu bytes of audio (%lu s), result %s", path, (unsigned long)written,
             (unsigned long)(written / (MIC_SAMPLE_RATE * 2)), esp_err_to_name(err));
    return err;
}

static void recorder_task(void *arg)
{
    (void)arg;
    const char *mount = NULL;
    uint8_t *wbuf = heap_caps_malloc(WRITE_CHUNK_BYTES + READ_SAMPLES * sizeof(int16_t),
                                     MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    esp_err_t err = wbuf ? ESP_OK : ESP_ERR_NO_MEM;
    if (err == ESP_OK) {
        err = board_storage_mount(&mount);
    }
    if (err == ESP_OK) {
        make_path(s_state.path, sizeof(s_state.path), mount);
        ESP_LOGI(TAG, "recording %d s to %s", s_seconds, s_state.path);
        err = record(s_state.path, s_seconds, wbuf);
        board_storage_unmount();
    } else {
        ESP_LOGE(TAG, "cannot record: %s", esp_err_to_name(err));
    }
    free(wbuf);
    s_state.last_err = err;
    s_state.running = false;
    if (s_done) {
        s_done(err, s_ctx);
    }
    vTaskDelete(NULL);
}

esp_err_t recorder_start(int seconds, recorder_done_cb_t done, recorder_tick_cb_t tick, void *ctx)
{
    ESP_RETURN_ON_FALSE(!s_state.running, ESP_ERR_INVALID_STATE, TAG, "already recording");
    ESP_RETURN_ON_FALSE(seconds > 0 && seconds <= 3600, ESP_ERR_INVALID_ARG, TAG, "1..3600 s");
    s_seconds = seconds;
    s_done = done;
    s_tick = tick;
    s_ctx = ctx;
    s_state.running = true;
    s_state.elapsed_s = 0;
    s_state.total_s = seconds;
    s_state.last_err = ESP_OK;
    if (xTaskCreate(recorder_task, "recorder", 6144, NULL, 8, NULL) != pdPASS) {
        s_state.running = false;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void recorder_get_state(recorder_state_t *out)
{
    *out = s_state;
}
