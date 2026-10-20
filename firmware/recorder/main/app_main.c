/* odysseus-sound recorder (docs/t-watch-ultra-plan.md §8): recording sessions to Opus chunks
 * on the SD card, drained by a phone or PC relay over BLE (docs/twatch-ble-protocol.md).
 *
 * Power: the chip light-sleeps whenever every task is blocked (BLE keeps working). After
 * STANDBY_IDLE_MS with nothing to do (no session, no relay wanted or connected, screen
 * off, card unmounted, no USB power) it goes into standby: deep sleep until PWR is pressed
 * or USB power is plugged in. Waking is a reboot that keeps the mute state and shows the
 * status screen.
 *
 * Boot: board bring-up with every unused rail off, RTC -> system clock, mic ready, chunk
 * queue rebuilt from the card, BLE (advertising only when there is something to sync), USB
 * console.
 * PWR short press: status screen for ~5 s. BOOT short press: toggle mute (1 buzz = unmuted,
 * 2 = muted); muting ends a running session. BOOT long press: open the 2-minute BLE sync
 * window (advertise now, accept a new phone). BOOT held + PWR short press, or `session
 * start`: start a recording session; the same combination, `session stop` or mute ends it.
 * `rec` on the console still records the milestone-1 test WAV.
 * No Wi-Fi: it is never initialised and not linked; only the BT controller uses the radio. */
#include <inttypes.h>
#include <stdio.h>
#include <sys/time.h>

#include "app.h"
#include "ble_sync.h"
#include "board.h"
#include "chunk_store.h"
#include "console_cmds.h"
#include "esp_attr.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_pm.h"
#include "esp_random.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "isotime.h"
#include "mic.h"
#include "nvs_flash.h"
#include "recorder.h"
#include "session_rec.h"
#include "ui.h"

static const char *TAG = "app";

#define SCREEN_ON_MS 5000
#define PASSKEY_ON_MS 30000 /* pairing code: the SMP timeout, pairing fails after it anyway */
#define TEST_RECORDING_SECONDS 60
#define BATTERY_LOG_PERIOD_MS 60000
#define STANDBY_IDLE_MS (2 * 60 * 1000)
#define FW_VERSION "0.2.0"

typedef struct {
    app_event_type_t type;
    int arg;
} app_event_t;

static QueueHandle_t s_events;
static esp_timer_handle_t s_screen_timer;
/* RTC memory: survives standby (deep sleep), cleared by a power-on or a reset. */
static RTC_DATA_ATTR volatile bool s_muted;
static volatile bool s_time_valid;
static esp_err_t s_session_result; /* set by the session's done callback */
static int64_t s_last_activity_ms; /* main task only */

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

static void on_session_done(esp_err_t result, session_rec_end_t why, void *ctx)
{
    (void)ctx;
    s_session_result = result;
    app_post(APP_EV_SESSION_DONE, (int)why);
}

static void on_session_tick(uint32_t elapsed_s, void *ctx)
{
    (void)ctx;
    app_post(APP_EV_SESSION_TICK, (int)elapsed_s);
}

static void on_queue_changed(void *ctx)
{
    (void)ctx;
    ble_sync_kick();
    app_post(APP_EV_QUEUE_CHANGED, 0);
}

/* ---- BLE hooks (docs/twatch-ble-protocol.md §4.2, §5.5) ---- */
static void ble_get_state(ble_sync_app_state_t *out)
{
    out->recording = session_rec_running();
    out->muted = s_muted;
    struct timeval tv;
    gettimeofday(&tv, NULL);
    out->time_valid = s_time_valid || isotime_is_plausible(tv.tv_sec);
}

