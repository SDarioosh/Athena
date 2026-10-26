/* Athena PDA on the LilyGO T5 E-Paper Pro (docs/t5-pda-plan.md).
 *
 * Boot: board (I2C, buttons, RTC), the e-paper (epdiy), the stored feed and outbox from
 * LittleFS, touch, BLE (the glance protocol, advertising as odysseus-pda), the screens and
 * the USB console. The phone (or the PC relay) pushes the feed and drains the outbox over
 * BLE; the T5 never talks to a server and holds no tokens.
 *
 * Deep sleep: a BOOT press wakes into this normal boot; the sync timer (every 15 min) runs
 * only pda_sleep_sync_wake(): no UI, the panel left alone, BLE for a minute, sleep again.
 *
 * Buttons: BOOT short = back / Today, BOOT held 1 s = deep sleep (a BOOT press wakes it),
 * the S3 button next to BOOT (labelled IO48, on the PCA9535) short = front light on/off,
 * held 1 s = sync mode (the 2-minute pairing window). The light's level is on the Status
 * screen. The power button is hardware only. No Wi-Fi: never initialised, not
 * linked. */
#include <stdio.h>
#include <sys/time.h>

#include "ble_pda.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "pda_app.h"
#include "pda_console.h"
#include "pda_epd.h"
#include "pda_sleep.h"
#include "t5_board.h"
#include "touch_gt911.h"
#include "ui_pda.h"
#include "wake_log_store.h"

static const char *TAG = "app";

static esp_timer_handle_t s_tick;

static void on_button(t5_button_t b, t5_press_t p, void *ctx)
{
    (void)ctx;
    ESP_LOGI(TAG, "button %s %s", b == T5_BUTTON_BOOT ? "boot" : "side",
             p == T5_PRESS_SHORT ? "short" : "long");
    const pda_ev_t ev = {.type = PDA_EV_BUTTON, .arg = ((int)b << 1) | (p == T5_PRESS_LONG)};
    pda_app_post(&ev);
}

static void on_touch(const gesture_t *g, void *ctx)
{
    (void)ctx;
    const pda_ev_t ev = {.type = PDA_EV_GESTURE, .gesture = (uint8_t)g->kind, .x = (int16_t)g->x,
                         .y = (int16_t)g->y};
    pda_app_post(&ev);
}

static void ble_state(ble_pda_state_t *st)
{
    t5_battery_t b;
    static t5_battery_t cached = {.percent = -1};
    static int64_t cached_at = -60000;
    const int64_t now = esp_timer_get_time() / 1000;
    if (now - cached_at > 30000 && t5_battery_read(&b) == ESP_OK) {
        cached = b;
        cached_at = now;
    }
    st->feed_version = pda_app_feed_version();
    st->outbox = pda_app_outbox_waiting();
    st->time_valid = pda_app_time_valid();
    st->user_active = pda_app_user_active();
    st->battery_pct = (int8_t)(cached.present ? cached.percent : -1);
    st->charging = cached.charging;
    st->usb = cached.vbus;
}

static void on_passkey(int32_t code)
{
    const pda_ev_t ev = {.type = PDA_EV_PASSKEY, .arg = code};
    pda_app_post(&ev);
}

static void on_link(bool up)
{
    pda_app_note_link(up);
    const pda_ev_t ev = {.type = PDA_EV_LINK, .arg = up};
    pda_app_post(&ev);
}

/* The minute clock: fire at the next minute boundary, then re-arm. */
static void arm_tick(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    const int64_t us = (60 - tv.tv_sec % 60) * 1000000LL - tv.tv_usec + 50000;
    esp_timer_stop(s_tick);
    esp_timer_start_once(s_tick, (uint64_t)(us > 0 ? us : 60000000));
}

static void on_tick(void *arg)
{
    (void)arg;
    if (pda_app_time_valid()) {
        const pda_ev_t ev = {.type = PDA_EV_TICK};
        pda_app_post(&ev);
    }
    arm_tick();
}

static const ble_pda_hooks_t s_hooks = {
    .fw_version = PDA_FW_VERSION,
    .get_state = ble_state,
    .on_passkey = on_passkey,
    .on_link = on_link,
};

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    const t5_wake_t wake = t5_board_wake_reason();
    ESP_ERROR_CHECK(t5_board_init());
    wake_log_init();
    wake_log_boot(wake);
    if (wake == T5_WAKE_TIMER) {
        pda_sleep_sync_wake(&s_hooks); /* doesn't return */
    }
    pda_sleep_check_crash(); /* a crash during a sync wake: back to sleep */
    if (wake == T5_WAKE_BUTTON) {
        ESP_LOGI(TAG, "woke from deep sleep (BOOT)");
    }
    ESP_ERROR_CHECK(pda_epd_init(t5_board_i2c()));
    ESP_ERROR_CHECK(pda_app_init());
    if (touch_init(t5_board_i2c(), on_touch, NULL) != ESP_OK) {
        ESP_LOGE(TAG, "touch unavailable: the console's tap/screen commands still work");
    }
    t5_board_start_buttons(on_button, NULL);

    err = ble_pda_init(pda_app_store(), NULL, &s_hooks);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "BLE: %s", esp_err_to_name(err));
    }
    ESP_ERROR_CHECK(ui_pda_start());

    const esp_timer_create_args_t t = {.callback = on_tick, .name = "clock"};
    esp_timer_create(&t, &s_tick);
    arm_tick();
    pda_console_start();
}
