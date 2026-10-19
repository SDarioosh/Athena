/* barvis-sound recorder, milestone 1: bring-up test firmware (docs/t-watch-ultra-plan.md §8).
 *
 * Boot: board bring-up with every unused rail off, RTC -> system clock, mic ready, USB console.
 * PWR short press: status screen for ~5 s. BOOT short press: toggle mute (1 buzz = recording,
 * 2 = muted). BOOT long press: "sync now" placeholder. BOOT held + PWR short press, or the
 * console command `rec`: record 60 s from the mic to a WAV on the SD card; the same
 * combination, `stop`, or muting stops it early.
 * No Wi-Fi, no BLE: neither is initialised, and neither is linked (CMakeLists.txt). */
#include <inttypes.h>
#include <stdio.h>
#include <sys/time.h>

#include "app.h"
#include "board.h"
#include "console_cmds.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_pm.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "isotime.h"
#include "mic.h"
#include "nvs_flash.h"
#include "recorder.h"
#include "ui.h"

static const char *TAG = "app";

#define SCREEN_ON_MS 5000
#define TEST_RECORDING_SECONDS 60
#define BATTERY_LOG_PERIOD_MS 60000

typedef struct {
    app_event_type_t type;
    int arg;
} app_event_t;

static QueueHandle_t s_events;
static esp_timer_handle_t s_screen_timer;
static bool s_muted;
static bool s_time_valid;

void app_post(app_event_type_t type, int arg)
{
    const app_event_t ev = {.type = type, .arg = arg};
    if (s_events && xQueueSend(s_events, &ev, 0) != pdTRUE) {
        ESP_LOGW(TAG, "event queue full, dropped %d", (int)type);
    }
}

bool app_is_muted(void)
{
    return s_muted;
}

static void on_button(board_button_t button, board_press_t press, void *ctx)
{
    (void)ctx;
    if (button == BOARD_BUTTON_PWR) {
        app_post(press == BOARD_PRESS_SHORT ? APP_EV_PWR_SHORT : APP_EV_PWR_LONG, 0);
    } else {
        app_post(press == BOARD_PRESS_SHORT ? APP_EV_BOOT_SHORT : APP_EV_BOOT_LONG, 0);
    }
}

static void on_screen_timer(void *arg)
{
    (void)arg;
    app_post(APP_EV_SCREEN_TIMEOUT, 0);
}

static void on_rec_done(esp_err_t result, void *ctx)
{
    (void)ctx;
    app_post(APP_EV_REC_DONE, (int)result);
}

static void on_rec_tick(int elapsed_s, void *ctx)
{
    (void)ctx;
    app_post(APP_EV_REC_TICK, elapsed_s);
}

static void gather_status(ui_status_t *s)
{
    recorder_state_t rs;
    recorder_get_state(&rs);
    struct timeval tv;
    gettimeofday(&tv, NULL);
    *s = (ui_status_t){
        .muted = s_muted,
        .recording_file = rs.running,
        .rec_elapsed_s = rs.elapsed_s,
        .rec_total_s = rs.total_s,
        .now = tv.tv_sec,
        .time_valid = s_time_valid || isotime_is_plausible(tv.tv_sec),
        .sd_present = board_storage_card_present(),
        .sd_mounted = board_storage_is_mounted(),
    };
    if (board_battery_read(&s->battery) != ESP_OK) {
        s->battery = (board_battery_t){.present = false, .percent = -1};
    }
    if (s->sd_mounted) {
        uint64_t total = 0, freeb = 0;
        if (board_storage_info(&total, &freeb) == ESP_OK) {
            s->sd_free_mb = freeb >> 20;
        }
    }
}

void app_print_status(void)
{
    ui_status_t s;
    gather_status(&s);
    char buf[24];
    isotime_format(s.now, buf, sizeof(buf));
    printf("state: %s%s\n", s.muted ? "muted" : "recording",
           s.recording_file ? " (writing test WAV)" : "");
    printf("battery: %d%%, %d mV, %s%s\n", s.battery.percent, s.battery.millivolts,
           s.battery.charging ? "charging" : "not charging", s.battery.vbus ? ", USB in" : "");
    printf("time: %s (%s)\n", buf, s.time_valid ? "set" : "NOT SET");
    printf("sd: %s%s\n", s.sd_present ? "card in" : "no card", s.sd_mounted ? ", mounted" : "");
    recorder_state_t rs;
    recorder_get_state(&rs);
    if (rs.running) {
        printf("recording: %d/%d s -> %s\n", rs.elapsed_s, rs.total_s, rs.path);
    } else if (rs.path[0]) {
        printf("last recording: %s (%s)\n", rs.path, esp_err_to_name(rs.last_err));
    }
    printf("screen: %s\n", ui_is_shown() ? "on" : "off");
}

static void show_screen(void)
{
    ui_status_t s;
    gather_status(&s);
    const esp_err_t err = ui_show_status(&s);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "status screen failed: %s", esp_err_to_name(err));
        ui_hide();
        return;
    }
    esp_timer_stop(s_screen_timer);
    esp_timer_start_once(s_screen_timer, (uint64_t)SCREEN_ON_MS * 1000);
}

static void refresh_screen_if_shown(void)
{
    if (ui_is_shown()) {
        ui_status_t s;
        gather_status(&s);
        ui_show_status(&s);
    }
}

static void start_test_recording(int seconds)
{
    if (s_muted) {
        ESP_LOGW(TAG, "muted: not recording (press BOOT to unmute)");
        board_haptic_pulse(2);
        return;
    }
    const esp_err_t err = recorder_start(seconds, on_rec_done, on_rec_tick, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "recorder start: %s", esp_err_to_name(err));
    }
    refresh_screen_if_shown();
}

