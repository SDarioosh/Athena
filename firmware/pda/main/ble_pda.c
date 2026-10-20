/* NimBLE glue for the glance protocol (docs/t5-pda-plan.md §3), in the style of the
 * recorder's ble_sync.c: NimBLE callbacks run in the host task, update the small shared
 * connection state under s_lock and post jobs; commands run in the BLE task, one at a time.
 *
 * PUT_DATA is the exception: it is copied into the PUT buffer right in the GATT write
 * callback, so the write response only goes back once the bytes are in (that is the flow
 * control, §3.3) and the next write can't overtake it. The PUT state is under s_put_lock;
 * PUT_BEGIN and PUT_END run in the BLE task under the same lock. */
#include "ble_pda.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <sys/time.h>

#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
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

#define DEVICE_NAME "odysseus-pda"
/* The scan response holds 31 bytes: with the 19-byte service data the name is shortened. */
#define SHORT_NAME "odysseus-p"
#define WINDOW_MS (2 * 60 * 1000)
#define IDLE_DISCONNECT_MS 30000
#define ACTIVE_IDLE_DISCONNECT_MS (6 * 60 * 1000) /* the relay refreshes every 5 min */
#define BYE_DISCONNECT_MS 1000
#define STATUS_MIN_PERIOD_MS 1000
#define SEND_GIVE_UP_MS 5000
#define SEND_BACKOFF_MAX_MS 32
#define MSYS_RESERVE 8
#define ADV_ITVL_MIN 1600     /* 1000 ms */
#define ADV_ITVL_MAX 2000     /* 1250 ms */
#define ADV_FAST_ITVL_MIN 244 /* 152.5 ms, in the pairing window */
#define ADV_FAST_ITVL_MAX 338 /* 211.25 ms */
#define CONN_ITVL_MIN 12
#define CONN_ITVL_MAX 24
#define CONN_SUPERVISION 400
#define DLE_TX_OCTETS 251
#define DLE_TX_TIME 2120
#define JOB_QUEUE_LEN 12
#define TASK_STACK 8192
#define TASK_PRIO 5

/* d023xxxx-51f4-43be-86e4-1653768283cd, little-endian as NimBLE stores it. */
#define PDA_UUID(n)                                                                          \
    BLE_UUID128_INIT(0xcd, 0x83, 0x82, 0x76, 0x53, 0x16, 0xe4, 0x86, 0xbe, 0x43, 0xf4, 0x51, \
                     (n) & 0xff, ((n) >> 8) & 0xff, 0x23, 0xd0)

static const ble_uuid128_t s_svc_uuid = PDA_UUID(0x0000);
static const ble_uuid128_t s_info_uuid = PDA_UUID(0x0001);
static const ble_uuid128_t s_status_uuid = PDA_UUID(0x0002);
static const ble_uuid128_t s_ctrl_uuid = PDA_UUID(0x0003);
static const ble_uuid128_t s_data_uuid = PDA_UUID(0x0004);

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

/* ---- shared state: under s_lock ---- */
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
typedef struct {
    uint16_t handle;
    uint32_t gen;
    uint16_t mtu;
    bool authorized;
    bool status_sub;
    ble_addr_t peer;
} conn_t;
static conn_t s_conn = {.handle = BLE_HS_CONN_HANDLE_NONE};
static pda_gate_t s_gate;
static int64_t s_window_until_ms;
static bool s_kick_queued;
static uint8_t s_status_cache[PDA_PROTO_STATUS_LEN];
static int64_t s_last_sync_s;

/* ---- the PUT in progress: under s_put_lock ---- */
static SemaphoreHandle_t s_put_lock;
static pda_put_t s_put;

/* ---- host task only ---- */
static ble_addr_t s_bonded[CONFIG_BT_NIMBLE_MAX_BONDS];
static int s_n_bonded;

