#include "event_log_store.h"

#include <stdio.h>
#include <sys/time.h>

#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "isotime.h"
#include "nvs.h"

static const char *TAG = "eventlog";

#define NS "evlog"
#define KEY_NEXT "next"
#define LOCK_MS 500

_Static_assert(65536 % EVENT_LOG_CAP == 0, "the 16-bit entry counter must wrap on a slot 0");

static nvs_handle_t s_nvs;
static bool s_ready;
static el_ring_t s_ring;
static SemaphoreHandle_t s_lock;
static StaticSemaphore_t s_lock_buf;

static void block_key(unsigned block, char key[8])
{
    snprintf(key, 8, "eb%02u", block % EVENT_LOG_BLOCKS);
}

void event_log_init(void)
{
    if (s_ready) {
        return;
    }
    s_lock = xSemaphoreCreateMutexStatic(&s_lock_buf);
    const esp_err_t err = nvs_open(NS, NVS_READWRITE, &s_nvs);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "NVS: %s: no event log", esp_err_to_name(err));
        return;
    }
    uint16_t next = 0;
    nvs_get_u16(s_nvs, KEY_NEXT, &next);
    el_ring_init(&s_ring, next);
    for (unsigned b = 0; b < EVENT_LOG_BLOCKS; b++) {
        char key[8];
        block_key(b, key);
        size_t len = sizeof(el_entry_t) * EVENT_LOG_BLOCK;
        if (nvs_get_blob(s_nvs, key, &s_ring.e[b * EVENT_LOG_BLOCK], &len) != ESP_OK ||
            len != sizeof(el_entry_t) * EVENT_LOG_BLOCK) {
            /* Never written (or another size): empty. */
            for (unsigned i = 0; i < EVENT_LOG_BLOCK; i++) {
                s_ring.e[b * EVENT_LOG_BLOCK + i] = (el_entry_t){0};
            }
        }
    }
    s_ready = true;
}

static el_reset_t reset_reason(void)
{
    switch (esp_reset_reason()) {
    case ESP_RST_POWERON:
        return EL_RST_POWERON;
    case ESP_RST_EXT:
        return EL_RST_EXT;
    case ESP_RST_SW:
        return EL_RST_SW;
    case ESP_RST_PANIC:
        return EL_RST_PANIC;
    case ESP_RST_INT_WDT:
        return EL_RST_INT_WDT;
    case ESP_RST_TASK_WDT:
        return EL_RST_TASK_WDT;
    case ESP_RST_WDT:
        return EL_RST_WDT;
    case ESP_RST_DEEPSLEEP:
        return EL_RST_DEEPSLEEP;
    case ESP_RST_BROWNOUT:
        return EL_RST_BROWNOUT;
    case ESP_RST_USB:
        return EL_RST_USB;
    case ESP_RST_JTAG:
        return EL_RST_JTAG;
    case ESP_RST_UNKNOWN:
        return EL_RST_UNKNOWN;
    default:
        return EL_RST_OTHER;
    }
}

void event_log_add(el_kind_t kind, int why, int32_t arg, uint8_t aux, int batt)
{
    if (!s_ready) {
        return;
    }
    struct timeval tv;
    gettimeofday(&tv, NULL);
    const el_entry_t e = {
        .t = isotime_is_plausible(tv.tv_sec) ? (uint32_t)tv.tv_sec : 0,
        .up_s = (uint32_t)(esp_timer_get_time() / 1000000),
        .arg = arg,
        .kind = (uint8_t)kind,
        .why = (uint8_t)why,
        .batt = (int8_t)(batt >= 0 && batt <= 100 ? batt : -1),
        .aux = aux,
    };
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(LOCK_MS)) != pdTRUE) {
        ESP_LOGW(TAG, "busy: entry dropped");
        return;
    }
    const unsigned block = el_ring_add(&s_ring, &e);
    char key[8];
    block_key(block, key);
    esp_err_t err = nvs_set_blob(s_nvs, key, &s_ring.e[block * EVENT_LOG_BLOCK],
                                 sizeof(el_entry_t) * EVENT_LOG_BLOCK);
    if (err == ESP_OK) {
        err = nvs_set_u16(s_nvs, KEY_NEXT, s_ring.next);
    }
    if (err == ESP_OK) {
        err = nvs_commit(s_nvs);
    }
    xSemaphoreGive(s_lock);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "write: %s", esp_err_to_name(err));
    }
}

void event_log_boot(int32_t arg, int batt)
{
    event_log_add(EL_BOOT, reset_reason(), arg, 0, batt);
}

void event_log_print(void)
{
    if (!s_ready) {
        printf("events: unavailable (NVS)\n");
        return;
    }
    int n = 0;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    for (unsigned i = 0; i < EVENT_LOG_CAP; i++) {
        const el_entry_t *e = el_ring_get(&s_ring, i);
        if (!e) {
            continue;
        }
        char line[EVENT_LOG_LINE];
        el_format(e, line, sizeof(line));
        printf("%s\n", line);
        n++;
    }
    xSemaphoreGive(s_lock);
    printf("events: %d of %d kept, oldest first, UTC; +Ns = seconds since that boot\n", n,
           EVENT_LOG_CAP);
}

void event_log_clear(void)
{
    if (!s_ready) {
        return;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    nvs_erase_all(s_nvs);
    nvs_commit(s_nvs);
    el_ring_init(&s_ring, 0);
    xSemaphoreGive(s_lock);
    printf("events: cleared\n");
}