static void stop_test_recording(const char *why)
{
    if (recorder_stop()) {
        ESP_LOGI(TAG, "%s: stopping the test recording", why);
    } else {
        ESP_LOGI(TAG, "%s: nothing is recording", why);
    }
}

static void toggle_mute(void)
{
    s_muted = !s_muted;
    if (s_muted) {
        /* Muting must never leave the mic recording. */
        recorder_stop();
    }
    ESP_LOGI(TAG, "%s", s_muted ? "MUTED (2 buzzes)" : "RECORDING (1 buzz)");
    refresh_screen_if_shown();
    board_haptic_pulse(s_muted ? 2 : 1);
}

static void handle(const app_event_t *ev)
{
    switch (ev->type) {
    case APP_EV_PWR_SHORT:
        if (board_button_is_down(BOARD_BUTTON_BOOT)) {
            /* Same combination starts and stops the test recording. */
            recorder_state_t rs;
            recorder_get_state(&rs);
            if (rs.running) {
                stop_test_recording("BOOT + PWR");
            } else {
                ESP_LOGI(TAG, "BOOT + PWR: test recording");
                start_test_recording(TEST_RECORDING_SECONDS);
            }
        } else {
            show_screen();
        }
        break;
    case APP_EV_PWR_LONG:
        ESP_LOGI(TAG, "PWR long press (hardware power-off follows at ~6 s)");
        break;
    case APP_EV_BOOT_SHORT:
    case APP_EV_MUTE_TOGGLE:
        toggle_mute();
        break;
    case APP_EV_BOOT_LONG:
    case APP_EV_SYNC_NOW:
        ESP_LOGI(TAG, "sync now: placeholder, BLE transfer comes in a later milestone");
        break;
    case APP_EV_SCREEN_SHOW:
        show_screen();
        break;
    case APP_EV_SCREEN_TIMEOUT:
        ui_hide();
        break;
    case APP_EV_RECORD:
        start_test_recording(ev->arg);
        break;
    case APP_EV_REC_STOP:
        stop_test_recording("console");
        break;
    case APP_EV_REC_TICK:
        refresh_screen_if_shown();
        break;
    case APP_EV_REC_DONE: {
        recorder_state_t rs;
        recorder_get_state(&rs);
        if (ev->arg == ESP_OK) {
            ESP_LOGI(TAG, "recording finished: %s", rs.path);
            if (!s_muted) { /* stopped by mute: the 2 mute buzzes already said so */
                board_haptic_pulse(1);
            }
        } else {
            ESP_LOGE(TAG, "recording failed: %s", esp_err_to_name((esp_err_t)ev->arg));
            board_haptic_pulse(3);
        }
        refresh_screen_if_shown();
        break;
    }
    }
}

static void clock_from_rtc(void)
{
    int64_t epoch = 0;
    bool valid = false;
    if (board_rtc_get(&epoch, &valid) == ESP_OK && valid) {
        struct timeval tv = {.tv_sec = (time_t)epoch, .tv_usec = 0};
        settimeofday(&tv, NULL);
        s_time_valid = true;
        char buf[24];
        isotime_format(epoch, buf, sizeof(buf));
        ESP_LOGI(TAG, "system clock set from RTC: %s", buf);
    } else {
        ESP_LOGW(TAG, "RTC time not valid; set it with `time 2026-10-19T12:34:56Z` on the console");
    }
}

static void log_battery(void)
{
    board_battery_t b;
    if (board_battery_read(&b) == ESP_OK) {
        ESP_LOGI(TAG, "battery %d%% %d mV %s%s", b.percent, b.millivolts,
                 b.charging ? "charging" : "discharging", b.vbus ? " (USB)" : "");
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "barvis-sound recorder, milestone 1 bring-up");

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

#if CONFIG_PM_ENABLE
    /* DFS between 80 and 40 MHz. Light sleep stays off: it would stop the mic DMA and the USB
     * console; whether it can coexist with I2S is the milestone-2 question. */
    const esp_pm_config_t pm = {.max_freq_mhz = CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ,
                                .min_freq_mhz = 40, .light_sleep_enable = false};
    ESP_ERROR_CHECK(esp_pm_configure(&pm));
#endif

    s_events = xQueueCreate(16, sizeof(app_event_t));
    const esp_timer_create_args_t targs = {.callback = on_screen_timer, .name = "screen"};
    ESP_ERROR_CHECK(esp_timer_create(&targs, &s_screen_timer));

    ESP_ERROR_CHECK(board_init());
    ESP_LOGI(TAG, "board %s; PSRAM %u KB free, internal %u KB free", board_name(),
             (unsigned)(heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024),
             (unsigned)(heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024));

    clock_from_rtc();
    ESP_ERROR_CHECK(mic_init(board_mic_pins()));
    board_buttons_set_callback(on_button, NULL);
    log_battery();

    /* One buzz: haptics work and the recorder starts unmuted. */
    board_haptic_pulse(1);

    ESP_ERROR_CHECK(console_start());
    ESP_LOGI(TAG, "ready: PWR = status screen, BOOT = mute, BOOT+PWR = start/stop 60 s test recording, "
                  "type `help` on the USB console");

    TickType_t last_batt = xTaskGetTickCount();
    for (;;) {
        app_event_t ev;
        if (xQueueReceive(s_events, &ev, pdMS_TO_TICKS(1000)) == pdTRUE) {
            handle(&ev);
        }
        if (xTaskGetTickCount() - last_batt >= pdMS_TO_TICKS(BATTERY_LOG_PERIOD_MS)) {
            last_batt = xTaskGetTickCount();
            log_battery();
        }
    }
}
