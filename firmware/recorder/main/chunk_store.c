/* chunk_store.h on the ESP32: chunk_queue.c behind a mutex, on the SD card at /sdcard/q. */
#include "chunk_store.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "board.h"
#include "chunk_queue.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static const char *TAG = "chunk_store";

#define QUEUE_SUBDIR "/q"

_Static_assert(CQ_FLAG_LAST == CHUNK_FLAG_LAST && CQ_FLAG_TIME_UNSET == CHUNK_FLAG_TIME_UNSET,
               "chunk_queue.h and chunk_store.h flags differ");

static chunk_queue_t s_q;
static bool s_ready;
static uint32_t s_boot_id;
static int s_media_users; /* our own count of board_storage_mount() calls */
static chunk_store_listener_t s_listener;
static void *s_listener_ctx;

static SemaphoreHandle_t s_lock;
static StaticSemaphore_t s_lock_buf;
static portMUX_TYPE s_lock_init = portMUX_INITIALIZER_UNLOCKED;

/* The BLE task can ask for stats before chunk_store_init() ran: create the mutex on first
 * use. */
static void lock(void)
{
    if (!s_lock) {
        taskENTER_CRITICAL(&s_lock_init);
        if (!s_lock) {
            s_lock = xSemaphoreCreateMutexStatic(&s_lock_buf);
        }
        taskEXIT_CRITICAL(&s_lock_init);
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
}

static void unlock(void)
{
    xSemaphoreGive(s_lock);
}

static void notify(void)
{
    if (s_listener) {
        s_listener(s_listener_ctx);
    }
}

static esp_err_t to_esp(int cq)
{
    switch (cq) {
    case CQ_OK:
        return ESP_OK;
    case CQ_ERR_NOT_FOUND:
        return ESP_ERR_NOT_FOUND;
    case CQ_ERR_STATE:
        return ESP_ERR_INVALID_STATE;
    case CQ_ERR_NO_MEM:
        return ESP_ERR_NO_MEM;
    case CQ_ERR_ARG:
        return ESP_ERR_INVALID_ARG;
    default:
        return ESP_FAIL;
    }
}

static void to_info(const cq_info_t *c, chunk_info_t *out)
{
    out->chunk_id = c->chunk_id;
    memcpy(out->session_id, c->session_id, sizeof(out->session_id));
    out->sequence = c->sequence;
    out->start_ms = c->start_ms;
    out->duration_ms = c->duration_ms;
    out->bytes = c->bytes;
    out->crc32 = c->crc32;
    out->flags = c->flags;
    out->boot_id = c->boot_id;
    out->mono_ms = c->mono_ms;
}

/* Mount bookkeeping; the caller holds the lock. */
static esp_err_t mount_locked(void)
{
    const char *mp;
    const esp_err_t err = board_storage_mount(&mp);
    if (err == ESP_OK) {
        s_media_users++;
    }
    return err;
}

static void unmount_locked(void)
{
    if (s_media_users > 0) {
        s_media_users--;
        if (s_media_users == 0) {
            cq_read_close(&s_q); /* no file may stay open over an unmount */
        }
        board_storage_unmount();
    }
}

esp_err_t chunk_store_init(uint32_t boot_id)
{
    lock();
    s_boot_id = boot_id;
    if (s_ready) {
        unlock();
        return ESP_OK;
    }
    const char *mp;
    esp_err_t err = board_storage_mount(&mp);
    if (err != ESP_OK) {
        unlock();
        ESP_LOGW(TAG, "no card: queue empty until it is mounted (%s)", esp_err_to_name(err));
        return err;
    }
    char dir[32];
    snprintf(dir, sizeof(dir), "%s" QUEUE_SUBDIR, mp);
    cq_boot_report_t rep;
    const int64_t t0 = esp_timer_get_time();
    err = to_esp(cq_init(&s_q, dir, &rep));
    board_storage_unmount();
    if (err == ESP_OK) {
        s_ready = true;
        cq_stats_t st;
        cq_stats(&s_q, &st);
        ESP_LOGI(TAG,
                 "%s: %" PRIu32 " chunks queued (%" PRIu64 " bytes, %" PRIu64 " s audio), next id %" PRIu32
                 "; recovered %" PRIu32 ", discarded %" PRIu32 ", cleaned %" PRIu32 " (%lld ms)",
                 dir, st.chunks, st.bytes, st.audio_ms / 1000, s_q.next_id, rep.recovered,
                 rep.discarded, rep.cleaned, (long long)((esp_timer_get_time() - t0) / 1000));
        if (rep.corrupt) {
            ESP_LOGW(TAG, "%" PRIu32 " chunk(s) with unreadable records left on the card, not queued",
                     rep.corrupt);
        }
    } else {
        ESP_LOGE(TAG, "queue at %s: %s", dir, esp_err_to_name(err));
    }
    unlock();
    if (err == ESP_OK) {
        notify();
    }
    return err;
}

bool chunk_store_ready(void)
{
    return s_ready;
}

uint32_t chunk_store_boot_id(void)
{
    return s_boot_id;
}

void chunk_store_set_listener(chunk_store_listener_t cb, void *ctx)
{
    lock();
    s_listener = cb;
    s_listener_ctx = ctx;
    unlock();
}

void chunk_store_stats(chunk_store_stats_t *out)
{
    memset(out, 0, sizeof(*out));
    lock();
    if (s_ready) {
        cq_stats_t st;
        cq_stats(&s_q, &st);
        out->chunks = st.chunks;
        out->bytes = st.bytes > UINT32_MAX ? UINT32_MAX : (uint32_t)st.bytes;
        out->audio_ms = st.audio_ms > UINT32_MAX ? UINT32_MAX : (uint32_t)st.audio_ms;
        out->has_last = st.has_last;
        out->oldest_id = st.oldest_id;
    }
    unlock();
}

size_t chunk_store_list(uint32_t after_id, chunk_info_t *out, size_t max, uint32_t *remaining)
{
    cq_info_t tmp[8];
    size_t n = 0;
    if (remaining) {
        *remaining = 0;
    }
    lock();
    while (s_ready && n < max) {
        uint32_t rest = 0;
        const size_t want = max - n < 8 ? max - n : 8;
        const size_t got = cq_list(&s_q, after_id, tmp, want, &rest);
        for (size_t i = 0; i < got; i++) {
            to_info(&tmp[i], &out[n++]);
        }
        if (remaining) {
            *remaining = rest;
        }
        if (got < want) {
            break;
        }
        after_id = tmp[got - 1].chunk_id;
    }
    unlock();
    return n;
}

bool chunk_store_get(uint32_t chunk_id, chunk_info_t *out)
{
    cq_info_t c;
    lock();
    const bool found = s_ready && cq_get(&s_q, chunk_id, &c);
    unlock();
    if (found && out) {
        to_info(&c, out);
    }
    return found;
}

esp_err_t chunk_store_media_open(void)
{
    lock();
    const esp_err_t err = mount_locked();
    unlock();
    return err;
}

void chunk_store_media_close(void)
{
    lock();
    /* A BLE connection ends here: its cached read handle goes even if the recorder keeps
     * the card mounted. */
    cq_read_close(&s_q);
    unmount_locked();
    unlock();
}

esp_err_t chunk_store_read(uint32_t chunk_id, uint32_t offset, void *buf, size_t len,
                           size_t *got)
{
    *got = 0;
    lock();
    esp_err_t err = ESP_ERR_INVALID_STATE;
    if (s_ready && s_media_users > 0) {
        err = to_esp(cq_read(&s_q, chunk_id, offset, buf, len, got));
    }
    unlock();
    if (err != ESP_OK && err != ESP_ERR_NOT_FOUND) {
        ESP_LOGW(TAG, "read %08" PRIx32 " @%" PRIu32 ": %s", chunk_id, offset, esp_err_to_name(err));
    }
    return err;
}

static esp_err_t delete_chunk(uint32_t chunk_id, bool *was_queued, const char *why)
{
    bool was = false;
    lock();
    esp_err_t err = ESP_ERR_INVALID_STATE;
    if (s_ready && s_media_users > 0) {
        err = to_esp(cq_delete(&s_q, chunk_id, &was));
    }
    unlock();
    if (was_queued) {
        *was_queued = was;
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "%s %08" PRIx32 ": %s", why, chunk_id, esp_err_to_name(err));
    } else if (was) {
        ESP_LOGI(TAG, "%s %08" PRIx32, why, chunk_id);
        notify();
    }
    return err;
}

