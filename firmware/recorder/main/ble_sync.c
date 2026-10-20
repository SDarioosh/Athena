/* NimBLE glue for the chunk-transfer protocol (docs/twatch-ble-protocol.md). The protocol
 * itself (frames, parsing, the busy rule, LIST/READ/ACK/TIME/BYE, the advertising rule)
 * is in ble_proto.c; this file owns the radio, pairing and the BLE task.
 *
 * Threads: NimBLE callbacks (GAP events, GATT access) run in the NimBLE host task and only
 * update the small shared connection state under s_lock and post jobs. Everything slow (SD
 * reads, I2C, notifications with retries, advertising decisions) runs in the BLE task,
 * one job at a time, so a command never blocks the host task. */
#include "ble_sync.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <sys/time.h>

#include "ble_proto.h"
#include "board.h"
#include "chunk_store.h"
#include "esp_log.h"
#include "esp_task_wdt.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

/* NimBLE's NVS-backed key store (CONFIG_BT_NIMBLE_NVS_PERSIST); it has no public header. */
void ble_store_config_init(void);

static const char *TAG = "ble";

#define DEVICE_NAME "barvis-tw"
#define WINDOW_MS (2 * 60 * 1000)
#define IDLE_DISCONNECT_MS 30000
#define BYE_DISCONNECT_MS 1000
#define STATUS_MIN_PERIOD_MS 1000
#define BATTERY_MAX_AGE_MS 30000
/* A notification that can't get a buffer for this long means the link is dead. */
#define SEND_GIVE_UP_MS 5000
#define SEND_BACKOFF_MAX_MS 32
/* mbuf blocks left to the host (ATT responses, L2CAP signalling) while we stream DATA. */
#define MSYS_RESERVE 8
#define ADV_ITVL_MIN 1600   /* 1000 ms, 0.625 ms units */
#define ADV_ITVL_MAX 2000   /* 1250 ms */
/* In the sync window someone is connecting right now. Windows' connection scan is slow
 * enough that at ~1 s it sometimes missed the watch for its whole 20 s connect timeout. */
#define ADV_FAST_ITVL_MIN 244 /* 152.5 ms */
#define ADV_FAST_ITVL_MAX 338 /* 211.25 ms */
#define CONN_ITVL_MIN 12    /* 15 ms, 1.25 ms units */
#define CONN_ITVL_MAX 24    /* 30 ms */
#define CONN_SUPERVISION 400 /* 4 s, 10 ms units */
#define DLE_TX_OCTETS 251
#define DLE_TX_TIME 2120    /* us for 251 octets on 1M, the longest uncoded packet */
#define JOB_QUEUE_LEN 12
#define TASK_STACK 6144
#define TASK_PRIO 5

/* f806xxxx-61c0-4fda-8b7b-26b08e916438, little-endian as NimBLE stores it. */
#define BARVIS_UUID(n)                                                                       \
    BLE_UUID128_INIT(0x38, 0x64, 0x91, 0x8e, 0xb0, 0x26, 0x7b, 0x8b, 0xda, 0x4f, 0xc0, 0x61, \
                     (n) & 0xff, ((n) >> 8) & 0xff, 0x06, 0xf8)

static const ble_uuid128_t s_svc_uuid = BARVIS_UUID(0x0000);
static const ble_uuid128_t s_info_uuid = BARVIS_UUID(0x0001);
static const ble_uuid128_t s_status_uuid = BARVIS_UUID(0x0002);
static const ble_uuid128_t s_ctrl_uuid = BARVIS_UUID(0x0003);
static const ble_uuid128_t s_data_uuid = BARVIS_UUID(0x0004);

typedef enum { CHR_INFO = 1, CHR_STATUS, CHR_CTRL, CHR_DATA } chr_id_t;

static int on_access(uint16_t conn_handle, uint16_t attr_handle, struct ble_gatt_access_ctxt *ctxt,
                     void *arg);
static int on_gap_event(struct ble_gap_event *ev, void *arg);

static uint16_t s_status_handle;
static uint16_t s_data_handle;

static const struct ble_gatt_svc_def s_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &s_svc_uuid.u,
        .characteristics =
            (struct ble_gatt_chr_def[]){
                {
                    .uuid = &s_info_uuid.u,
                    .access_cb = on_access,
                    .arg = (void *)CHR_INFO,
                    .flags = BLE_GATT_CHR_F_READ,
                },
                {
                    .uuid = &s_status_uuid.u,
                    .access_cb = on_access,
                    .arg = (void *)CHR_STATUS,
                    .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_READ_ENC |
                             BLE_GATT_CHR_F_READ_AUTHEN | BLE_GATT_CHR_F_NOTIFY |
                             BLE_GATT_CHR_F_NOTIFY_INDICATE_ENC |
                             BLE_GATT_CHR_F_NOTIFY_INDICATE_AUTHEN,
                    .val_handle = &s_status_handle,
                },
                {
                    .uuid = &s_ctrl_uuid.u,
                    .access_cb = on_access,
                    .arg = (void *)CHR_CTRL,
                    .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_ENC |
                             BLE_GATT_CHR_F_WRITE_AUTHEN,
                },
                {
                    .uuid = &s_data_uuid.u,
                    .access_cb = on_access,
                    .arg = (void *)CHR_DATA,
                    .flags = BLE_GATT_CHR_F_NOTIFY | BLE_GATT_CHR_F_NOTIFY_INDICATE_ENC |
                             BLE_GATT_CHR_F_NOTIFY_INDICATE_AUTHEN,
                    .val_handle = &s_data_handle,
                },
                {0},
            },
    },
    {0},
};

/* ---- shared state (host task, BLE task, timers, other tasks): under s_lock ---- */

static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
typedef struct {
    uint16_t handle; /* BLE_HS_CONN_HANDLE_NONE when not connected */
    uint32_t gen;    /* bumped per connection, so stale jobs can be told apart */
    uint16_t mtu;
    bool authorized; /* encrypted, and bonded before or paired in the window */
    bool status_sub;
    uint8_t tx_phy, rx_phy;
    ble_addr_t peer;
} conn_t;
static conn_t s_conn = {.handle = BLE_HS_CONN_HANDLE_NONE};
static ble_proto_gate_t s_gate;
static int64_t s_window_until_ms;
static bool s_kick_queued;
static uint8_t s_status_cache[BLE_PROTO_STATUS_LEN];
static board_battery_t s_batt;
static bool s_batt_ok;

/* ---- host task only ---- */
static ble_addr_t s_bonded[CONFIG_BT_NIMBLE_MAX_BONDS]; /* bonds as they were at connect */
static int s_n_bonded;

