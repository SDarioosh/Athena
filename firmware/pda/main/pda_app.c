#include "pda_app.h"

#include <inttypes.h>
#include <string.h>
#include <sys/time.h>

#include "ble_pda.h"
#include "crc32.h"
#include "esp_heap_caps.h"
#include "esp_littlefs.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "feed_store.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "isotime.h"
#include "nvs.h"
#include "t5_board.h"

static const char *TAG = "pda";

#define OUTBOX_PATH PDA_MOUNT "/ob"
#define ACTIVE_MS 60000
#define EVENT_QUEUE_LEN 24

static SemaphoreHandle_t s_lock;
static QueueHandle_t s_events;
static feed_t *s_feed;      /* NULL: no feed yet */
static outbox_t *s_outbox;  /* PSRAM: ~1.5 KB of index, but keep internal RAM free */
static pda_pending_t *s_pending;
static int s_n_pending;
static pda_failed_t s_failed[PDA_FAILED_MAX];
static int s_n_failed;
static volatile bool s_time_valid;
static volatile int s_tz_min;
static volatile int64_t s_last_touch_ms = -ACTIVE_MS;
static volatile int64_t s_last_link_s;
static volatile uint16_t s_ob_waiting;
static volatile uint32_t s_feed_version;

static void *psram_calloc(size_t n)
{
    void *p = heap_caps_calloc(1, n, MALLOC_CAP_SPIRAM);
    return p ? p : calloc(1, n);
}

void pda_app_lock(void)
{
    xSemaphoreTakeRecursive(s_lock, portMAX_DELAY);
}

void pda_app_unlock(void)
{
    xSemaphoreGiveRecursive(s_lock);
}

void pda_app_post(const pda_ev_t *ev)
{
    if (s_events && xQueueSend(s_events, ev, 0) != pdTRUE) {
        ESP_LOGW(TAG, "UI event queue full, dropped %d", (int)ev->type);
    }
}

static void post_simple(pda_ev_type_t t, int32_t arg)
{
    const pda_ev_t ev = {.type = t, .arg = arg};
    pda_app_post(&ev);
}

bool pda_app_wait_event(pda_ev_t *ev, uint32_t timeout_ms)
{
    return xQueueReceive(s_events, ev, pdMS_TO_TICKS(timeout_ms)) == pdTRUE;
}

/* ---- pending list, rebuilt from the outbox (under the lock) ---- */

static void rebuild_pending(void)
{
    s_ob_waiting = (uint16_t)s_outbox->n;
    s_n_pending = 0;
    pda_ob_entry_t *e = malloc(sizeof(*e));
    if (!e) {
        return;
    }
    for (int i = 0; i < s_outbox->n; i++) {
        pda_pending_t *p = &s_pending[s_n_pending];
        p->id = s_outbox->e[i].id;
        p->listed = s_outbox->e[i].listed;
        if (outbox_read(s_outbox, p->id, e) == 0 && pda_action_parse(e->json, e->len, &p->info)) {
            s_n_pending++;
        }
    }
    free(e);
}

static void sync_listed(void)
{
    for (int i = 0; i < s_n_pending; i++) {
        const outbox_meta_t *m = outbox_find(s_outbox, s_pending[i].id);
        s_pending[i].listed = m && m->listed;
    }
}

const feed_t *pda_app_feed(void)
{
    return s_feed;
}

int pda_app_pending(const pda_pending_t **out)
{
    *out = s_pending;
    return s_n_pending;
}

int pda_app_failed(const pda_failed_t **out)
{
    *out = s_failed;
    return s_n_failed;
}

const pda_pending_t *pda_app_pending_item(int64_t item_id)
{
    const pda_pending_t *found = NULL;
    for (int i = 0; i < s_n_pending; i++) {
        if (s_pending[i].info.type == PDA_ACT_ITEM_STATUS && s_pending[i].info.item_id == item_id) {
            found = &s_pending[i]; /* the newest one wins */
        }
    }
    return found;
}