esp_err_t chunk_store_ack(uint32_t chunk_id, bool *was_queued)
{
    return delete_chunk(chunk_id, was_queued, "acked");
}

esp_err_t chunk_store_drop(uint32_t chunk_id)
{
    lock();
    esp_err_t err = mount_locked();
    unlock();
    if (err != ESP_OK) {
        return err;
    }
    bool was = false;
    err = delete_chunk(chunk_id, &was, "dropped");
    lock();
    unmount_locked();
    unlock();
    return err == ESP_OK && !was ? ESP_ERR_NOT_FOUND : err;
}

int chunk_store_fix_time(int64_t now_epoch_ms)
{
    lock();
    if (!s_ready) {
        unlock();
        return 0;
    }
    /* Mount only if something needs a rewrite: the TIME command comes on every connection. */
    bool any = cq_current(&s_q, NULL);
    for (size_t i = 0; !any && i < s_q.count; i++) {
        any = (s_q.items[i].flags & CQ_FLAG_TIME_UNSET) && s_q.items[i].boot_id == s_boot_id;
    }
    int n = 0;
    if (any && mount_locked() == ESP_OK) {
        n = cq_fix_time(&s_q, s_boot_id, now_epoch_ms, esp_timer_get_time() / 1000);
        unmount_locked();
    }
    unlock();
    if (n < 0) {
        ESP_LOGE(TAG, "TIME fix-up: a record could not be rewritten");
    }
    if (n > 0) {
        notify();
    }
    return n;
}