/* ---- BLE task only (print reads them unlocked; they are diagnostics) ---- */
static ble_sync_hooks_t s_hooks;
static QueueHandle_t s_jobs;
static esp_timer_handle_t s_idle_timer, s_bye_timer, s_kick_timer, s_status_timer;
static volatile bool s_synced;
static uint8_t s_own_addr_type;
static bool s_media_open;
static int64_t s_last_sync_ms = -1; /* a BYE ended a sync; -1 = not since boot */
/* Newest chunk queued when the last BYE came. Chunks up to it were offered to a relay that
 * left them (e.g. the server rejected them): they only bring advertising back through the
 * hourly stale rule, so a rejected LAST chunk can't keep the radio on. BLE task only. */
static uint32_t s_bye_seen_id;
static int64_t s_batt_ms = -BATTERY_MAX_AGE_MS;
static ble_adv_reason_t s_adv_reason;
static bool s_adv_fast; /* the running advertising uses the fast interval */
static uint8_t s_status_sent[BLE_PROTO_STATUS_LEN];
static bool s_status_sent_valid;
static int64_t s_status_sent_ms;
static uint8_t s_frame[BLE_PROTO_FRAME_MAX];
static char s_info[128];
static struct {
    uint32_t connections, rejected, commands, frames, acked, retries, link_lost, host_resets,
        read_retries;
    uint64_t bytes;
} s_stats;

typedef enum {
    JOB_CMD,
    JOB_FRAME,     /* an ERROR answer decided in the host task */
    JOB_KICK,
    JOB_SYNCED,
    JOB_CONNECTED,
    JOB_DISCONNECTED,
    JOB_IDLE,
    JOB_BYE_DONE,
} job_kind_t;

typedef struct {
    uint8_t kind;
    uint32_t gen;
    union {
        ble_proto_cmd_t cmd;
        uint8_t frame[BLE_PROTO_ERROR_LEN];
    } u;
} job_t;

/* Jobs taken off the queue while a command ran, handled after it. */
static job_t s_deferred[JOB_QUEUE_LEN * 2];
static size_t s_n_deferred;

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

static void restart_timer(esp_timer_handle_t t, int64_t ms)
{
    esp_timer_stop(t); /* ESP_ERR_INVALID_STATE if it wasn't running: fine */
    esp_timer_start_once(t, (uint64_t)(ms > 0 ? ms : 1) * 1000);
}

static bool post_job(job_kind_t kind, uint32_t gen)
{
    const job_t job = {.kind = kind, .gen = gen};
    if (!s_jobs || xQueueSend(s_jobs, &job, 0) != pdTRUE) {
        ESP_LOGW(TAG, "job queue full, dropped %d", (int)kind);
        return false;
    }
    return true;
}

static uint32_t current_gen(void)
{
    taskENTER_CRITICAL(&s_lock);
    const uint32_t gen = s_conn.gen;
    taskEXIT_CRITICAL(&s_lock);
    return gen;
}

/* Coalesced: many kicks while one is queued cost nothing. */
static void post_kick(void)
{
    taskENTER_CRITICAL(&s_lock);
    const bool post = !s_kick_queued;
    s_kick_queued = true;
    taskEXIT_CRITICAL(&s_lock);
    if (post && !post_job(JOB_KICK, 0)) {
        taskENTER_CRITICAL(&s_lock);
        s_kick_queued = false;
        taskEXIT_CRITICAL(&s_lock);
    }
}

static bool window_open_locked(void)
{
    return now_ms() < s_window_until_ms;
}

bool ble_sync_window_is_open(void)
{
    taskENTER_CRITICAL(&s_lock);
    const bool open = window_open_locked();
    taskEXIT_CRITICAL(&s_lock);
    return open;
}

bool ble_sync_is_advertising(void)
{
    return ble_gap_adv_active() != 0;
}

bool ble_sync_is_connected(void)
{
    taskENTER_CRITICAL(&s_lock);
    const bool connected = s_conn.handle != BLE_HS_CONN_HANDLE_NONE;
    taskEXIT_CRITICAL(&s_lock);
    return connected;
}

/* ---- status characteristic ---- */

static void refresh_battery(bool force)
{
    if (!force && now_ms() - s_batt_ms < BATTERY_MAX_AGE_MS) {
        return;
    }
    board_battery_t b;
    const bool ok = board_battery_read(&b) == ESP_OK;
    s_batt_ms = now_ms();
    taskENTER_CRITICAL(&s_lock);
    s_batt = b;
    s_batt_ok = ok;
    taskEXIT_CRITICAL(&s_lock);
}

/* BLE task: rebuild the cached status value (the host task serves reads from the cache). */
static void build_status(uint8_t out[BLE_PROTO_STATUS_LEN])
{
    ble_sync_app_state_t app = {0};
    if (s_hooks.get_state) {
        s_hooks.get_state(&app);
    }
    chunk_store_stats_t st;
    chunk_store_stats(&st);

    taskENTER_CRITICAL(&s_lock);
    const board_battery_t b = s_batt;
    const bool read_ok = s_batt_ok;
    const bool batt_ok = s_batt_ok && s_batt.present;
    const bool window = window_open_locked();
    taskEXIT_CRITICAL(&s_lock);

    ble_proto_status_t s = {
        .battery_pct = -1,
        .queued_chunks = st.chunks > 0xffff ? 0xffff : (uint16_t)st.chunks,
        .queued_bytes = st.bytes,
        .queued_ms = st.audio_ms,
        .boot_id = chunk_store_boot_id(),
    };
    s.flags = (app.recording ? BLE_STATUS_RECORDING : 0) | (app.muted ? BLE_STATUS_MUTED : 0) |
              (app.time_valid ? BLE_STATUS_TIME_VALID : 0) |
              (chunk_store_ready() ? BLE_STATUS_SD_OK : 0) | (window ? BLE_STATUS_WINDOW : 0);
    if (read_ok) {
        s.flags |= (b.charging ? BLE_STATUS_CHARGING : 0) | (b.vbus ? BLE_STATUS_USB : 0);
    }
    if (batt_ok) {
        s.battery_pct = (int8_t)(b.percent < -1 ? -1 : b.percent > 100 ? 100 : b.percent);
        s.battery_mv = (uint16_t)(b.millivolts < 0 ? 0 : b.millivolts > 0xffff ? 0xffff : b.millivolts);
    }
    if (app.time_valid) {
        struct timeval tv;
        gettimeofday(&tv, NULL);
        s.clock_s = (uint32_t)tv.tv_sec;
    }
    ble_proto_encode_status(&s, out);
    taskENTER_CRITICAL(&s_lock);
    memcpy(s_status_cache, out, BLE_PROTO_STATUS_LEN);
    taskEXIT_CRITICAL(&s_lock);
}

/* ---- notifications ---- */

static uint16_t handle_for(uint32_t gen)
{
    taskENTER_CRITICAL(&s_lock);
    const uint16_t h = s_conn.gen == gen ? s_conn.handle : BLE_HS_CONN_HANDLE_NONE;
    taskEXIT_CRITICAL(&s_lock);
    return h;
}