const pda_pending_t *pda_app_pending_todo(const char *note, int32_t i)
{
    const pda_pending_t *found = NULL;
    for (int k = 0; k < s_n_pending; k++) {
        const pda_action_info_t *a = &s_pending[k].info;
        if (a->type == PDA_ACT_TODO_TOGGLE && a->item_id == i && strcmp(a->ref, note) == 0) {
            found = &s_pending[k]; /* the newest one wins */
        }
    }
    return found;
}

const pda_pending_t *pda_app_pending_fetch(const char *ref)
{
    for (int i = 0; i < s_n_pending; i++) {
        if (s_pending[i].info.type == PDA_ACT_FETCH && strcmp(s_pending[i].info.ref, ref) == 0) {
            return &s_pending[i];
        }
    }
    return NULL;
}

uint16_t pda_app_outbox_waiting(void)
{
    return s_ob_waiting;
}

uint32_t pda_app_feed_version(void)
{
    return s_feed_version;
}

int pda_app_outbox_count(void)
{
    pda_app_lock();
    const int n = s_outbox->n;
    pda_app_unlock();
    return n;
}

const outbox_t *pda_app_outbox(void)
{
    return s_outbox;
}

int pda_app_queue(const char *json, size_t len)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    const uint64_t now_ms = s_time_valid ? (uint64_t)tv.tv_sec * 1000 + (uint64_t)tv.tv_usec / 1000 : 0;
    pda_app_lock();
    uint32_t id = 0;
    const int rc = outbox_add(s_outbox, json, len, now_ms, &id);
    if (rc == 0) {
        rebuild_pending();
    }
    pda_app_unlock();
    if (rc == 0) {
        ESP_LOGI(TAG, "queued action %08x: %.*s", (unsigned)id, (int)len, json);
        ble_pda_kick();
        post_simple(PDA_EV_OUTBOX, 0);
    } else {
        ESP_LOGW(TAG, "queue action failed: %d", rc);
    }
    return rc;
}

int pda_app_cancel(uint32_t action_id)
{
    pda_app_lock();
    const outbox_meta_t *m = outbox_find(s_outbox, action_id);
    int rc = -1;
    if (m && !m->listed) {
        bool was;
        rc = outbox_remove(s_outbox, action_id, &was) == 0 ? 0 : -3;
        rebuild_pending();
    }
    pda_app_unlock();
    if (rc == 0) {
        ESP_LOGI(TAG, "action %08x cancelled", (unsigned)action_id);
        ble_pda_kick();
        post_simple(PDA_EV_OUTBOX, 0);
    }
    return rc;
}

detail_t *pda_app_load_detail(const char *ref)
{
    char *data;
    size_t len;
    if (feed_store_load_detail(PDA_MOUNT, ref, &data, &len) != 0) {
        return NULL;
    }
    detail_t *d = psram_calloc(sizeof(*d));
    if (d && detail_parse(data, len, d) != 0) {
        free(d);
        d = NULL;
    }
    free(data);
    if (d && strcmp(d->ref, ref) != 0) { /* a cache-key collision */
        detail_free(d);
        free(d);
        d = NULL;
    }
    return d;
}

/* ---- the BLE store ---- */

int pda_app_commit_blob(uint8_t kind, uint32_t blob_id, const uint8_t *data, size_t len)
{
    if (kind == PDA_KIND_FEED) {
        feed_t *nf = psram_calloc(sizeof(*nf));
        if (!nf) {
            return PDA_E_IO;
        }
        const int prc = feed_parse((const char *)data, len, nf);
        if (prc != 0) {
            ESP_LOGW(TAG, "feed %" PRIu32 " rejected: parse error %d", blob_id, prc);
            free(nf);
            return PDA_E_BAD_STATE;
        }
        if (feed_store_put_feed(PDA_MOUNT, data, len) != 0) {
            feed_free(nf);
            free(nf);
            return PDA_E_IO;
        }
        pda_app_lock();
        feed_t *old = s_feed;
        s_feed = nf;
        s_feed_version = nf->version;
        s_n_failed = 0; /* the new feed shows the server's truth */
        pda_app_unlock();
        if (old) {
            feed_free(old);
            free(old);
        }
        ESP_LOGI(TAG, "feed %" PRIu32 " stored: %d conversations, %d actions, %d events, %u bytes",
                 nf->version, nf->n_conv, nf->n_actions, nf->n_events, (unsigned)len);
        ble_pda_kick();
        post_simple(PDA_EV_FEED, 0);
        return 0;
    }
    char ref[PDA_REF_MAX];
    if (!detail_peek_ref((const char *)data, len, ref, sizeof(ref))) {
        ESP_LOGW(TAG, "detail %" PRIu32 " rejected: no ref / not JSON", blob_id);
        return PDA_E_BAD_STATE;
    }
    if (feed_store_put_detail(PDA_MOUNT, ref, data, len) != 0) {
        return PDA_E_IO;
    }
    ESP_LOGI(TAG, "detail %s stored (%u bytes)", ref, (unsigned)len);
    post_simple(PDA_EV_DETAIL, (int32_t)crc32_update(0, ref, strlen(ref)));
    return 0;
}

