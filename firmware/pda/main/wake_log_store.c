#include "wake_log_store.h"

#include <stdio.h>
#include <string.h>
#include <sys/time.h>

#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"
#include "pda_app.h"
#include "t5_board.h"

static const char *TAG = "wakelog";

#define NS "wakelog"
#define KEY_NEXT "wlnext"
#define CLOCK_SET_S 1577836800 /* 2020-01-01: anything before is an unset clock */
#define LOCK_MS 500

static nvs_handle_t s_nvs;
static bool s_ready;
static uint16_t s_next; /* the slot the next entry goes to */
static SemaphoreHandle_t s_lock;
static StaticSemaphore_t s_lock_buf;

static void slot_key(unsigned slot, char key[8])
{
    snprintf(key, 8, "wl%03u", slot % WAKE_LOG_CAP);
}

void wake_log_init(void)
{
    if (s_ready) {
        return;
    }
    s_lock = xSemaphoreCreateMutexStatic(&s_lock_buf);
    const esp_err_t err = nvs_open(NS, NVS_READWRITE, &s_nvs);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "NVS: %s: no sleep/wake log", esp_err_to_name(err));
        return;
    }
    uint16_t next = 0;
    nvs_get_u16(s_nvs, KEY_NEXT, &next);
    s_next = next % WAKE_LOG_CAP;
    s_ready = true;
}

static wl_reset_t reset_reason(void)
{
    switch (esp_reset_reason()) {
    case ESP_RST_POWERON:
        return WL_RST_POWERON;
    case ESP_RST_EXT:
        return WL_RST_EXT;
    case ESP_RST_SW:
        return WL_RST_SW;
    case ESP_RST_PANIC:
        return WL_RST_PANIC;
    case ESP_RST_INT_WDT:
        return WL_RST_INT_WDT;
    case ESP_RST_TASK_WDT:
        return WL_RST_TASK_WDT;
    case ESP_RST_WDT:
        return WL_RST_WDT;
    case ESP_RST_DEEPSLEEP:
        return WL_RST_DEEPSLEEP;
    case ESP_RST_BROWNOUT:
        return WL_RST_BROWNOUT;
    case ESP_RST_USB:
        return WL_RST_USB;
    case ESP_RST_JTAG:
        return WL_RST_JTAG;
    case ESP_RST_UNKNOWN:
        return WL_RST_UNKNOWN;
    default:
        return WL_RST_OTHER;
    }
}

void wake_log_add(wl_kind_t kind, int why, int32_t arg, bool use_i2c)
{
    if (!s_ready) {
        return;
    }
    const int64_t up = esp_timer_get_time() / 1000000;
    wl_entry_t e = {
        .up_s = (uint16_t)(up > UINT16_MAX ? UINT16_MAX : up),
        .kind = (uint8_t)kind,
        .why = (uint8_t)why,
        .arg = arg,
        .batt = -1,
    };
    struct timeval tv;
    gettimeofday(&tv, NULL);
    int64_t now = tv.tv_sec;
    if (now < CLOCK_SET_S && use_i2c) {
        bool valid = false;
        if (t5_rtc_get(&now, &valid) != ESP_OK || !valid) {
            now = 0;
        }
    }
    e.t = now >= CLOCK_SET_S ? (uint32_t)now : 0;
    if (use_i2c) {
        t5_battery_t b;
        if (t5_battery_read(&b) == ESP_OK && b.percent >= 0 && b.percent <= 100) {
            e.batt = (int8_t)b.percent;
        }
    }

    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(LOCK_MS)) != pdTRUE) {
        ESP_LOGW(TAG, "busy: entry dropped");
        return;
    }
    char key[8];
    slot_key(s_next, key);
    esp_err_t err = nvs_set_blob(s_nvs, key, &e, sizeof(e));
    if (err == ESP_OK) {
        s_next = (uint16_t)((s_next + 1) % WAKE_LOG_CAP);
        err = nvs_set_u16(s_nvs, KEY_NEXT, s_next);
    }
    if (err == ESP_OK) {
        err = nvs_commit(s_nvs);
    }
    xSemaphoreGive(s_lock);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "write: %s", esp_err_to_name(err));
    }
}

void wake_log_boot(int t5_wake)
{
    wake_log_add(WL_BOOT, reset_reason(), t5_wake, true);
}

void wake_log_print(void)
{
    if (!s_ready) {
        printf("wakelog: unavailable\n");
        return;
    }
    const int tz = pda_app_tz_min();
    wl_entry_t prev;
    bool have_prev = false;
    int n = 0;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    const uint16_t first = s_next;
    xSemaphoreGive(s_lock);
    for (unsigned i = 0; i < WAKE_LOG_CAP; i++) {
        char key[8];
        slot_key(first + i, key);
        wl_entry_t e;
        size_t len = sizeof(e);
        if (nvs_get_blob(s_nvs, key, &e, &len) != ESP_OK || len != sizeof(e)) {
            continue; /* never written yet */
        }
        if (have_prev && wl_missed_sleep(&prev, &e)) {
            printf("  ^ that sync wake never went back to sleep: the boot below says why it "
                   "reset\n");
        }
        char line[WAKE_LOG_LINE];
        wl_format(&e, tz, line, sizeof(line));
        printf("%s\n", line);
        prev = e;
        have_prev = true;
        n++;
    }
    printf("wakelog: %d of %d entries, oldest first\n", n, WAKE_LOG_CAP);
}

void wake_log_clear(void)
{
    if (!s_ready) {
        return;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    nvs_erase_all(s_nvs);
    nvs_commit(s_nvs);
    s_next = 0;
    xSemaphoreGive(s_lock);
    printf("wakelog: cleared\n");
}