/* Send one notification, waiting for buffers rather than dropping the frame: a lost DATA
 * frame would make the relay discard the whole chunk. Returns 0, or -1 if the link is gone. */
static int notify(uint32_t gen, uint16_t attr, const uint8_t *buf, size_t len)
{
    const int64_t give_up = now_ms() + SEND_GIVE_UP_MS;
    int backoff = 1;
    esp_task_wdt_reset(); /* one per frame while a READ streams */
    for (;;) {
        const uint16_t h = handle_for(gen);
        if (h == BLE_HS_CONN_HANDLE_NONE) {
            return -1;
        }
        if (os_msys_num_free() >= MSYS_RESERVE) {
            struct os_mbuf *om = ble_hs_mbuf_from_flat(buf, (uint16_t)len);
            if (om) {
                const int rc = ble_gatts_notify_custom(h, attr, om); /* consumes om */
                if (rc == 0) {
                    return 0;
                }
                if (rc != BLE_HS_ENOMEM) {
                    ESP_LOGW(TAG, "notify failed: %d", rc);
                    return -1;
                }
            }
        }
        if (now_ms() >= give_up) {
            ESP_LOGW(TAG, "no buffers for %d ms, giving up on the link", SEND_GIVE_UP_MS);
            return -1;
        }
        s_stats.retries++;
        const TickType_t ticks = pdMS_TO_TICKS(backoff);
        vTaskDelay(ticks > 0 ? ticks : 1);
        backoff = backoff < SEND_BACKOFF_MAX_MS ? backoff * 2 : SEND_BACKOFF_MAX_MS;
    }
}

/* While a command streams, send ERROR frames the host task queued (busy) in between, and
 * keep everything else for after the command. */
static void drain_jobs(uint32_t gen)
{
    job_t job;
    while (s_n_deferred < sizeof(s_deferred) / sizeof(s_deferred[0]) &&
           xQueueReceive(s_jobs, &job, 0) == pdTRUE) {
        if (job.kind == JOB_FRAME) {
            if (job.gen == gen) {
                notify(gen, s_data_handle, job.u.frame, BLE_PROTO_ERROR_LEN);
            }
        } else {
            s_deferred[s_n_deferred++] = job;
        }
    }
}

static int send_data(void *ctx, const uint8_t *frame, size_t len)
{
    const uint32_t gen = (uint32_t)(uintptr_t)ctx;
    drain_jobs(gen);
    if (notify(gen, s_data_handle, frame, len) != 0) {
        return -1;
    }
    s_stats.frames++;
    return 0;
}

static void maybe_notify_status(void)
{
    taskENTER_CRITICAL(&s_lock);
    const bool want = s_conn.handle != BLE_HS_CONN_HANDLE_NONE && s_conn.authorized &&
                      s_conn.status_sub;
    const uint32_t gen = s_conn.gen;
    taskEXIT_CRITICAL(&s_lock);
    if (!want) {
        return;
    }
    refresh_battery(false);
    uint8_t v[BLE_PROTO_STATUS_LEN];
    build_status(v);
    /* The clock (bytes 16..19) ticks every second; it isn't a change worth a notification. */
    const bool changed = !s_status_sent_valid || memcmp(v, s_status_sent, 16) != 0 ||
                         memcmp(v + 20, s_status_sent + 20, 4) != 0;
    if (!changed) {
        return;
    }
    const int64_t wait = s_status_sent_ms + STATUS_MIN_PERIOD_MS - now_ms();
    if (s_status_sent_valid && wait > 0) {
        if (!esp_timer_is_active(s_status_timer)) {
            esp_timer_start_once(s_status_timer, (uint64_t)wait * 1000);
        }
        return;
    }
    if (notify(gen, s_status_handle, v, sizeof(v)) == 0) {
        memcpy(s_status_sent, v, sizeof(v));
        s_status_sent_valid = true;
        s_status_sent_ms = now_ms();
    }
}

/* ---- advertising (§7.1) ---- */

static void adv_start(bool fast)
{
    const struct ble_gap_adv_params params = {
        .conn_mode = BLE_GAP_CONN_MODE_UND,
        .disc_mode = BLE_GAP_DISC_MODE_GEN,
        .itvl_min = fast ? ADV_FAST_ITVL_MIN : ADV_ITVL_MIN,
        .itvl_max = fast ? ADV_FAST_ITVL_MAX : ADV_ITVL_MAX,
    };
    const int rc = ble_gap_adv_start(s_own_addr_type, NULL, BLE_HS_FOREVER, &params, on_gap_event,
                                     NULL);
    if (rc != 0) {
        ESP_LOGW(TAG, "advertising start failed: %d", rc);
    } else {
        s_adv_fast = fast;
        ESP_LOGI(TAG, "advertising (%s%s)", ble_proto_adv_reason_name(s_adv_reason),
                 fast ? ", fast" : "");
    }
}

static int adv_setup(void)
{
    int rc = ble_hs_util_ensure_addr(0);
    if (rc == 0) {
        rc = ble_hs_id_infer_auto(0, &s_own_addr_type);
    }
    if (rc != 0) {
        ESP_LOGE(TAG, "no usable address: %d", rc);
        return rc;
    }
    /* flags + the 128-bit service UUID: 3 + 18 bytes; the name goes in the scan response */
    const struct ble_hs_adv_fields adv = {
        .flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP,
        .uuids128 = &s_svc_uuid,
        .num_uuids128 = 1,
        .uuids128_is_complete = 1,
    };
    rc = ble_gap_adv_set_fields(&adv);
    if (rc == 0) {
        const struct ble_hs_adv_fields rsp = {
            .name = (const uint8_t *)DEVICE_NAME,
            .name_len = sizeof(DEVICE_NAME) - 1,
            .name_is_complete = 1,
        };
        rc = ble_gap_adv_rsp_set_fields(&rsp);
    }
    if (rc != 0) {
        ESP_LOGE(TAG, "advertising data: %d", rc);
    }
    return rc;
}

/* Chunks newer than `after`: whether one ends a session, their audio, the newest id. */
static void queued_after(uint32_t after, bool *has_last, uint32_t *audio_ms, uint32_t *newest)
{
    chunk_info_t page[8];
    uint32_t remaining = 0;
    *has_last = false;
    *audio_ms = 0;
    *newest = after;
    do {
        const size_t n = chunk_store_list(*newest, page, 8, &remaining);
        if (n == 0) {
            break;
        }
        for (size_t i = 0; i < n; i++) {
            *has_last |= (page[i].flags & CHUNK_FLAG_LAST) != 0;
            *audio_ms += page[i].duration_ms;
        }
        *newest = page[n - 1].chunk_id;
    } while (remaining > 0);
}