static void ble_on_time(int64_t epoch_ms)
{
    const struct timeval tv = {.tv_sec = (time_t)(epoch_ms / 1000),
                               .tv_usec = (suseconds_t)(epoch_ms % 1000) * 1000};
    settimeofday(&tv, NULL);
    const esp_err_t err = board_rtc_set(epoch_ms / 1000);
    s_time_valid = true;
    char buf[24];
    isotime_format(epoch_ms / 1000, buf, sizeof(buf));
    ESP_LOGI(TAG, "time from the relay: %s (RTC write %s)", buf, esp_err_to_name(err));
}

static void ble_on_passkey(int32_t passkey)
{
    app_post(APP_EV_PASSKEY, (int)passkey);
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
        .ble_connected = ble_sync_is_connected(),
    };
    session_rec_state_t ss;
    session_rec_get_state(&ss);
    s->session = ss.running;
    s->session_elapsed_s = (int)ss.elapsed_s;
    chunk_store_stats_t q;
    chunk_store_stats(&q);
    s->queued_chunks = (int)q.chunks;
    s->queued_min = (int)((q.audio_ms + 59999) / 60000);
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
    printf("state: %s%s%s\n", s.session ? "recording session" : "idle",
           s.muted ? ", muted" : "", s.recording_file ? " (writing test WAV)" : "");
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
    session_rec_state_t ss;
    session_rec_get_state(&ss);
    if (ss.session_hex[0]) {
        printf("session %s: %s, chunk %" PRIu32 ", %" PRIu32 " s, %" PRIu32 " chunks closed%s; "
               "encoder %" PRIu32 " us avg / %" PRIu32 " us max per 20 ms frame; last: %s\n",
               ss.session_hex, ss.running ? "running" : "ended", ss.sequence, ss.elapsed_s,
               ss.chunks_written, ss.time_unset ? ", TIME UNSET" : "", ss.enc_avg_us,
               ss.enc_max_us, esp_err_to_name(ss.last_err));
    }
    chunk_store_stats_t q;
    chunk_store_stats(&q);
    printf("queue: %" PRIu32 " chunks, %" PRIu32 " KB, %" PRIu32 " s of audio%s%s\n", q.chunks,
           q.bytes / 1024, q.audio_ms / 1000, q.has_last ? ", a finished session" : "",
           chunk_store_ready() ? "" : " (card not read)");
    printf("ble: %s%s\n", ble_sync_is_connected() ? "relay connected" : "not connected",
           ble_sync_window_is_open() ? ", sync window open" : "");
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
    if (ui_is_shown() && !ui_is_pairing()) {
        ui_status_t s;
        gather_status(&s);
        ui_show_status(&s);
    }
}

static void start_test_recording(int seconds)
{
    if (session_rec_running()) {
        ESP_LOGW(TAG, "a recording session is running; stop it first (BOOT+PWR or `session stop`)");
        return;
    }
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

static void start_session(void)
{
    if (s_muted) {
        ESP_LOGW(TAG, "muted: not recording (press BOOT to unmute)");
        board_haptic_pulse(2);
        return;
    }
    recorder_state_t rs;
    recorder_get_state(&rs);
    if (rs.running) {
        ESP_LOGW(TAG, "the test WAV recorder is running; `stop` it first");
        return;
    }
    const esp_err_t err = session_rec_start(on_session_done, on_session_tick, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "session start: %s", esp_err_to_name(err));
        board_haptic_pulse(3);
        return;
    }
    ESP_LOGI(TAG, "recording session started (1 buzz)");
    board_haptic_pulse(1);
    ble_sync_kick();
    refresh_screen_if_shown();
}

static void stop_session(const char *why)
{
    if (session_rec_stop()) {
        ESP_LOGI(TAG, "%s: ending the recording session", why);
    } else {
        ESP_LOGI(TAG, "%s: no session is recording", why);
    }
}

static void toggle_mute(void)
{
    s_muted = !s_muted;
    if (s_muted) {
        /* Muting must never leave the mic recording. */
        recorder_stop();
        session_rec_stop();
    }
    ble_sync_kick();
    ESP_LOGI(TAG, "%s", s_muted ? "MUTED (2 buzzes)" : "RECORDING (1 buzz)");
    refresh_screen_if_shown();
    board_haptic_pulse(s_muted ? 2 : 1);
}

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