/* ---- BLE task ---- */
static ble_pda_hooks_t s_hooks;
static const pda_store_t *s_store;
static void *s_store_ctx;
static QueueHandle_t s_jobs;
static esp_timer_handle_t s_idle_timer, s_bye_timer, s_kick_timer, s_status_timer;
static volatile bool s_synced;
static uint8_t s_own_addr_type;
static bool s_adv_fast;
static uint8_t s_adv_flags = 0xff; /* scan response content last set */
static uint8_t s_status_sent[PDA_PROTO_STATUS_LEN];
static bool s_status_sent_valid;
static int64_t s_status_sent_ms;
static uint8_t s_frame[PDA_PROTO_FRAME_MAX];
static char s_info[96];
static uint32_t s_boot_id;
static struct {
    uint32_t connections, rejected, commands, put_bytes, puts, put_errors, link_lost,
        host_resets, retries;
} s_stats;

typedef enum {
    JOB_CMD,
    JOB_FRAME,
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
        pda_cmd_t cmd; /* never PUT_DATA (its payload pointer would dangle) */
        uint8_t frame[PDA_PROTO_ERROR_LEN];
    } u;
} job_t;

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

static void restart_timer(esp_timer_handle_t t, int64_t ms)
{
    esp_timer_stop(t);
    esp_timer_start_once(t, (uint64_t)(ms > 0 ? ms : 1) * 1000);
}

static bool post(const job_t *job)
{
    if (!s_jobs || xQueueSend(s_jobs, job, 0) != pdTRUE) {
        ESP_LOGW(TAG, "job queue full, dropped %d", (int)job->kind);
        return false;
    }
    return true;
}

static bool post_job(job_kind_t kind, uint32_t gen)
{
    const job_t job = {.kind = kind, .gen = gen};
    return post(&job);
}

static uint32_t current_gen(void)
{
    taskENTER_CRITICAL(&s_lock);
    const uint32_t gen = s_conn.gen;
    taskEXIT_CRITICAL(&s_lock);
    return gen;
}

static void post_kick(void)
{
    taskENTER_CRITICAL(&s_lock);
    const bool p = !s_kick_queued;
    s_kick_queued = true;
    taskEXIT_CRITICAL(&s_lock);
    if (p && !post_job(JOB_KICK, 0)) {
        taskENTER_CRITICAL(&s_lock);
        s_kick_queued = false;
        taskEXIT_CRITICAL(&s_lock);
    }
}

static bool window_open_locked(void)
{
    return now_ms() < s_window_until_ms;
}

bool ble_pda_window_is_open(void)
{
    taskENTER_CRITICAL(&s_lock);
    const bool open = window_open_locked();
    taskEXIT_CRITICAL(&s_lock);
    return open;
}

int ble_pda_window_left_s(void)
{
    taskENTER_CRITICAL(&s_lock);
    const int64_t left = s_window_until_ms - now_ms();
    taskEXIT_CRITICAL(&s_lock);
    return left > 0 ? (int)((left + 999) / 1000) : 0;
}

bool ble_pda_is_connected(void)
{
    taskENTER_CRITICAL(&s_lock);
    const bool c = s_conn.handle != BLE_HS_CONN_HANDLE_NONE && s_conn.authorized;
    taskEXIT_CRITICAL(&s_lock);
    return c;
}

int64_t ble_pda_last_sync(void)
{
    taskENTER_CRITICAL(&s_lock);
    const int64_t t = s_last_sync_s;
    taskEXIT_CRITICAL(&s_lock);
    return t;
}

static void app_state(ble_pda_state_t *st)
{
    *st = (ble_pda_state_t){.battery_pct = -1};
    if (s_hooks.get_state) {
        s_hooks.get_state(st);
    }
}

/* ---- status ---- */

static void build_status(uint8_t out[PDA_PROTO_STATUS_LEN])
{
    ble_pda_state_t app;
    app_state(&app);
    pda_status_t s = {
        .battery_pct = app.battery_pct,
        .outbox = app.outbox,
        .feed_version = app.feed_version,
        .boot_id = s_boot_id,
    };
    s.flags = (app.time_valid ? PDA_STATUS_TIME_VALID : 0) | (app.charging ? PDA_STATUS_CHARGING : 0) |
              (app.usb ? PDA_STATUS_USB : 0) | (ble_pda_window_is_open() ? PDA_STATUS_WINDOW : 0) |
              (app.user_active ? PDA_STATUS_ACTIVE : 0);
    pda_encode_status(&s, out);
    taskENTER_CRITICAL(&s_lock);
    memcpy(s_status_cache, out, PDA_PROTO_STATUS_LEN);
    taskEXIT_CRITICAL(&s_lock);
}