/* Re-evaluate everything a state change can affect. BLE task only. */
static void refresh(void)
{
    chunk_store_stats_t st;
    chunk_store_stats(&st);
    const int64_t now = now_ms();

    taskENTER_CRITICAL(&s_lock);
    const bool connected = s_conn.handle != BLE_HS_CONN_HANDLE_NONE;
    const bool window = window_open_locked();
    const int64_t window_left = s_window_until_ms - now;
    taskEXIT_CRITICAL(&s_lock);

    /* New bonds only in the window: outside it a stranger can't even make the watch store
     * a key (which, with 4 bonds already, would evict the oldest real one). */
    ble_hs_cfg.sm_bonding = window ? 1 : 0;

    bool new_last = false;
    uint32_t new_ms = 0, newest = 0;
    queued_after(s_bye_seen_id, &new_last, &new_ms, &newest);
    const ble_adv_inputs_t in = {
        .connected = connected,
        .window_open = window,
        .has_last = new_last,
        .queued_chunks = st.chunks,
        .queued_ms = new_ms,
        .ms_since_sync = s_last_sync_ms < 0 ? -1 : now - s_last_sync_ms,
    };
    const ble_adv_reason_t reason = ble_proto_adv_reason(&in);
    s_adv_reason = reason;
    if (s_synced && !connected) {
        bool active = ble_gap_adv_active();
        if (active && reason != BLE_ADV_NONE && s_adv_fast != window) {
            ble_gap_adv_stop(); /* the window opened or closed: restart at the other interval */
            active = false;
        }
        if (reason != BLE_ADV_NONE && !active) {
            adv_start(window);
        } else if (reason == BLE_ADV_NONE && active) {
            ble_gap_adv_stop();
            ESP_LOGI(TAG, "advertising stopped");
        }
    }

    /* Wake up when a time-based rule changes: the window closing, or the queue going stale. */
    int64_t next = -1;
    if (window) {
        next = window_left + 10;
    } else if (st.chunks > 0 && reason == BLE_ADV_NONE && s_last_sync_ms >= 0) {
        next = s_last_sync_ms + BLE_ADV_STALE_MS + 1000 - now;
    }
    if (next >= 0) {
        restart_timer(s_kick_timer, next);
    } else {
        esp_timer_stop(s_kick_timer);
    }

    if (connected) {
        maybe_notify_status();
    }
    uint8_t status[BLE_PROTO_STATUS_LEN];
    build_status(status); /* keeps the read cache fresh, subscribed or not */
}

/* ---- the queue, as ble_proto sees it (BLE task) ---- */

static int ensure_media(void)
{
    if (!s_media_open) {
        const esp_err_t err = chunk_store_media_open();
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "card: %s", esp_err_to_name(err));
            return BLE_PROTO_E_IO;
        }
        s_media_open = true;
    }
    return 0;
}

_Static_assert(BLE_ENTRY_LAST == CHUNK_FLAG_LAST && BLE_ENTRY_TIME_UNSET == CHUNK_FLAG_TIME_UNSET &&
                   BLE_ENTRY_NOTE == CHUNK_FLAG_NOTE,
               "ble_proto.h entry flags and chunk_store.h flags differ");

static void to_entry(const chunk_info_t *c, ble_proto_entry_t *e)
{
    e->chunk_id = c->chunk_id;
    e->flags = c->flags & (BLE_ENTRY_LAST | BLE_ENTRY_TIME_UNSET | BLE_ENTRY_NOTE);
    memcpy(e->session_id, c->session_id, sizeof(e->session_id));
    e->sequence = c->sequence;
    e->start_ms = c->start_ms;
    e->duration_ms = c->duration_ms;
    e->bytes = c->bytes;
    e->crc32 = c->crc32;
}

static size_t st_list(void *ctx, uint32_t after_id, ble_proto_entry_t *out, size_t max,
                      uint32_t *remaining)
{
    (void)ctx;
    chunk_info_t info[BLE_PROTO_LIST_BATCH];
    if (max > BLE_PROTO_LIST_BATCH) {
        max = BLE_PROTO_LIST_BATCH;
    }
    const size_t n = chunk_store_list(after_id, info, max, remaining);
    for (size_t i = 0; i < n; i++) {
        to_entry(&info[i], &out[i]);
    }
    return n;
}

static bool st_get(void *ctx, uint32_t chunk_id, ble_proto_entry_t *out)
{
    (void)ctx;
    chunk_info_t info;
    if (!chunk_store_get(chunk_id, &info)) {
        return false;
    }
    to_entry(&info, out);
    return true;
}

static int st_read(void *ctx, uint32_t chunk_id, uint32_t offset, uint8_t *buf, size_t len,
                   size_t *got)
{
    (void)ctx;
    const int rc = ensure_media();
    if (rc != 0) {
        return rc;
    }
    /* Retry before failing the READ: right after an ACK (two deletes) the card sometimes
     * answers a read late or garbled (sdspi 0x107/0x108); the same blocks read fine a
     * moment later. A failed read closes the file, so a retry reopens it cleanly. */
    static const int retry_ms[] = {20, 100, 300};
    esp_err_t err = chunk_store_read(chunk_id, offset, buf, len, got);
    for (size_t i = 0; err != ESP_OK && err != ESP_ERR_NOT_FOUND &&
                       i < sizeof(retry_ms) / sizeof(retry_ms[0]);
         i++) {
        vTaskDelay(pdMS_TO_TICKS(retry_ms[i]));
        s_stats.read_retries++;
        err = chunk_store_read(chunk_id, offset, buf, len, got);
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "read chunk %" PRIu32 " @%" PRIu32 ": ok on retry %u", chunk_id, offset,
                     (unsigned)(i + 1));
        }
    }
    if (err == ESP_ERR_NOT_FOUND) {
        return BLE_PROTO_E_NOT_FOUND;
    }
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "read chunk %" PRIu32 " @%" PRIu32 ": %s", chunk_id, offset,
                 esp_err_to_name(err));
        return BLE_PROTO_E_IO;
    }
    s_stats.bytes += *got;
    return 0;
}

static int st_ack(void *ctx, uint32_t chunk_id, bool *was_queued)
{
    (void)ctx;
    chunk_info_t info;
    if (!chunk_store_get(chunk_id, &info)) {
        *was_queued = false; /* a repeated ACK: success without touching the card */
        return 0;
    }
    const int rc = ensure_media();
    if (rc != 0) {
        return rc;
    }
    const esp_err_t err = chunk_store_ack(chunk_id, was_queued);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "ack chunk %" PRIu32 ": %s", chunk_id, esp_err_to_name(err));
        return BLE_PROTO_E_IO;
    }
    s_stats.acked++;
    return 0;
}

static int st_set_time(void *ctx, int64_t epoch_ms, uint16_t *fixed)
{
    (void)ctx;
    if (s_hooks.on_time) {
        s_hooks.on_time(epoch_ms);
    }
    chunk_store_stats_t st;
    chunk_store_stats(&st);
    if (st.chunks > 0) {
        ensure_media(); /* fix-ups rewrite records; without a card there is nothing to fix */
    }
    const int n = chunk_store_fix_time(epoch_ms);
    *fixed = (uint16_t)(n < 0 ? 0 : n > 0xffff ? 0xffff : n);
    if (n > 0) {
        ESP_LOGI(TAG, "TIME: %d chunk(s) got wall-clock times", n);
    }
    return 0;
}