/* Why the watch can't go into standby now, or NULL if it can. */
static const char *standby_blocker(bool forced)
{
    recorder_state_t rs;
    recorder_get_state(&rs);
    if (session_rec_running() || rs.running) {
        return "recording";
    }
    if (ble_sync_is_connected()) {
        return "relay connected";
    }
    if (ble_sync_wants_relay()) {
        return "advertising for a relay";
    }
    if (ui_is_shown()) {
        return "screen on";
    }
    if (board_storage_is_mounted()) {
        return "SD card in use";
    }
    if (forced) {
        return NULL;
    }
    board_battery_t b;
    if (board_battery_read(&b) == ESP_OK && b.vbus) {
        return "USB power"; /* charging, and the console stays usable */
    }
    return NULL;
}

static void enter_standby(bool forced)
{
    const char *why = standby_blocker(forced);
    if (why) {
        /* The automatic check runs once a minute while blocked: keep it out of the log. */
        if (forced) {
            ESP_LOGW(TAG, "no standby: %s", why);
        } else {
            ESP_LOGD(TAG, "no standby: %s", why);
        }
        return;
    }
    if (board_standby_prepare() != ESP_OK) {
        ESP_LOGW(TAG, "no standby: board wake sources not armed");
        return;
    }
    ESP_LOGI(TAG, "standby: deep sleep until PWR is pressed or USB power is plugged in");
    /* The BT controller asked for the main crystal through light sleep
     * (BT_CTRL_MAIN_XTAL_PU_DURING_LIGHT_SLEEP); deep sleep would keep it running too. */
    esp_sleep_pd_config(ESP_PD_DOMAIN_XTAL, ESP_PD_OPTION_AUTO);
    esp_deep_sleep_start();
}