static uint16_t handle_for(uint32_t gen)
{
    taskENTER_CRITICAL(&s_lock);
    const uint16_t h = s_conn.gen == gen ? s_conn.handle : BLE_HS_CONN_HANDLE_NONE;
    taskEXIT_CRITICAL(&s_lock);
    return h;
}

static int notify(uint32_t gen, uint16_t attr, const uint8_t *buf, size_t len)
{
    const int64_t give_up = now_ms() + SEND_GIVE_UP_MS;
    int backoff = 1;
    for (;;) {
        const uint16_t h = handle_for(gen);
        if (h == BLE_HS_CONN_HANDLE_NONE) {
            return -1;
        }
        if (os_msys_num_free() >= MSYS_RESERVE) {
            struct os_mbuf *om = ble_hs_mbuf_from_flat(buf, (uint16_t)len);
            if (om) {
                const int rc = ble_gatts_notify_custom(h, attr, om);
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

static int send_data(void *ctx, const uint8_t *frame, size_t len)
{
    return notify((uint32_t)(uintptr_t)ctx, s_data_handle, frame, len);
}

static void maybe_notify_status(void)
{
    taskENTER_CRITICAL(&s_lock);
    const bool want = s_conn.handle != BLE_HS_CONN_HANDLE_NONE && s_conn.authorized && s_conn.status_sub;
    const uint32_t gen = s_conn.gen;
    taskEXIT_CRITICAL(&s_lock);
    uint8_t v[PDA_PROTO_STATUS_LEN];
    build_status(v);
    if (!want) {
        return;
    }
    if (s_status_sent_valid && memcmp(v, s_status_sent, sizeof(v)) == 0) {
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

/* ---- advertising: always while not connected (the relay reconnects every 15 min) ---- */

static int set_scan_response(uint8_t flags)
{
    /* 16-byte UUID followed by the flags byte (AD type 0x21). */
    uint8_t svc_data[17];
    memcpy(svc_data, s_svc_uuid.value, 16);
    svc_data[16] = flags;
    struct ble_hs_adv_fields rsp = {0};
    if (flags) {
        rsp.svc_data_uuid128 = svc_data;
        rsp.svc_data_uuid128_len = sizeof(svc_data);
        rsp.name = (const uint8_t *)SHORT_NAME;
        rsp.name_len = sizeof(SHORT_NAME) - 1;
        rsp.name_is_complete = 0;
    } else {
        rsp.name = (const uint8_t *)DEVICE_NAME;
        rsp.name_len = sizeof(DEVICE_NAME) - 1;
        rsp.name_is_complete = 1;
    }
    const int rc = ble_gap_adv_rsp_set_fields(&rsp);
    if (rc != 0) {
        ESP_LOGE(TAG, "scan response: %d", rc);
    }
    return rc;
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
    const struct ble_hs_adv_fields adv = {
        .flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP,
        .uuids128 = &s_svc_uuid,
        .num_uuids128 = 1,
        .uuids128_is_complete = 1,
    };
    rc = ble_gap_adv_set_fields(&adv);
    if (rc != 0) {
        ESP_LOGE(TAG, "advertising data: %d", rc);
    }
    return rc;
}

static void adv_start(bool fast)
{
    const struct ble_gap_adv_params params = {
        .conn_mode = BLE_GAP_CONN_MODE_UND,
        .disc_mode = BLE_GAP_DISC_MODE_GEN,
        .itvl_min = fast ? ADV_FAST_ITVL_MIN : ADV_ITVL_MIN,
        .itvl_max = fast ? ADV_FAST_ITVL_MAX : ADV_ITVL_MAX,
    };
    const int rc = ble_gap_adv_start(s_own_addr_type, NULL, BLE_HS_FOREVER, &params, on_gap_event, NULL);
    if (rc != 0) {
        ESP_LOGW(TAG, "advertising start failed: %d", rc);
    } else {
        s_adv_fast = fast;
        ESP_LOGI(TAG, "advertising%s%s", fast ? " (pairing window)" : "",
                 s_adv_flags ? ", outbox waiting" : "");
    }
}

/* Re-evaluate advertising, bonding and status. BLE task only. */
static void refresh(void)
{
    taskENTER_CRITICAL(&s_lock);
    const bool connected = s_conn.handle != BLE_HS_CONN_HANDLE_NONE;
    const bool window = window_open_locked();
    const int64_t window_left = s_window_until_ms - now_ms();
    taskEXIT_CRITICAL(&s_lock);
    ble_hs_cfg.sm_bonding = window ? 1 : 0;

    ble_pda_state_t app;
    app_state(&app);
    const uint8_t flags = pda_adv_flags(app.outbox);
    if (s_synced && !connected) {
        bool active = ble_gap_adv_active();
        if (active && (s_adv_fast != window || flags != s_adv_flags)) {
            ble_gap_adv_stop();
            active = false;
        }
        if (!active) {
            if (flags != s_adv_flags && set_scan_response(flags) == 0) {
                s_adv_flags = flags;
            }
            adv_start(window);
        }
    }
    if (window) {
        restart_timer(s_kick_timer, window_left + 10);
    }
    maybe_notify_status();
}

/* ---- the BLE task ---- */

static void drop_link(uint32_t gen, uint8_t reason)
{
    const uint16_t h = handle_for(gen);
    if (h != BLE_HS_CONN_HANDLE_NONE) {
        ble_gap_terminate(h, reason);
    }
}

static int idle_timeout_ms(void)
{
    ble_pda_state_t app;
    app_state(&app);
    return app.user_active ? ACTIVE_IDLE_DISCONNECT_MS : IDLE_DISCONNECT_MS;
}

static void run_command(const job_t *job)
{
    taskENTER_CRITICAL(&s_lock);
    const bool current = s_conn.handle != BLE_HS_CONN_HANDLE_NONE && s_conn.gen == job->gen;
    taskEXIT_CRITICAL(&s_lock);

    pda_result_t r = PDA_DONE;
    if (current) {
        s_stats.commands++;
        xSemaphoreTake(s_put_lock, portMAX_DELAY);
        r = pda_execute(&job->u.cmd, &s_put, s_store, s_store_ctx, send_data,
                        (void *)(uintptr_t)job->gen, s_frame);
        xSemaphoreGive(s_put_lock);
        if (job->u.cmd.op == PDA_OP_PUT_END) {
            s_stats.puts++;
        }
    }
    taskENTER_CRITICAL(&s_lock);
    pda_gate_done(&s_gate);
    taskEXIT_CRITICAL(&s_lock);
    if (!current) {
        return;
    }
    restart_timer(s_idle_timer, idle_timeout_ms());
    switch (r) {
    case PDA_BYE: {
        struct timeval tv;
        gettimeofday(&tv, NULL);
        taskENTER_CRITICAL(&s_lock);
        s_last_sync_s = tv.tv_sec;
        taskEXIT_CRITICAL(&s_lock);
        ESP_LOGI(TAG, "BYE: sync done");
        restart_timer(s_bye_timer, BYE_DISCONNECT_MS);
        break;
    }
    case PDA_LINK_LOST:
        s_stats.link_lost++;
        drop_link(job->gen, BLE_ERR_REM_USER_CONN_TERM);
        break;
    default:
        break;
    }
    post_kick(); /* outbox, feed or clock may have changed */
}

static void handle_job(const job_t *job)
{
    switch (job->kind) {
    case JOB_CMD:
        run_command(job);
        break;
    case JOB_FRAME:
        notify(job->gen, s_data_handle, job->u.frame, PDA_PROTO_ERROR_LEN);
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
            s_adv_flags = 0xff;
        }
        refresh();
        break;
    case JOB_CONNECTED:
        s_stats.connections++;
        s_status_sent_valid = false;
        refresh();
        break;
    case JOB_DISCONNECTED:
        refresh();
        break;
    case JOB_IDLE: {
        taskENTER_CRITICAL(&s_lock);
        const bool busy = s_gate.busy;
        taskEXIT_CRITICAL(&s_lock);
        if (busy) {
            restart_timer(s_idle_timer, IDLE_DISCONNECT_MS);
        } else if (handle_for(job->gen) != BLE_HS_CONN_HANDLE_NONE) {
            ESP_LOGI(TAG, "no command for a while: disconnecting");
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

static void ble_task(void *arg)
{
    (void)arg;
    job_t job;
    for (;;) {
        if (xQueueReceive(s_jobs, &job, portMAX_DELAY) == pdTRUE) {
            handle_job(&job);
        }
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

/* ---- NimBLE callbacks (host task) ---- */

static bool was_bonded(const ble_addr_t *addr)
{
    for (int i = 0; i < s_n_bonded; i++) {
        if (ble_addr_cmp(&s_bonded[i], addr) == 0) {
            return true;
        }
    }
    return false;
}

static void show_passkey(int32_t passkey)
{
    if (s_hooks.on_passkey) {
        s_hooks.on_passkey(passkey);
    }
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
    s_conn.peer = desc.peer_id_addr;
    const uint32_t gen = s_conn.gen;
    const bool window = window_open_locked();
    taskEXIT_CRITICAL(&s_lock);
    ble_hs_cfg.sm_bonding = window ? 1 : 0;
    ESP_LOGI(TAG, "connected: %02x:%02x:%02x:%02x:%02x:%02x", desc.peer_id_addr.val[5],
             desc.peer_id_addr.val[4], desc.peer_id_addr.val[3], desc.peer_id_addr.val[2],
             desc.peer_id_addr.val[1], desc.peer_id_addr.val[0]);
    /* No HCI commands here (see the recorder's ble_sync.c: a bonded Windows PC raced them). */
    restart_timer(s_idle_timer, IDLE_DISCONNECT_MS);
    post_job(JOB_CONNECTED, gen);
}

static void on_passkey_action(uint16_t handle, uint8_t action)
{
    if (action != BLE_SM_IOACT_DISP) {
        ESP_LOGW(TAG, "pairing action %u not supported (display only)", action);
        ble_gap_terminate(handle, BLE_ERR_AUTH_FAIL);
        return;
    }
    if (!ble_pda_window_is_open()) {
        ESP_LOGW(TAG, "pairing outside the pairing window refused");
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
    ESP_LOGI(TAG, "pairing code %06" PRIu32 " (type it on the phone/PC)", io.passkey);
    show_passkey((int32_t)io.passkey);
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
    const bool window = ble_pda_window_is_open();
    if (!desc.sec_state.authenticated) {
        ESP_LOGW(TAG, "link encrypted without an authenticated key: disconnecting");
        if (!was_bonded(&desc.peer_id_addr)) {
            ble_store_util_delete_peer(&desc.peer_id_addr);
        }
        ble_gap_terminate(handle, BLE_ERR_AUTH_FAIL);
        s_stats.rejected++;
        return;
    }
    if (!window && !was_bonded(&desc.peer_id_addr)) {
        ESP_LOGW(TAG, "unknown peer outside the pairing window: deleting its bond, disconnecting");
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
    const struct ble_gap_upd_params params = {
        .itvl_min = CONN_ITVL_MIN,
        .itvl_max = CONN_ITVL_MAX,
        .latency = 0,
        .supervision_timeout = CONN_SUPERVISION,
    };
    ble_gap_update_params(handle, &params);
    if (s_hooks.on_link) {
        s_hooks.on_link(true);
    }
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
        const bool was_auth = s_conn.authorized;
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
        /* A disconnect mid-PUT discards it (§3.3). */
        xSemaphoreTake(s_put_lock, portMAX_DELAY);
        if (s_put.active) {
            ESP_LOGW(TAG, "link lost during a PUT: discarded");
        }
        pda_put_abort(&s_put);
        xSemaphoreGive(s_put_lock);
        ESP_LOGI(TAG, "disconnected: 0x%x", ev->disconnect.reason);
        show_passkey(-1);
        if (was_auth && s_hooks.on_link) {
            s_hooks.on_link(false);
        }
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
        if (!ble_pda_window_is_open()) {
            ESP_LOGW(TAG, "re-pairing refused outside the pairing window");
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
    case BLE_GAP_EVENT_ADV_COMPLETE:
        post_kick();
        return 0;
    default:
        return 0;
    }
}

/* ERROR answers decided in the host task go out through the BLE task. */
static void post_error_frame(uint32_t gen, const uint8_t frame[PDA_PROTO_ERROR_LEN])
{
    job_t job = {.kind = JOB_FRAME, .gen = gen};
    memcpy(job.u.frame, frame, PDA_PROTO_ERROR_LEN);
    post(&job);
}

static int inline_send(void *ctx, const uint8_t *frame, size_t len)
{
    if (len == PDA_PROTO_ERROR_LEN) {
        post_error_frame((uint32_t)(uintptr_t)ctx, frame);
    }
    return 0;
}

static int on_ctrl_write(uint16_t conn_handle, struct ble_gatt_access_ctxt *ctxt)
{
    /* One byte more than the largest write, so anything longer reads as bad_length. */
    static uint8_t buf[PDA_PROTO_FRAME_MAX + 1]; /* host task only */
    const uint16_t pkt_len = OS_MBUF_PKTLEN(ctxt->om);
    const uint16_t len = pkt_len < sizeof(buf) ? pkt_len : sizeof(buf);
    if (os_mbuf_copydata(ctxt->om, 0, len, buf) != 0) {
        return BLE_ATT_ERR_UNLIKELY;
    }
    pda_cmd_t cmd;
    uint8_t err[PDA_PROTO_ERROR_LEN];
    taskENTER_CRITICAL(&s_lock);
    const bool current = s_conn.handle == conn_handle;
    const uint32_t gen = s_conn.gen;
    const bool accepted = current && pda_gate_submit(&s_gate, buf, len, s_conn.mtu, &cmd, err);
    taskEXIT_CRITICAL(&s_lock);
    if (!current) {
        return BLE_ATT_ERR_UNLIKELY;
    }
    restart_timer(s_idle_timer, idle_timeout_ms());
    if (!accepted) {
        post_error_frame(gen, err);
        return 0;
    }
    if (cmd.op == PDA_OP_PUT_DATA) {
        xSemaphoreTake(s_put_lock, portMAX_DELAY);
        pda_execute(&cmd, &s_put, s_store, s_store_ctx, inline_send, (void *)(uintptr_t)gen, NULL);
        xSemaphoreGive(s_put_lock);
        s_stats.put_bytes += cmd.data_len;
        taskENTER_CRITICAL(&s_lock);
        pda_gate_done(&s_gate);
        taskEXIT_CRITICAL(&s_lock);
        return 0;
    }
    job_t job = {.kind = JOB_CMD, .gen = gen};
    job.u.cmd = cmd;
    if (!post(&job)) {
        taskENTER_CRITICAL(&s_lock);
        pda_gate_done(&s_gate);
        taskEXIT_CRITICAL(&s_lock);
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
        return os_mbuf_append(ctxt->om, s_info, strlen(s_info)) == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
    }
    taskENTER_CRITICAL(&s_lock);
    const bool ok = s_conn.handle == conn_handle && s_conn.authorized;
    uint8_t status[PDA_PROTO_STATUS_LEN];
    memcpy(status, s_status_cache, sizeof(status));
    taskEXIT_CRITICAL(&s_lock);
    if (!ok) {
        return BLE_ATT_ERR_INSUFFICIENT_AUTHOR;
    }
    if (chr == CHR_STATUS && ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        post_kick(); /* refresh the cache for the next read */
        return os_mbuf_append(ctxt->om, status, sizeof(status)) == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
    }
    if (chr == CHR_CTRL && ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
        return on_ctrl_write(conn_handle, ctxt);
    }
    return BLE_ATT_ERR_UNLIKELY;
}

static void on_sync(void)
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
    nimble_port_run();
    nimble_port_freertos_deinit();
}

esp_err_t ble_pda_init(const pda_store_t *store, void *store_ctx, const ble_pda_hooks_t *hooks)
{
    if (s_jobs) {
        return ESP_ERR_INVALID_STATE;
    }
    s_store = store;
    s_store_ctx = store_ctx;
    if (hooks) {
        s_hooks = *hooks;
    }
    s_boot_id = esp_random();
    snprintf(s_info, sizeof(s_info), "proto=%d;device=t5-pda;fw=%s;board=t5_epaper_pro",
             PDA_PROTO_VERSION, s_hooks.fw_version ? s_hooks.fw_version : "unknown");
    s_put_lock = xSemaphoreCreateMutex();
    s_jobs = xQueueCreate(JOB_QUEUE_LEN, sizeof(job_t));
    if (!s_jobs || !s_put_lock) {
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
    ble_hs_cfg.store_status_cb = ble_store_util_status_rr;
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
        rc = ble_att_set_preferred_mtu(PDA_PROTO_MAX_MTU);
    }
    if (rc != 0) {
        ESP_LOGE(TAG, "GATT setup: %d", rc);
        return ESP_FAIL;
    }
    ble_store_config_init();
    if (xTaskCreate(ble_task, "ble_pda", TASK_STACK, NULL, TASK_PRIO, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    nimble_port_freertos_init(host_task);
    ESP_LOGI(TAG, "ready: %s", s_info);
    return ESP_OK;
}

void ble_pda_open_window(void)
{
    taskENTER_CRITICAL(&s_lock);
    s_window_until_ms = now_ms() + WINDOW_MS;
    taskEXIT_CRITICAL(&s_lock);
    ble_hs_cfg.sm_bonding = 1;
    ESP_LOGI(TAG, "pairing window open for %d s: new bonds accepted", WINDOW_MS / 1000);
    post_kick();
}

void ble_pda_kick(void)
{
    post_kick();
}

esp_err_t ble_pda_forget_bonds(void)
{
    const int rc = ble_store_clear();
    if (rc != 0) {
        ESP_LOGE(TAG, "forget bonds: %d", rc);
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "all bonds deleted");
    return ESP_OK;
}

int ble_pda_bond_count(void)
{
    int n = 0;
    ble_store_util_count(BLE_STORE_OBJ_TYPE_OUR_SEC, &n);
    return n;
}

void ble_pda_print(void)
{
    taskENTER_CRITICAL(&s_lock);
    const conn_t c = s_conn;
    taskEXIT_CRITICAL(&s_lock);
    printf("ble: %s, advertising %s%s\n", s_synced ? "up" : "starting",
           s_synced && ble_gap_adv_active() ? "on" : "off",
           s_adv_flags == PDA_ADV_OUTBOX ? " (outbox waiting)" : "");
    if (c.handle != BLE_HS_CONN_HANDLE_NONE) {
        printf("connected: %02x:%02x:%02x:%02x:%02x:%02x, %s, MTU %u\n", c.peer.val[5], c.peer.val[4],
               c.peer.val[3], c.peer.val[2], c.peer.val[1], c.peer.val[0],
               c.authorized ? "authenticated" : "not authenticated yet", c.mtu);
    } else {
        printf("connected: no\n");
    }
    const int left = ble_pda_window_left_s();
    printf("pairing window: %s", left > 0 ? "open" : "closed\n");
    if (left > 0) {
        printf(", %d s left\n", left);
    }
    printf("bonds: %d of %d\n", ble_pda_bond_count(), CONFIG_BT_NIMBLE_MAX_BONDS);
    printf("counters: %" PRIu32 " connections, %" PRIu32 " refused, %" PRIu32 " commands, %" PRIu32
           " PUTs, %" PRIu32 " PUT bytes, %" PRIu32 " links lost, %" PRIu32 " send retries, %" PRIu32
           " host resets\n",
           s_stats.connections, s_stats.rejected, s_stats.commands, s_stats.puts, s_stats.put_bytes,
           s_stats.link_lost, s_stats.retries, s_stats.host_resets);
}