static const ble_proto_store_t STORE = {
    .list = st_list,
    .get = st_get,
    .read = st_read,
    .ack = st_ack,
    .set_time = st_set_time,
};

/* ---- the BLE task ---- */

static void drop_link(uint32_t gen, uint8_t reason)
{
    const uint16_t h = handle_for(gen);
    if (h != BLE_HS_CONN_HANDLE_NONE) {
        ble_gap_terminate(h, reason);
    }
}

static void run_command(const job_t *job)
{
    taskENTER_CRITICAL(&s_lock);
    const bool current = s_conn.handle != BLE_HS_CONN_HANDLE_NONE && s_conn.gen == job->gen;
    const uint16_t mtu = s_conn.mtu;
    taskEXIT_CRITICAL(&s_lock);

    ble_proto_result_t r = BLE_PROTO_DONE;
    if (current) {
        s_stats.commands++;
        r = ble_proto_execute(&job->u.cmd, mtu, &STORE, NULL, send_data,
                              (void *)(uintptr_t)job->gen, s_frame);
    }
    taskENTER_CRITICAL(&s_lock);
    ble_proto_gate_done(&s_gate);
    taskEXIT_CRITICAL(&s_lock);
    if (!current) {
        return;
    }
    restart_timer(s_idle_timer, IDLE_DISCONNECT_MS);
    switch (r) {
    case BLE_PROTO_BYE:
        s_last_sync_ms = now_ms();
        {
            bool last;
            uint32_t ms;
            queued_after(0, &last, &ms, &s_bye_seen_id);
        }
        ESP_LOGI(TAG, "BYE: sync done (queued up to chunk %" PRIu32 ")", s_bye_seen_id);
        restart_timer(s_bye_timer, BYE_DISCONNECT_MS);
        post_kick();
        break;
    case BLE_PROTO_LINK_LOST:
        s_stats.link_lost++;
        drop_link(job->gen, BLE_ERR_REM_USER_CONN_TERM);
        break;
    default:
        if (job->u.cmd.op == BLE_OP_ACK || job->u.cmd.op == BLE_OP_TIME) {
            post_kick(); /* queue or clock changed: status */
        }
        break;
    }
}

static void handle_job(const job_t *job)
{
    switch (job->kind) {
    case JOB_CMD:
        run_command(job);
        break;
    case JOB_FRAME:
        notify(job->gen, s_data_handle, job->u.frame, BLE_PROTO_ERROR_LEN);
        break;
    case JOB_KICK:
        taskENTER_CRITICAL(&s_lock);
        s_kick_queued = false;
        taskEXIT_CRITICAL(&s_lock);
        refresh();
        break;
    case JOB_SYNCED:
        if (adv_setup() == 0) {
            s_synced = true;
        }
        refresh();
        break;
    case JOB_CONNECTED:
        s_stats.connections++;
        s_status_sent_valid = false;
        refresh_battery(true);
        refresh();
        break;
    case JOB_DISCONNECTED:
        if (s_media_open) {
            chunk_store_media_close();
            s_media_open = false;
        }
        refresh();
        break;
    case JOB_IDLE: {
        taskENTER_CRITICAL(&s_lock);
        const bool busy = s_gate.busy;
        taskEXIT_CRITICAL(&s_lock);
        if (busy) {
            restart_timer(s_idle_timer, IDLE_DISCONNECT_MS);
        } else if (handle_for(job->gen) != BLE_HS_CONN_HANDLE_NONE) {
            ESP_LOGI(TAG, "no command for %d s: disconnecting", IDLE_DISCONNECT_MS / 1000);
            drop_link(job->gen, BLE_ERR_REM_USER_CONN_TERM);
        }
        break;
    }
    case JOB_BYE_DONE:
        drop_link(job->gen, BLE_ERR_REM_USER_CONN_TERM);
        break;
    default:
        break;
    }
}

/* Task watchdog: the task waits for jobs at most JOB_WAIT_MS and feeds it every round and
 * while it streams (notify retries, READ frames), so a stuck card read or host call
 * restarts the watch with a core dump and reset=task_wdt. */
#define JOB_WAIT_MS 2000

static void ble_task(void *arg)
{
    (void)arg;
    job_t job;
    esp_task_wdt_add(NULL);
    for (;;) {
        esp_task_wdt_reset();
        if (xQueueReceive(s_jobs, &job, pdMS_TO_TICKS(JOB_WAIT_MS)) != pdTRUE) {
            continue;
        }
        handle_job(&job);
        for (size_t i = 0; i < s_n_deferred; i++) {
            handle_job(&s_deferred[i]);
        }
        s_n_deferred = 0;
    }
}

static void on_idle_timer(void *arg)
{
    (void)arg;
    post_job(JOB_IDLE, current_gen());
}

static void on_bye_timer(void *arg)
{
    (void)arg;
    post_job(JOB_BYE_DONE, current_gen());
}

static void on_kick_timer(void *arg)
{
    (void)arg;
    post_kick();
}

/* ---- NimBLE callbacks (host task): short, no SD or I2C ---- */

static bool was_bonded(const ble_addr_t *addr)
{
    for (int i = 0; i < s_n_bonded; i++) {
        if (ble_addr_cmp(&s_bonded[i], addr) == 0) {
            return true;
        }
    }
    return false;
}

static void on_connect(uint16_t handle)
{
    struct ble_gap_conn_desc desc = {0};
    ble_gap_conn_find(handle, &desc);
    s_n_bonded = 0;
    if (ble_store_util_bonded_peers(s_bonded, &s_n_bonded, CONFIG_BT_NIMBLE_MAX_BONDS) != 0) {
        s_n_bonded = 0;
    }
    taskENTER_CRITICAL(&s_lock);
    s_conn.handle = handle;
    s_conn.gen++;
    s_conn.mtu = BLE_ATT_MTU_DFLT;
    s_conn.authorized = false;
    s_conn.status_sub = false;
    s_conn.tx_phy = s_conn.rx_phy = BLE_HCI_LE_PHY_1M;
    s_conn.peer = desc.peer_id_addr;
    const uint32_t gen = s_conn.gen;
    const bool window = window_open_locked();
    taskEXIT_CRITICAL(&s_lock);
    ble_hs_cfg.sm_bonding = window ? 1 : 0;

    ESP_LOGI(TAG, "connected: %02x:%02x:%02x:%02x:%02x:%02x", desc.peer_id_addr.val[5],
             desc.peer_id_addr.val[4], desc.peer_id_addr.val[3], desc.peer_id_addr.val[2],
             desc.peer_id_addr.val[1], desc.peer_id_addr.val[0]);

    /* No HCI commands here. A bonded Windows PC starts encryption ~0.4 s after connecting;
     * an LE Set Data Length sent from this callback then went unanswered by the controller,
     * so the host timed out (BLE_HS_ETIMEOUT_HCI, ocf 0x0022), reset and dropped the link.
     * 2M PHY and long packets are controller defaults set once at sync (set_link_defaults);
     * the faster interval is asked for once the link is encrypted. */
    restart_timer(s_idle_timer, IDLE_DISCONNECT_MS);
    post_job(JOB_CONNECTED, gen);
}