static int st_time(void *ctx, int64_t epoch_ms, int16_t tz_min)
{
    (void)ctx;
    return pda_app_set_time(epoch_ms, tz_min);
}

static int st_commit(void *ctx, uint8_t kind, uint32_t blob_id, const uint8_t *data, size_t len)
{
    (void)ctx;
    return pda_app_commit_blob(kind, blob_id, data, len);
}

static size_t st_list(void *ctx, uint32_t after, pda_ob_entry_t *out, size_t max, uint32_t *rem)
{
    (void)ctx;
    pda_app_lock();
    const size_t n = outbox_list(s_outbox, after, out, max, rem);
    sync_listed();
    pda_app_unlock();
    return n;
}

static int st_ack(void *ctx, uint32_t action_id, uint8_t result, bool *was_queued)
{
    (void)ctx;
    pda_app_lock();
    if (result != 0) {
        for (int i = 0; i < s_n_pending; i++) {
            const pda_pending_t *p = &s_pending[i];
            if (p->id != action_id) {
                continue;
            }
            if (s_n_failed == PDA_FAILED_MAX) {
                memmove(&s_failed[0], &s_failed[1], sizeof(s_failed[0]) * (PDA_FAILED_MAX - 1));
                s_n_failed--;
            }
            pda_failed_t *f = &s_failed[s_n_failed++];
            f->type = p->info.type;
            f->item_id = p->info.item_id;
            snprintf(f->what, sizeof(f->what), "%s",
                     p->info.type == PDA_ACT_CAL_ADD ? p->info.title : p->info.ref);
        }
    }
    const int rc = outbox_remove(s_outbox, action_id, was_queued);
    rebuild_pending();
    pda_app_unlock();
    if (rc != 0) {
        return PDA_E_IO;
    }
    ESP_LOGI(TAG, "action %08x acked: %s%s", (unsigned)action_id, result ? "rejected" : "done",
             *was_queued ? "" : " (was not queued)");
    post_simple(PDA_EV_OUTBOX, result ? 1 : 0);
    return 0;
}

static const pda_store_t STORE = {st_time, st_commit, st_list, st_ack};

const pda_store_t *pda_app_store(void)
{
    return &STORE;
}

/* ---- clock ---- */

bool pda_app_time_valid(void)
{
    return s_time_valid;
}

int pda_app_tz_min(void)
{
    return s_tz_min;
}

int64_t pda_app_now(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return tv.tv_sec;
}

int pda_app_set_time(int64_t epoch_ms, int16_t tz_min)
{
    const struct timeval tv = {.tv_sec = (time_t)(epoch_ms / 1000),
                               .tv_usec = (suseconds_t)((epoch_ms % 1000) * 1000)};
    settimeofday(&tv, NULL);
    s_time_valid = true;
    const esp_err_t err = t5_rtc_set(epoch_ms / 1000);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "RTC write: %s", esp_err_to_name(err));
    }
    if (tz_min != s_tz_min) {
        s_tz_min = tz_min;
        nvs_handle_t h;
        if (nvs_open("pda", NVS_READWRITE, &h) == ESP_OK) {
            nvs_set_i16(h, "tz", tz_min);
            nvs_commit(h);
            nvs_close(h);
        }
    }
    char buf[24];
    isotime_format(epoch_ms / 1000, buf, sizeof(buf));
    ESP_LOGI(TAG, "TIME %s, UTC%+d min", buf, tz_min);
    post_simple(PDA_EV_TICK, 0);
    return 0;
}