static void handle(const app_event_t *ev)
{
    switch (ev->type) {
    case APP_EV_PWR_SHORT:
        if (board_button_is_down(BOARD_BUTTON_BOOT)) {
            /* The same combination starts and ends a session (or stops a test WAV). */
            recorder_state_t rs;
            recorder_get_state(&rs);
            if (rs.running) {
                stop_test_recording("BOOT + PWR");
            } else if (session_rec_running()) {
                stop_session("BOOT + PWR");
            } else {
                ESP_LOGI(TAG, "BOOT + PWR: recording session");
                start_session();
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
        ESP_LOGI(TAG, "sync now: BLE sync window open for 2 min (advertising, new phones may pair)");
        ble_sync_open_window();
        board_haptic_pulse(1);
        break;
    case APP_EV_SESSION_START:
        start_session();
        break;
    case APP_EV_SESSION_STOP:
        stop_session("console");
        break;
    case APP_EV_PASSKEY:
        if (ev->arg >= 0) {
            if (ui_show_passkey((uint32_t)ev->arg) == ESP_OK) {
                esp_timer_stop(s_screen_timer);
                esp_timer_start_once(s_screen_timer, (uint64_t)PASSKEY_ON_MS * 1000);
            }
            board_haptic_pulse(1);
        } else if (ui_is_pairing()) {
            esp_timer_stop(s_screen_timer);
            ui_hide();
        }
        break;
    case APP_EV_SESSION_TICK:
    case APP_EV_QUEUE_CHANGED:
        refresh_screen_if_shown();
        break;
    case APP_EV_SESSION_DONE: {
        session_rec_state_t ss;
        session_rec_get_state(&ss);
        const session_rec_end_t why = (session_rec_end_t)ev->arg;
        ESP_LOGI(TAG, "session %s ended (%s, %s): %" PRIu32 " s in %" PRIu32 " chunks; encoder "
                 "%" PRIu32 " us avg / %" PRIu32 " us max per 20 ms frame; stack %" PRIu32 " B unused",
                 ss.session_hex, why == SESSION_END_REQUEST ? "stopped" :
                 why == SESSION_END_CARD_FULL ? "card full" : "error",
                 esp_err_to_name(s_session_result), ss.elapsed_s, ss.chunks_written,
                 ss.enc_avg_us, ss.enc_max_us, ss.stack_free_min);
        if (why != SESSION_END_REQUEST) {
            board_haptic_pulse(3);
        } else if (!s_muted) { /* stopped by mute: the 2 mute buzzes already said so */
            board_haptic_pulse(1);
        }
        ble_sync_kick();
        refresh_screen_if_shown();
        break;
    }
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
    case APP_EV_STANDBY:
        enter_standby(true);
        break;
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
    const bool from_standby = esp_sleep_get_wakeup_cause() != ESP_SLEEP_WAKEUP_UNDEFINED;
    ESP_LOGI(TAG, "odysseus-sound recorder %s%s", FW_VERSION,
             from_standby ? " (woken from standby)" : "");

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

#if CONFIG_PM_ENABLE
    /* DFS between 80 and 40 MHz, light sleep when idle. Recording can't be cut by it: the
     * I2S channel holds a PM lock while enabled. A connected USB host holds one too
     * (CONFIG_USJ_NO_AUTO_LS_ON_CONNECTION), so the console keeps working. */
    const esp_pm_config_t pm = {.max_freq_mhz = CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ,
                                .min_freq_mhz = 40, .light_sleep_enable = true};
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

    /* The queue survives reboots: rebuild it from the card (recovers a chunk cut by a power
     * loss). Without a card the queue starts empty; a session start retries. */
    const uint32_t boot_id = esp_random();
    err = chunk_store_init(boot_id);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "chunk queue not read (%s); is the SD card in?", esp_err_to_name(err));
    }
    chunk_store_set_listener(on_queue_changed, NULL);
    static const ble_sync_hooks_t hooks = {
        .fw_version = FW_VERSION, .get_state = ble_get_state, .on_time = ble_on_time,
        .on_passkey = ble_on_passkey};
    ESP_ERROR_CHECK(ble_sync_init(&hooks));

    /* After a power-on one buzz: haptics work and the recorder starts unmuted. Out of
     * standby the mute state was kept: buzz it (1 = unmuted, 2 = muted) and show the
     * screen, so the press that woke the watch shows it is awake. */
    board_haptic_pulse(s_muted ? 2 : 1);
    if (from_standby) {
        show_screen();
    }

    ESP_ERROR_CHECK(console_start());
    ESP_LOGI(TAG, "ready: PWR = status screen, BOOT = mute, BOOT long = BLE sync window, "
                  "BOOT+PWR = start/stop a recording session; type `help` on the USB console");

    /* Wake only for events, the battery log and the standby check, so the chip can light
     * sleep in between. */
    int64_t last_batt = now_ms();
    s_last_activity_ms = last_batt;
    for (;;) {
        const int64_t now = now_ms();
        int64_t wait = last_batt + BATTERY_LOG_PERIOD_MS - now;
        const int64_t idle_left = s_last_activity_ms + STANDBY_IDLE_MS - now;
        if (idle_left > 0 && idle_left < wait) {
            wait = idle_left;
        }
        app_event_t ev;
        if (xQueueReceive(s_events, &ev, pdMS_TO_TICKS(wait > 10 ? wait : 10)) == pdTRUE) {
            s_last_activity_ms = now_ms();
            handle(&ev);
            continue;
        }
        if (now_ms() - last_batt >= BATTERY_LOG_PERIOD_MS) {
            last_batt = now_ms();
            log_battery();
        }
        if (now_ms() - s_last_activity_ms >= STANDBY_IDLE_MS) {
            enter_standby(false); /* returns if something keeps the watch awake */
        }
    }
}