void chunk_store_print(void)
{
    lock();
    if (!s_ready) {
        unlock();
        printf("queue not ready (no card at boot?)\n");
        return;
    }
    cq_stats_t st;
    cq_stats(&s_q, &st);
    printf("%" PRIu32 " chunks, %" PRIu64 " bytes, %" PRIu64 " s audio, next id %" PRIu32 "\n",
           st.chunks, st.bytes, st.audio_ms / 1000, s_q.next_id);
    printf("id        session          seq    start_ms       dur_ms  bytes   crc32    flags\n");
    for (size_t i = 0; i < s_q.count; i++) {
        const cq_info_t *c = &s_q.items[i];
        char hex[17];
        cq_session_hex(c->session_id, hex);
        printf("%08" PRIx32 "  %s %-6" PRIu32 " %-14" PRIu64 " %-7" PRIu32 " %-7" PRIu32 " %08" PRIx32
               " %s%s\n",
               c->chunk_id, hex, c->sequence, c->start_ms, c->duration_ms, c->bytes, c->crc32,
               (c->flags & CQ_FLAG_LAST) ? "LAST " : "",
               (c->flags & CQ_FLAG_TIME_UNSET) ? "TIME_UNSET" : "");
    }
    cq_info_t cur;
    if (cq_current(&s_q, &cur)) {
        printf("recording: %08" PRIx32 " (seq %" PRIu32 ", %" PRIu32 " bytes so far)\n",
               cur.chunk_id, cur.sequence, cur.bytes);
    }
    unlock();
}

/* ---- recording side ---- */

esp_err_t chunk_store_begin(const chunk_store_new_t *c, uint32_t *chunk_id)
{
    cq_new_t n = {
        .sequence = c->sequence,
        .start_ms = c->start_ms,
        .flags = c->flags,
        .mono_ms = c->mono_ms,
    };
    memcpy(n.session_id, c->session_id, sizeof(n.session_id));
    lock();
    n.boot_id = s_boot_id;
    esp_err_t err = ESP_ERR_INVALID_STATE;
    if (s_ready && s_media_users > 0) {
        err = to_esp(cq_begin(&s_q, &n, chunk_id));
    }
    unlock();
    return err;
}

esp_err_t chunk_store_append(const void *data, size_t len)
{
    lock();
    const esp_err_t err = to_esp(cq_append(&s_q, data, len));
    unlock();
    return err;
}

esp_err_t chunk_store_sync(void)
{
    lock();
    const esp_err_t err = to_esp(cq_sync(&s_q));
    unlock();
    return err;
}

esp_err_t chunk_store_end(uint32_t duration_ms, bool last)
{
    lock();
    const esp_err_t err = to_esp(cq_end(&s_q, duration_ms, last));
    unlock();
    notify(); /* even on an error: the chunk may have been recovered into the queue */
    return err;
}

esp_err_t chunk_store_abandon(uint32_t *kept_id)
{
    lock();
    const esp_err_t err = to_esp(cq_abandon(&s_q, kept_id));
    unlock();
    notify();
    return err;
}

esp_err_t chunk_store_set_last(uint32_t chunk_id)
{
    lock();
    esp_err_t err = ESP_ERR_INVALID_STATE;
    if (s_ready && s_media_users > 0) {
        err = to_esp(cq_set_last(&s_q, chunk_id));
    }
    unlock();
    if (err == ESP_OK) {
        notify();
    }
    return err;
}

esp_err_t chunk_store_free_bytes(uint64_t *free_bytes)
{
    uint64_t total = 0;
    lock();
    const esp_err_t err = s_media_users > 0 ? board_storage_info(&total, free_bytes)
                                            : ESP_ERR_INVALID_STATE;
    unlock();
    return err;
}