/* A short burst: fast interval for the transfer. The relay may refuse it. Asked after
 * encryption, so it doesn't overlap the peer's own procedures at connect. */
static void request_fast_interval(uint16_t handle)
{
    const struct ble_gap_upd_params params = {
        .itvl_min = CONN_ITVL_MIN,
        .itvl_max = CONN_ITVL_MAX,
        .latency = 0,
        .supervision_timeout = CONN_SUPERVISION,
    };
    const int rc = ble_gap_update_params(handle, &params);
    if (rc != 0) {
        ESP_LOGD(TAG, "conn params request: %d", rc);
    }
}

/* The pairing code on the watch screen (§3.1); -1 hides it. */
static void show_passkey(int32_t passkey)
{
    if (s_hooks.on_passkey) {
        s_hooks.on_passkey(passkey);
    }
}

/* Passkey entry (§3.1): the watch displays a random 6-digit code, the relay's user types it.
 * Only in the sync window: outside it no new pairing starts at all. */
static void on_passkey_action(uint16_t handle, uint8_t action)
{
    if (action != BLE_SM_IOACT_DISP) {
        ESP_LOGW(TAG, "pairing action %u not supported (display only)", action);
        ble_gap_terminate(handle, BLE_ERR_AUTH_FAIL);
        return;
    }
    if (!ble_sync_window_is_open()) {
        ESP_LOGW(TAG, "pairing outside the sync window refused");
        ble_gap_terminate(handle, BLE_ERR_AUTH_FAIL);
        s_stats.rejected++;
        return;
    }
    struct ble_sm_io io = {.action = BLE_SM_IOACT_DISP, .passkey = esp_random() % 1000000};
    const int rc = ble_sm_inject_io(handle, &io);
    if (rc != 0) {
        ESP_LOGW(TAG, "passkey inject: %d", rc);
        return;
    }
    /* Logged too: the USB console is local, and it helps when the screen is off. */
    ESP_LOGI(TAG, "pairing code %06" PRIu32 " (type it on the phone/PC)", io.passkey);
    show_passkey((int32_t)io.passkey);
    /* Typing the code takes a while; SMP itself gives up after 30 s (Core spec, SMP timeout). */
    restart_timer(s_idle_timer, IDLE_DISCONNECT_MS + 15000);
}

static void on_enc_change(uint16_t handle, int status)
{
    show_passkey(-1);
    if (status != 0) {
        ESP_LOGW(TAG, "encryption failed: %d", status);
        return;
    }
    struct ble_gap_conn_desc desc;
    if (ble_gap_conn_find(handle, &desc) != 0) {
        return;
    }
    taskENTER_CRITICAL(&s_lock);
    const bool window = window_open_locked();
    taskEXIT_CRITICAL(&s_lock);
    if (!desc.sec_state.authenticated) {
        /* Only passkey (MITM-protected) keys open the service; a Just Works bond doesn't. */
        ESP_LOGW(TAG, "link encrypted without an authenticated key: disconnecting");
        if (!was_bonded(&desc.peer_id_addr)) {
            ble_store_util_delete_peer(&desc.peer_id_addr);
        }
        ble_gap_terminate(handle, BLE_ERR_AUTH_FAIL);
        s_stats.rejected++;
        return;
    }
    if (!window && !was_bonded(&desc.peer_id_addr)) {
        /* §3.1: outside the sync window only peers bonded before this connection. */
        ESP_LOGW(TAG, "unknown peer outside the sync window: deleting its bond, disconnecting");
        ble_store_util_delete_peer(&desc.peer_id_addr);
        ble_gap_terminate(handle, BLE_ERR_AUTH_FAIL);
        s_stats.rejected++;
        return;
    }
    taskENTER_CRITICAL(&s_lock);
    if (s_conn.handle == handle) {
        s_conn.authorized = true;
        s_conn.peer = desc.peer_id_addr;
    }
    taskEXIT_CRITICAL(&s_lock);
    ESP_LOGI(TAG, "encrypted (%s)", desc.sec_state.bonded ? "bonded" : "not bonded");
    request_fast_interval(handle);
    post_kick();
}

static int on_gap_event(struct ble_gap_event *ev, void *arg)
{
    (void)arg;
    switch (ev->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (ev->connect.status == 0) {
            on_connect(ev->connect.conn_handle);
        } else {
            ESP_LOGW(TAG, "connect failed: %d", ev->connect.status);
            post_kick();
        }
        return 0;

    case BLE_GAP_EVENT_DISCONNECT: {
        taskENTER_CRITICAL(&s_lock);
        if (s_conn.handle == ev->disconnect.conn.conn_handle) {
            s_conn.handle = BLE_HS_CONN_HANDLE_NONE;
            s_conn.authorized = false;
            s_conn.status_sub = false;
        }
        const uint32_t gen = s_conn.gen;
        taskEXIT_CRITICAL(&s_lock);
        esp_timer_stop(s_idle_timer);
        esp_timer_stop(s_bye_timer);
        esp_timer_stop(s_status_timer);
        ESP_LOGI(TAG, "disconnected: 0x%x", ev->disconnect.reason);
        show_passkey(-1);
        post_job(JOB_DISCONNECTED, gen);
        return 0;
    }

    case BLE_GAP_EVENT_PASSKEY_ACTION:
        on_passkey_action(ev->passkey.conn_handle, ev->passkey.params.action);
        return 0;

    case BLE_GAP_EVENT_ENC_CHANGE:
        on_enc_change(ev->enc_change.conn_handle, ev->enc_change.status);
        return 0;

    case BLE_GAP_EVENT_REPEAT_PAIRING: {
        /* The peer lost its keys and pairs again. Replacing a bond is a new bond. */
        if (!ble_sync_window_is_open()) {
            ESP_LOGW(TAG, "re-pairing refused outside the sync window");
            return BLE_GAP_REPEAT_PAIRING_IGNORE;
        }
        struct ble_gap_conn_desc desc;
        if (ble_gap_conn_find(ev->repeat_pairing.conn_handle, &desc) == 0) {
            ble_store_util_delete_peer(&desc.peer_id_addr);
        }
        return BLE_GAP_REPEAT_PAIRING_RETRY;
    }

    case BLE_GAP_EVENT_MTU:
        taskENTER_CRITICAL(&s_lock);
        if (s_conn.handle == ev->mtu.conn_handle) {
            s_conn.mtu = ev->mtu.value;
        }
        taskEXIT_CRITICAL(&s_lock);
        ESP_LOGI(TAG, "MTU %u", ev->mtu.value);
        return 0;

    case BLE_GAP_EVENT_SUBSCRIBE:
        if (ev->subscribe.attr_handle == s_status_handle) {
            taskENTER_CRITICAL(&s_lock);
            if (s_conn.handle == ev->subscribe.conn_handle) {
                s_conn.status_sub = ev->subscribe.cur_notify;
            }
            taskEXIT_CRITICAL(&s_lock);
            post_kick();
        }
        return 0;

    case BLE_GAP_EVENT_PHY_UPDATE_COMPLETE:
        if (ev->phy_updated.status == 0) {
            taskENTER_CRITICAL(&s_lock);
            if (s_conn.handle == ev->phy_updated.conn_handle) {
                s_conn.tx_phy = ev->phy_updated.tx_phy;
                s_conn.rx_phy = ev->phy_updated.rx_phy;
            }
            taskEXIT_CRITICAL(&s_lock);
        }
        return 0;

    case BLE_GAP_EVENT_CONN_UPDATE:
        if (ev->conn_update.status == 0) {
            struct ble_gap_conn_desc desc;
            if (ble_gap_conn_find(ev->conn_update.conn_handle, &desc) == 0) {
                ESP_LOGD(TAG, "interval %u.%02u ms, latency %u, timeout %u ms",
                         desc.conn_itvl * 125 / 100, desc.conn_itvl * 125 % 100,
                         desc.conn_latency, desc.supervision_timeout * 10);
            }
        }
        return 0;

    case BLE_GAP_EVENT_ADV_COMPLETE:
        post_kick();
        return 0;

    default:
        return 0;
    }
}