void pda_app_touch_activity(void)
{
    const bool was = pda_app_user_active();
    s_last_touch_ms = esp_timer_get_time() / 1000;
    if (!was) {
        ble_pda_kick(); /* status bit4 */
    }
}

bool pda_app_user_active(void)
{
    return esp_timer_get_time() / 1000 - s_last_touch_ms < ACTIVE_MS;
}

int64_t pda_app_last_link_s(void)
{
    return s_last_link_s;
}

void pda_app_note_link(bool up)
{
    (void)up;
    s_last_link_s = pda_app_now();
}

/* ---- init ---- */

esp_err_t pda_app_wipe(void)
{
    pda_app_lock();
    int rc = feed_store_wipe(PDA_MOUNT);
    rc |= outbox_clear(s_outbox);
    feed_t *old = s_feed;
    s_feed = NULL;
    s_feed_version = 0;
    s_n_failed = 0;
    rebuild_pending();
    pda_app_unlock();
    if (old) {
        feed_free(old);
        free(old);
    }
    ESP_LOGW(TAG, "cache wiped (feed, details, outbox)");
    ble_pda_kick();
    post_simple(PDA_EV_FEED, 0);
    return rc == 0 ? ESP_OK : ESP_FAIL;
}

esp_err_t pda_app_init(void)
{
    s_lock = xSemaphoreCreateRecursiveMutex();
    s_events = xQueueCreate(EVENT_QUEUE_LEN, sizeof(pda_ev_t));
    s_outbox = psram_calloc(sizeof(*s_outbox));
    s_pending = psram_calloc(sizeof(*s_pending) * OUTBOX_MAX);
    if (!s_lock || !s_events || !s_outbox || !s_pending) {
        return ESP_ERR_NO_MEM;
    }
    const esp_vfs_littlefs_conf_t conf = {
        .base_path = PDA_MOUNT,
        .partition_label = "storage",
        .format_if_mount_failed = true,
    };
    esp_err_t err = esp_vfs_littlefs_register(&conf);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "LittleFS: %s", esp_err_to_name(err));
        return err;
    }
    size_t total = 0, used = 0;
    esp_littlefs_info("storage", &total, &used);
    ESP_LOGI(TAG, "LittleFS: %u of %u KB used", (unsigned)(used / 1024), (unsigned)(total / 1024));
    if (feed_store_init(PDA_MOUNT) != 0 || outbox_init(s_outbox, OUTBOX_PATH, esp_random()) != 0) {
        ESP_LOGE(TAG, "storage init failed");
        return ESP_FAIL;
    }
    if (s_outbox->dropped_corrupt) {
        ESP_LOGW(TAG, "outbox: %d corrupt record(s) dropped", s_outbox->dropped_corrupt);
    }
    rebuild_pending();

    nvs_handle_t h;
    if (nvs_open("pda", NVS_READONLY, &h) == ESP_OK) {
        int16_t tz = 0;
        if (nvs_get_i16(h, "tz", &tz) == ESP_OK) {
            s_tz_min = tz;
        }
        nvs_close(h);
    }

    int64_t t = 0;
    bool valid = false;
    if (t5_rtc_get(&t, &valid) == ESP_OK && valid) {
        const struct timeval tv = {.tv_sec = (time_t)t};
        settimeofday(&tv, NULL);
        s_time_valid = true;
    }

    char *data;
    size_t len;
    if (feed_store_load_feed(PDA_MOUNT, &data, &len) == 0) {
        feed_t *f = psram_calloc(sizeof(*f));
        if (f && feed_parse(data, len, f) == 0) {
            s_feed = f;
            s_feed_version = f->version;
            ESP_LOGI(TAG, "feed %" PRIu32 " loaded from flash", f->version);
        } else {
            ESP_LOGW(TAG, "stored feed doesn't parse; ignored");
            free(f);
        }
        free(data);
    }
    ESP_LOGI(TAG, "outbox: %d waiting, next id %08x; clock %s, UTC%+d min", s_outbox->n,
             (unsigned)s_outbox->next_id, s_time_valid ? "valid" : "not set", s_tz_min);
    return ESP_OK;
}