static int on_ctrl_write(uint16_t conn_handle, struct ble_gatt_access_ctxt *ctxt)
{
    /* One byte more than the longest command, so anything longer reads as bad_length. */
    uint8_t buf[BLE_PROTO_CMD_MAX + 1];
    const uint16_t pkt_len = OS_MBUF_PKTLEN(ctxt->om);
    const uint16_t len = pkt_len < sizeof(buf) ? pkt_len : sizeof(buf);
    if (os_mbuf_copydata(ctxt->om, 0, len, buf) != 0) {
        return BLE_ATT_ERR_UNLIKELY;
    }
    restart_timer(s_idle_timer, IDLE_DISCONNECT_MS);

    ble_proto_cmd_t cmd;
    uint8_t err[BLE_PROTO_ERROR_LEN];
    job_t job = {0};
    taskENTER_CRITICAL(&s_lock);
    const bool current = s_conn.handle == conn_handle;
    job.gen = s_conn.gen;
    const bool accepted =
        current && ble_proto_gate_submit(&s_gate, buf, len, s_conn.mtu, &cmd, err);
    taskEXIT_CRITICAL(&s_lock);
    if (!current) {
        return BLE_ATT_ERR_UNLIKELY;
    }
    if (accepted) {
        job.kind = JOB_CMD;
        job.u.cmd = cmd;
    } else {
        job.kind = JOB_FRAME;
        memcpy(job.u.frame, err, sizeof(err));
    }
    if (xQueueSend(s_jobs, &job, 0) != pdTRUE) {
        ESP_LOGW(TAG, "job queue full: command dropped");
        if (accepted) {
            taskENTER_CRITICAL(&s_lock);
            ble_proto_gate_done(&s_gate);
            taskEXIT_CRITICAL(&s_lock);
        }
        return BLE_ATT_ERR_UNLIKELY;
    }
    return 0;
}

static int on_access(uint16_t conn_handle, uint16_t attr_handle, struct ble_gatt_access_ctxt *ctxt,
                     void *arg)
{
    (void)attr_handle;
    const chr_id_t chr = (chr_id_t)(uintptr_t)arg;
    if (chr == CHR_INFO && ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        return os_mbuf_append(ctxt->om, s_info, strlen(s_info)) == 0
                   ? 0
                   : BLE_ATT_ERR_INSUFFICIENT_RES;
    }
    /* NimBLE has already required encryption; this refuses a link that is encrypted but
     * about to be dropped (a stranger outside the sync window). */
    taskENTER_CRITICAL(&s_lock);
    const bool ok = s_conn.handle == conn_handle && s_conn.authorized;
    uint8_t status[BLE_PROTO_STATUS_LEN];
    memcpy(status, s_status_cache, sizeof(status));
    taskEXIT_CRITICAL(&s_lock);
    if (!ok) {
        return BLE_ATT_ERR_INSUFFICIENT_AUTHOR;
    }
    if (chr == CHR_STATUS && ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        if (status[1] & BLE_STATUS_TIME_VALID) {
            struct timeval tv;
            gettimeofday(&tv, NULL);
            const uint32_t t = (uint32_t)tv.tv_sec;
            for (int i = 0; i < 4; i++) {
                status[16 + i] = (uint8_t)(t >> (8 * i));
            }
        }
        return os_mbuf_append(ctxt->om, status, sizeof(status)) == 0
                   ? 0
                   : BLE_ATT_ERR_INSUFFICIENT_RES;
    }
    if (chr == CHR_CTRL && ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
        return on_ctrl_write(conn_handle, ctxt);
    }
    return BLE_ATT_ERR_UNLIKELY;
}

/* 2M PHY and long packets for every connection, as controller defaults: the controller
 * negotiates them itself, so no per-connection HCI command races the peer at connect. */
static void set_link_defaults(void)
{
    int rc = ble_gap_set_prefered_default_le_phy(BLE_GAP_LE_PHY_1M_MASK | BLE_GAP_LE_PHY_2M_MASK,
                                                 BLE_GAP_LE_PHY_1M_MASK | BLE_GAP_LE_PHY_2M_MASK);
    if (rc != 0) {
        ESP_LOGW(TAG, "default PHY: %d", rc);
    }
    rc = ble_gap_write_sugg_def_data_len(DLE_TX_OCTETS, DLE_TX_TIME);
    if (rc != 0) {
        ESP_LOGW(TAG, "default data length: %d", rc);
    }
}

static void on_sync(void)
{
    set_link_defaults();
    post_job(JOB_SYNCED, 0);
}

static void on_reset(int reason)
{
    s_synced = false;
    s_stats.host_resets++;
    ESP_LOGW(TAG, "host reset: %d", reason);
}

static void host_task(void *param)
{
    (void)param;
    nimble_port_run(); /* returns only after nimble_port_stop() */
    nimble_port_freertos_deinit();
}

/* ---- public API ---- */

esp_err_t ble_sync_init(const ble_sync_hooks_t *hooks)
{
    if (s_jobs) {
        return ESP_ERR_INVALID_STATE;
    }
    if (hooks) {
        s_hooks = *hooks;
    }

    /* info (§4.1): the device name is the board name with dashes. */
    char device[32];
    snprintf(device, sizeof(device), "%s", board_name());
    for (char *p = device; *p; p++) {
        if (*p == '_') {
            *p = '-';
        }
    }
    int n = snprintf(s_info, sizeof(s_info), "proto=%d;device=%s;fw=%s;board=%s",
                     BLE_PROTO_VERSION, device,
                     s_hooks.fw_version ? s_hooks.fw_version : "unknown", board_name());
    if (s_hooks.reset_reason && n > 0 && (size_t)n < sizeof(s_info)) {
        snprintf(s_info + n, sizeof(s_info) - (size_t)n, ";reset=%s", s_hooks.reset_reason);
    }

    s_jobs = xQueueCreate(JOB_QUEUE_LEN, sizeof(job_t));
    if (!s_jobs) {
        return ESP_ERR_NO_MEM;
    }
    const esp_timer_create_args_t timers[] = {
        {.callback = on_idle_timer, .name = "ble_idle"},
        {.callback = on_bye_timer, .name = "ble_bye"},
        {.callback = on_kick_timer, .name = "ble_kick"},
        {.callback = on_kick_timer, .name = "ble_status"},
    };
    esp_timer_handle_t *handles[] = {&s_idle_timer, &s_bye_timer, &s_kick_timer, &s_status_timer};
    for (size_t i = 0; i < sizeof(timers) / sizeof(timers[0]); i++) {
        const esp_err_t err = esp_timer_create(&timers[i], handles[i]);
        if (err != ESP_OK) {
            return err;
        }
    }

    esp_err_t err = nimble_port_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "NimBLE init: %s", esp_err_to_name(err));
        return err;
    }
    ble_hs_cfg.sync_cb = on_sync;
    ble_hs_cfg.reset_cb = on_reset;
    /* Bond table full (4): drop the oldest bond to make room for the new one. */
    ble_hs_cfg.store_status_cb = ble_store_util_status_rr;
    /* Passkey entry with LE Secure Connections (legacy pairing is compiled out): the watch
     * shows a 6-digit code, the relay's user types it, so pairing is MITM-protected.
     * Bonding only while the sync window is open (refresh()). Identity keys are exchanged so
     * phones with private addresses are recognised when they come back. */
    ble_hs_cfg.sm_io_cap = BLE_HS_IO_DISPLAY_ONLY;
    ble_hs_cfg.sm_bonding = 0;
    ble_hs_cfg.sm_mitm = 1;
    ble_hs_cfg.sm_sc = 1;
    ble_hs_cfg.sm_our_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
    ble_hs_cfg.sm_their_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;

    ble_svc_gap_init();
    ble_svc_gatt_init();
    int rc = ble_gatts_count_cfg(s_svcs);
    if (rc == 0) {
        rc = ble_gatts_add_svcs(s_svcs);
    }
    if (rc == 0) {
        rc = ble_svc_gap_device_name_set(DEVICE_NAME);
    }
    if (rc == 0) {
        rc = ble_att_set_preferred_mtu(BLE_PROTO_MAX_MTU);
    }
    if (rc != 0) {
        ESP_LOGE(TAG, "GATT setup: %d", rc);
        return ESP_FAIL;
    }
    ble_store_config_init();

    if (xTaskCreate(ble_task, "ble_sync", TASK_STACK, NULL, TASK_PRIO, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    nimble_port_freertos_init(host_task);
    ESP_LOGI(TAG, "ready: %s", s_info);
    return ESP_OK;
}

void ble_sync_open_window(void)
{
    taskENTER_CRITICAL(&s_lock);
    s_window_until_ms = now_ms() + WINDOW_MS;
    taskEXIT_CRITICAL(&s_lock);
    ble_hs_cfg.sm_bonding = 1;
    ESP_LOGI(TAG, "sync window open for %d s: advertising, new bonds accepted", WINDOW_MS / 1000);
    post_kick();
}

void ble_sync_kick(void)
{
    post_kick();
}

esp_err_t ble_sync_forget_bonds(void)
{
    const int rc = ble_store_clear();
    if (rc != 0) {
        ESP_LOGE(TAG, "forget bonds: %d", rc);
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "all bonds deleted");
    return ESP_OK;
}

static const char *phy_name(uint8_t phy)
{
    switch (phy) {
    case BLE_HCI_LE_PHY_1M: return "1M";
    case BLE_HCI_LE_PHY_2M: return "2M";
    case BLE_HCI_LE_PHY_CODED: return "coded";
    default: return "?";
    }
}

void ble_sync_print(void)
{
    taskENTER_CRITICAL(&s_lock);
    const conn_t c = s_conn;
    const int64_t window_left = s_window_until_ms - now_ms();
    taskEXIT_CRITICAL(&s_lock);

    int bonds = 0;
    ble_store_util_count(BLE_STORE_OBJ_TYPE_OUR_SEC, &bonds);
    printf("ble: %s, advertising %s (rule: %s)\n", s_synced ? "up" : "starting",
           s_synced && ble_gap_adv_active() ? "on" : "off", ble_proto_adv_reason_name(s_adv_reason));
    if (c.handle != BLE_HS_CONN_HANDLE_NONE) {
        printf("connected: %02x:%02x:%02x:%02x:%02x:%02x, %s, MTU %u, PHY tx %s rx %s\n",
               c.peer.val[5], c.peer.val[4], c.peer.val[3], c.peer.val[2], c.peer.val[1],
               c.peer.val[0], c.authorized ? "encrypted" : "not encrypted yet", c.mtu,
               phy_name(c.tx_phy), phy_name(c.rx_phy));
    } else {
        printf("connected: no\n");
    }
    if (window_left > 0) {
        printf("sync window: open, %" PRId64 " s left\n", (window_left + 999) / 1000);
    } else {
        printf("sync window: closed\n");
    }
    printf("bonds: %d of %d\n", bonds, CONFIG_BT_NIMBLE_MAX_BONDS);
    if (s_last_sync_ms < 0) {
        printf("last sync: none since boot\n");
    } else {
        printf("last sync: %" PRId64 " s ago\n", (now_ms() - s_last_sync_ms) / 1000);
    }
    printf("counters: %" PRIu32 " connections, %" PRIu32 " refused, %" PRIu32 " commands, %" PRIu32
           " frames, %" PRIu64 " bytes read, %" PRIu32 " acked, %" PRIu32 " send retries, %" PRIu32
           " links lost, %" PRIu32 " host resets, %" PRIu32 " SD read retries\n",
           s_stats.connections, s_stats.rejected, s_stats.commands, s_stats.frames, s_stats.bytes,
           s_stats.acked, s_stats.retries, s_stats.link_lost, s_stats.host_resets,
           s_stats.read_retries);
}
