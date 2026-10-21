/* odysseus-sound recorder (docs/t-watch-ultra-plan.md §8): recording sessions to Opus chunks
 * on the SD card, drained by a phone or PC relay over BLE (docs/twatch-ble-protocol.md).
 *
 * Power: the chip light-sleeps whenever every task is blocked (BLE keeps working, every
 * button works on its first press). It never goes into standby by itself: only the console
 * `standby` puts it into deep sleep until PWR is pressed or USB power is plugged in. Waking
 * is a reboot that keeps the mute state and shows the status screen; the press that woke
 * it does nothing else (it doesn't start a recording). Holding PWR 6 s powers the watch off
 * (the AXP2101 does it; a press powers it on).
 *
 * Boot: board bring-up with every unused rail off, RTC -> system clock, mic ready, chunk
 * queue rebuilt from the card, BLE (advertising only when there is something to sync), USB
 * console.
 * Buttons (every action lights the screen for ~3 s with what happened):
 *   BOOT press: status screen for ~5 s (after ~0.3 s, to rule out a double press).
 *   BOOT double press: mute / unmute (2 buzzes = muted, 1 long buzz = unmuted); muting ends
 *     whatever is recording.
 *   BOOT held ~1 s: open the 2-minute BLE sync window (advertise now, accept a new phone).
 *   PWR press: start / stop a conversation recording session (1 buzz start, 2 buzzes stop);
 *     while a voice note records, PWR (a press or a hold) stops the note instead.
 *   PWR held ~1 s (AXP2101 PKEY_LONG): start a voice note, its own session of kind NOTE,
 *     max 5 min. During a conversation the conversation session ends first and a new one
 *     starts after the note. While muted, recording is refused (2 buzzes).
 * Console: `session start|stop`, `note start|stop`; `rec` still records the milestone-1
 * test WAV.
 * No Wi-Fi: it is never initialised and not linked; only the BT controller uses the radio. */
#include <inttypes.h>
#include <stdio.h>
#include <sys/time.h>

#include "app.h"
#include "battery_guard.h"
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
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "isotime.h"
#include "mic.h"
#include "nvs_flash.h"
#include "power_log.h"
#include "recorder.h"
#include "reset_reason.h"
#include "session_rec.h"
#include "ui.h"

static const char *TAG = "app";

#define SCREEN_ON_MS 5000  /* status screen */
#define MESSAGE_ON_MS 3000 /* confirmation after a button action */
#define PASSKEY_ON_MS 30000 /* pairing code: the SMP timeout, pairing fails after it anyway */
#define TEST_RECORDING_SECONDS 60
#define BATTERY_LOG_PERIOD_MS 60000
#define BATTERY_CHECK_PERIOD_MS 15000 /* low-battery guard; it needs 2 low readings */
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
/* Event loop only. */
typedef enum { SCREEN_OFF, SCREEN_STATUS, SCREEN_MESSAGE, SCREEN_NOTE, SCREEN_PAIRING } screen_t;
static screen_t s_screen;           /* what is on the panel while ui_is_shown() */
static bool s_note_pending;         /* a note waits for the conversation session to end */
static bool s_resume_conversation;  /* start a conversation session again after the note */
/* Survives a crash or watchdog restart (not a power cut): a conversation was recording, so
 * the app starts a new session after the boot. Refreshed every second of recording. */
#define REC_RESUME_MAGIC 0x52454321u
RTC_NOINIT_ATTR static uint32_t s_rec_resume;
static battery_guard_t s_batt_guard;
static volatile bool s_batt_low;    /* too low to record: sessions refused (battery_guard.c) */
/* Console `batt sim`: a made-up battery (USB unplugged) for testing the low-battery path. */
static portMUX_TYPE s_sim_lock = portMUX_INITIALIZER_UNLOCKED;
static bool s_sim_on;
static int s_sim_percent, s_sim_mv;

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

bool app_battery_is_low(void)
{
    return s_batt_low;
}

void app_battery_simulate(bool on, int percent, int millivolts)
{
    taskENTER_CRITICAL(&s_sim_lock);
    s_sim_on = on;
    s_sim_percent = percent;
    s_sim_mv = millivolts;
    taskEXIT_CRITICAL(&s_sim_lock);
}

esp_err_t app_battery_read(board_battery_t *out, bool *simulated)
{
    taskENTER_CRITICAL(&s_sim_lock);
    const bool on = s_sim_on;
    const int percent = s_sim_percent, mv = s_sim_mv;
    taskEXIT_CRITICAL(&s_sim_lock);
    if (simulated) {
        *simulated = on;
    }
    if (on) {
        *out = (board_battery_t){.present = true, .millivolts = mv, .percent = percent};
        return ESP_OK;
    }
    return board_battery_read(out);
}

static void on_button(board_button_t button, board_press_t press, void *ctx)
{
    (void)ctx;
    if (button == BOARD_BUTTON_PWR) {
        if (press == BOARD_PRESS_SHORT) {
            app_post(APP_EV_PWR_SHORT, 0);
        } else if (press == BOARD_PRESS_LONG) {
            app_post(APP_EV_PWR_LONG, 0);
        }
    } else if (press == BOARD_PRESS_SHORT) {
        app_post(APP_EV_BOOT_SHORT, 0);
    } else if (press == BOARD_PRESS_DOUBLE) {
        app_post(APP_EV_BOOT_DOUBLE, 0);
    } else {
        app_post(APP_EV_BOOT_LONG, 0);
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
    session_rec_state_t ss;
    /* The ended session's kind: called right after it ended, before the event loop (the only
     * place sessions start) has seen this event. */
    session_rec_get_state(&ss);
    app_post(APP_EV_SESSION_DONE, (int)why | ((int)ss.kind << 8));
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
    s->note = ss.running && ss.kind == SESSION_KIND_NOTE;
    s->session_elapsed_s = (int)ss.elapsed_s;
    chunk_store_stats_t q;
    chunk_store_stats(&q);
    s->queued_chunks = (int)q.chunks;
    s->queued_min = (int)((q.audio_ms + 59999) / 60000);
    s->battery_low = s_batt_low;
    if (app_battery_read(&s->battery, NULL) != ESP_OK) {
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
    printf("state: %s%s%s\n",
           s.note ? "recording a voice note" : s.session ? "recording session" : "idle",
           s.muted ? ", muted" : "", s.recording_file ? " (writing test WAV)" : "");
    bool sim = false;
    board_battery_t b;
    app_battery_read(&b, &sim);
    printf("battery: %d%%, %d mV, %s%s%s%s\n", s.battery.percent, s.battery.millivolts,
           s.battery.charging ? "charging" : "not charging", s.battery.vbus ? ", USB in" : "",
           sim ? " (SIMULATED)" : "", s_batt_low ? ", LOW: recording refused" : "");
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
        printf("%s %s: %s, chunk %" PRIu32 ", %" PRIu32 " s, %" PRIu32 " chunks closed%s; "
               "encoder %" PRIu32 " us avg / %" PRIu32 " us max per 20 ms frame; "
               "card write errors recovered %" PRIu32 "; last: %s\n",
               ss.kind == SESSION_KIND_NOTE ? "note" : "session", ss.session_hex,
               ss.running ? "running" : "ended", ss.sequence, ss.elapsed_s,
               ss.chunks_written, ss.time_unset ? ", TIME UNSET" : "", ss.enc_avg_us,
               ss.enc_max_us, ss.write_retries, esp_err_to_name(ss.last_err));
    }
    chunk_store_stats_t q;
    chunk_store_stats(&q);
    printf("queue: %" PRIu32 " chunks, %" PRIu32 " KB, %" PRIu32 " s of audio%s%s\n", q.chunks,
           q.bytes / 1024, q.audio_ms / 1000, q.has_last ? ", a finished session" : "",
           chunk_store_ready() ? "" : " (card not read)");
    printf("ble: %s%s\n", ble_sync_is_connected() ? "relay connected" : "not connected",
           ble_sync_window_is_open() ? ", sync window open" : "");
    if (s_note_pending || s_resume_conversation) {
        printf("pending:%s%s\n", s_note_pending ? " note after the session ends" : "",
               s_resume_conversation ? " conversation resumes after the note" : "");
    }
    printf("screen: %s\n", ui_is_shown() ? "on" : "off");
    printf("reset: %s, up %lld s\n", reset_reason_name(), (long long)(esp_timer_get_time() / 1000000));
    const uint32_t caps = MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT;
    printf("heap: internal %u KB free (lowest %u KB, largest block %u KB), PSRAM %u KB free\n",
           (unsigned)(heap_caps_get_free_size(caps) / 1024),
           (unsigned)(heap_caps_get_minimum_free_size(caps) / 1024),
           (unsigned)(heap_caps_get_largest_free_block(caps) / 1024),
           (unsigned)(heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024));
}

/* ---- screen: status (BOOT press), a short message after every action, the live note ---- */

static void screen_timer_restart(int ms)
{
    esp_timer_stop(s_screen_timer);
    esp_timer_start_once(s_screen_timer, (uint64_t)ms * 1000);
}

static void show_screen(void)
{
    ui_status_t s;
    gather_status(&s);
    const esp_err_t err = ui_show_status(&s);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "status screen failed: %s", esp_err_to_name(err));
        ui_hide();
        s_screen = SCREEN_OFF;
        return;
    }
    s_screen = SCREEN_STATUS;
    screen_timer_restart(SCREEN_ON_MS);
}

/* Light the screen for MESSAGE_ON_MS with what just happened. */
static void show_message(const char *big, const char *small)
{
    const esp_err_t err = ui_show_message(big, small);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "message screen failed: %s", esp_err_to_name(err));
        ui_hide();
        s_screen = SCREEN_OFF;
        return;
    }
    s_screen = SCREEN_MESSAGE;
    screen_timer_restart(MESSAGE_ON_MS);
}

/* "NOTE m:ss" while a note records. restart_timer: light the screen anew (the note just
 * started); otherwise only redraw (a tick while the screen is still on). */
static void show_note(uint32_t elapsed_s, bool restart_timer)
{
    char big[32];
    snprintf(big, sizeof(big), "NOTE %" PRIu32 ":%02" PRIu32, elapsed_s / 60, elapsed_s % 60);
    if (ui_show_message(big, "PWR: STOP") != ESP_OK) {
        ui_hide();
        s_screen = SCREEN_OFF;
        return;
    }
    s_screen = SCREEN_NOTE;
    if (restart_timer) {
        screen_timer_restart(MESSAGE_ON_MS);
    }
}

static void refresh_screen_if_shown(void)
{
    if (ui_is_shown() && s_screen == SCREEN_STATUS) {
        ui_status_t s;
        gather_status(&s);
        ui_show_status(&s);
    }
}

static bool note_running(void)
{
    session_rec_state_t ss;
    session_rec_get_state(&ss);
    return ss.running && ss.kind == SESSION_KIND_NOTE;
}

static bool test_wav_running(void)
{
    recorder_state_t rs;
    recorder_get_state(&rs);
    return rs.running;
}

/* A recording was asked for while muted: 2 buzzes and say so. */
static void refuse_muted(void)
{
    ESP_LOGW(TAG, "muted: not recording (double-press BOOT to unmute)");
    board_haptic_pulse(2);
    show_message("MUTED", "2X BOOT: UNMUTE");
}

/* A recording was asked for with the battery too low: 3 buzzes and say so. */
static void refuse_low(void)
{
    ESP_LOGW(TAG, "battery low: not recording (charge it)");
    board_haptic_pulse(3);
    show_message("BATTERY LOW", "CHARGE ME");
}

/* ---- the test WAV recorder (console `rec`) ---- */

static void start_test_recording(int seconds)
{
    if (session_rec_running()) {
        ESP_LOGW(TAG, "a recording session is running; stop it first (PWR or `session stop`)");
        return;
    }
    if (s_muted) {
        refuse_muted();
        return;
    }
    if (s_batt_low) {
        refuse_low();
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

/* ---- conversation sessions and voice notes ----
 * The mic has one owner: a conversation session and a note never run at once. A note asked
 * for during a conversation ends the conversation session first (s_note_pending) and starts
 * when its SESSION_DONE arrives; when the note ends, a new conversation session starts
 * (s_resume_conversation). Mute cancels both. */

/* quiet: resuming after a note (no buzz; the note's 2 buzzes just played). */
static void start_conversation(bool quiet)
{
    if (s_muted || s_batt_low) {
        if (!quiet) {
            s_muted ? refuse_muted() : refuse_low();
        }
        return;
    }
    if (session_rec_running()) {
        ESP_LOGW(TAG, "a session is already recording");
        return;
    }
    if (test_wav_running()) {
        ESP_LOGW(TAG, "the test WAV recorder is running; `stop` it first");
        show_message("WAV RUNNING", "STOP IT FIRST");
        return;
    }
    const esp_err_t err =
        session_rec_start(SESSION_KIND_CONVERSATION, on_session_done, on_session_tick, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "session start: %s", esp_err_to_name(err));
        board_haptic_pulse(3);
        show_message("REC FAILED", "SEE CONSOLE LOG");
        return;
    }
    ESP_LOGI(TAG, "recording session started%s", quiet ? " again (no buzz)" : " (1 buzz)");
    if (!quiet) {
        board_haptic_pulse(1);
    }
    show_message("RECORDING", quiet ? "RESUMED" : NULL);
    ble_sync_kick();
}

/* Start the note session now (the mic is free). */
static void begin_note(void)
{
    const esp_err_t err =
        session_rec_start(SESSION_KIND_NOTE, on_session_done, on_session_tick, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "note start: %s", esp_err_to_name(err));
        board_haptic_pulse(3);
        show_message("NOTE FAILED", "SEE CONSOLE LOG");
        if (s_resume_conversation) {
            s_resume_conversation = false;
            start_conversation(true);
        }
        return;
    }
    ESP_LOGI(TAG, "voice note started (1 buzz; max %d s, PWR press or hold stops it)", SESSION_REC_NOTE_MAX_S);
    board_haptic_pulse(1);
    show_note(0, true);
    ble_sync_kick();
}

static void start_note(void)
{
    if (s_muted) {
        refuse_muted();
        return;
    }
    if (s_batt_low) {
        refuse_low();
        return;
    }
    if (note_running() || s_note_pending) {
        ESP_LOGI(TAG, "a note is already recording");
        return;
    }
    if (test_wav_running()) {
        ESP_LOGW(TAG, "the test WAV recorder is running; `stop` it first");
        show_message("WAV RUNNING", "STOP IT FIRST");
        return;
    }
    if (session_rec_running()) {
        /* A conversation is recording: end it cleanly, the note follows on SESSION_DONE, and
         * the conversation resumes as a new session after the note. */
        ESP_LOGI(TAG, "note: ending the conversation session first; it resumes after the note");
        s_note_pending = true;
        s_resume_conversation = true;
        session_rec_stop();
        show_message("NOTE", "STARTING");
        return;
    }
    begin_note();
}

static void stop_note(const char *why)
{
    if (s_note_pending) {
        /* The note hadn't started yet: drop it; the conversation resumes at SESSION_DONE. */
        ESP_LOGI(TAG, "%s: note cancelled before it started", why);
        s_note_pending = false;
        board_haptic_pulse(2);
        show_message("NOTE", "CANCELLED");
        return;
    }
    if (note_running()) {
        ESP_LOGI(TAG, "%s: ending the voice note", why);
        session_rec_stop(); /* 2 buzzes and "Note saved" when it has ended */
    } else {
        ESP_LOGI(TAG, "%s: no note is recording", why);
    }
}

static void stop_conversation(const char *why)
{
    s_resume_conversation = false;
    if (session_rec_stop()) {
        ESP_LOGI(TAG, "%s: ending the recording session (2 buzzes)", why);
        board_haptic_pulse(2);
        show_message("STOPPED", NULL);
    } else {
        ESP_LOGI(TAG, "%s: no session is recording", why);
    }
}

/* PWR short press: stop a note (or a test WAV), else start/stop a conversation session. */
static void pwr_short(void)
{
    if (test_wav_running()) {
        stop_test_recording("PWR");
        show_message("WAV STOPPED", NULL);
    } else if (s_note_pending || note_running()) {
        stop_note("PWR");
    } else if (session_rec_running()) {
        stop_conversation("PWR");
    } else {
        start_conversation(false);
    }
}

/* PWR hold ~1 s: start a voice note, or end the one recording (owner, 2026-10-20: either
 * PWR gesture ends a note). */
static void pwr_long(void)
{
    if (s_note_pending || note_running()) {
        stop_note("PWR hold");
    } else {
        start_note();
    }
}

static void toggle_mute(void)
{
    s_muted = !s_muted;
    if (s_muted) {
        /* Muting must never leave the mic recording, and nothing resumes afterwards. */
        s_note_pending = false;
        s_resume_conversation = false;
        const bool was_recording = test_wav_running() || session_rec_running();
        recorder_stop();
        session_rec_stop();
        show_message("MUTED", was_recording ? "RECORDING STOPPED" : NULL);
    } else {
        show_message("UNMUTED", NULL);
    }
    ble_sync_kick();
    ESP_LOGI(TAG, "%s", s_muted ? "MUTED (2 buzzes)" : "UNMUTED (1 long buzz)");
    if (s_muted) {
        board_haptic_pulse(2);
    } else {
        board_haptic_long();
    }
}

static void session_done(session_rec_kind_t kind, session_rec_end_t why)
{
    s_rec_resume = 0;
    session_rec_state_t ss;
    session_rec_get_state(&ss);
    static const char *const WHY[] = {"stopped", "card full", "error", "5 min limit"};
    const bool note = kind == SESSION_KIND_NOTE;
    ESP_LOGI(TAG, "%s %s ended (%s, %s): %" PRIu32 " s in %" PRIu32 " chunks; encoder "
             "%" PRIu32 " us avg / %" PRIu32 " us max per 20 ms frame; stack %" PRIu32 " B unused",
             note ? "note" : "session", ss.session_hex, (unsigned)why < 4 ? WHY[why] : "?",
             esp_err_to_name(s_session_result), ss.elapsed_s, ss.chunks_written,
             ss.enc_avg_us, ss.enc_max_us, ss.stack_free_min);
    ble_sync_kick();

    if (why == SESSION_END_CARD_FULL || why == SESSION_END_ERROR) {
        s_note_pending = false;
        s_resume_conversation = false;
        board_haptic_pulse(3);
        show_message(why == SESSION_END_CARD_FULL ? "CARD FULL" : "REC ERROR",
                     note ? "NOTE ENDED" : "SESSION ENDED");
        return;
    }
    if (note) {
        if (!s_muted && !s_batt_low) { /* mute or low battery already said so */
            char small[40];
            snprintf(small, sizeof(small), "%" PRIu32 ":%02" PRIu32 "%s", ss.elapsed_s / 60,
                     ss.elapsed_s % 60, why == SESSION_END_MAX ? " - 5 MIN MAX" : "");
            board_haptic_pulse(2);
            show_message("NOTE SAVED", small);
        }
        if (s_resume_conversation) {
            s_resume_conversation = false;
            start_conversation(true);
        }
        return;
    }
    /* A conversation session ended. */
    if (s_note_pending) {
        s_note_pending = false;
        begin_note();
    } else if (s_resume_conversation) { /* the pending note was cancelled */
        s_resume_conversation = false;
        start_conversation(true);
    } else {
        refresh_screen_if_shown();
    }
}

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

/* Why the watch can't go into standby now, or NULL if it can. */
static const char *standby_blocker(void)
{
    if (session_rec_running() || test_wav_running() || s_note_pending || s_resume_conversation) {
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
    return NULL;
}

/* Only on request (console `standby`): the watch never goes into standby by itself, so every
 * button works at once (light sleep). */
static void enter_standby(void)
{
    const char *why = standby_blocker();
    if (why) {
        ESP_LOGW(TAG, "no standby: %s", why);
        return;
    }
    /* The power log's RAM ring doesn't survive deep sleep: put it on the card first. */
    const esp_err_t ferr = power_log_flush();
    if (ferr != ESP_OK) {
        ESP_LOGW(TAG, "power log not flushed before standby: %s", esp_err_to_name(ferr));
    }
    if (board_standby_prepare() != ESP_OK) {
        ESP_LOGW(TAG, "no standby: board wake sources not armed");
        return;
    }
    ESP_LOGI(TAG, "standby: deep sleep until PWR is pressed or USB power is plugged in");
    /* The BT controller asked for the main crystal through light sleep
     * (BT_CTRL_MAIN_XTAL_PU_DURING_LIGHT_SLEEP); deep sleep would keep it running too. */
    esp_sleep_pd_config(ESP_PD_DOMAIN_XTAL, ESP_PD_OPTION_AUTO);
    /* Automatic light sleep arms a timer wake before every light sleep and leaves it armed.
     * Deep sleep would inherit it and wake after the last light sleep's length (0 s to a few
     * minutes), boot and light the screen, every time. Only the board's sources may wake us. */
    esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_TIMER);
    esp_deep_sleep_start();
}

static void handle(const app_event_t *ev)
{
    switch (ev->type) {
    case APP_EV_PWR_SHORT:
        pwr_short();
        break;
    case APP_EV_PWR_LONG:
        pwr_long();
        break;
    case APP_EV_NOTE_START:
        start_note();
        break;
    case APP_EV_NOTE_STOP:
        stop_note("console");
        break;
    case APP_EV_BOOT_SHORT:
    case APP_EV_SCREEN_SHOW:
        show_screen();
        break;
    case APP_EV_BOOT_DOUBLE:
    case APP_EV_MUTE_TOGGLE:
        toggle_mute();
        break;
    case APP_EV_BOOT_LONG:
    case APP_EV_SYNC_NOW:
        ESP_LOGI(TAG, "sync now: BLE sync window open for 2 min (advertising, new phones may pair)");
        ble_sync_open_window();
        board_haptic_pulse(1);
        show_message("SYNC OPEN", "2 MIN");
        break;
    case APP_EV_SESSION_START:
        start_conversation(false);
        break;
    case APP_EV_SESSION_STOP:
        s_note_pending = false;
        s_resume_conversation = false;
        if (note_running()) {
            stop_note("console");
        } else {
            stop_conversation("console");
        }
        break;
    case APP_EV_PASSKEY:
        if (ev->arg >= 0) {
            if (ui_show_passkey((uint32_t)ev->arg) == ESP_OK) {
                s_screen = SCREEN_PAIRING;
                screen_timer_restart(PASSKEY_ON_MS);
            }
            board_haptic_pulse(1);
        } else if (ui_is_pairing()) {
            esp_timer_stop(s_screen_timer);
            ui_hide();
            s_screen = SCREEN_OFF;
        }
        break;
    case APP_EV_SESSION_TICK: {
        session_rec_state_t ss;
        session_rec_get_state(&ss);
        s_rec_resume = ss.running && ss.kind == SESSION_KIND_CONVERSATION ? REC_RESUME_MAGIC : 0;
    }
        if (ui_is_shown() && s_screen == SCREEN_NOTE && note_running()) {
            show_note((uint32_t)ev->arg, false);
        } else {
            refresh_screen_if_shown();
        }
        break;
    case APP_EV_QUEUE_CHANGED:
        refresh_screen_if_shown();
        break;
    case APP_EV_SESSION_DONE:
        session_done((session_rec_kind_t)(ev->arg >> 8), (session_rec_end_t)(ev->arg & 0xff));
        break;
    case APP_EV_SCREEN_TIMEOUT:
        ui_hide();
        s_screen = SCREEN_OFF;
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
        enter_standby();
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
    bool sim = false;
    if (app_battery_read(&b, &sim) == ESP_OK) {
        ESP_LOGI(TAG, "battery %d%% %d mV %s%s%s%s", b.percent, b.millivolts,
                 b.charging ? "charging" : "discharging", b.vbus ? " (USB)" : "",
                 sim ? " SIMULATED" : "", s_batt_low ? " LOW" : "");
    }
}

/* One power-log sample: the battery (or `batt sim`) and what the watch is doing. */
static void power_sample(ps_sample_t *out)
{
    board_battery_t b;
    if (app_battery_read(&b, NULL) != ESP_OK) {
        b = (board_battery_t){.percent = -1};
    }
    struct timeval tv;
    gettimeofday(&tv, NULL);
    session_rec_state_t ss;
    session_rec_get_state(&ss);
    uint8_t f = 0;
    f |= b.charging ? PS_CHARGING : 0;
    f |= b.vbus ? PS_USB : 0;
    f |= ss.running && ss.kind == SESSION_KIND_CONVERSATION ? PS_RECORDING : 0;
    f |= ss.running && ss.kind == SESSION_KIND_NOTE ? PS_NOTE : 0;
    f |= ble_sync_is_advertising() ? PS_BLE_ADV : 0;
    f |= ble_sync_is_connected() ? PS_BLE_CONN : 0;
    f |= ui_is_shown() ? PS_SCREEN : 0;
    f |= board_rail_is_on(BOARD_RAIL_SD) ? PS_SD : 0;
    *out = (ps_sample_t){
        .epoch = isotime_is_plausible(tv.tv_sec) ? tv.tv_sec : 0,
        .uptime_s = (uint32_t)(esp_timer_get_time() / 1000000),
        .percent = (int16_t)b.percent,
        .mv = (uint16_t)(b.millivolts > 0 ? b.millivolts : 0),
        .flags = f,
    };
}

/* The power log leaves the card alone while it records or a relay syncs. */
static bool power_card_busy(void)
{
    return session_rec_running() || test_wav_running() || ble_sync_is_connected();
}

/* Low-battery guard: at the board's limit, end whatever records (its last chunk closes with
 * LAST as on a normal stop), buzz 3 times, and refuse new recordings until it recovers. */
static void check_battery(void)
{
    board_battery_t b;
    if (app_battery_read(&b, NULL) != ESP_OK) {
        return;
    }
    const bg_reading_t r = {.present = b.present, .millivolts = b.millivolts,
                            .percent = b.percent, .vbus = b.vbus};
    switch (battery_guard_update(&s_batt_guard, &r)) {
    case BG_ENTER_LOW: {
        s_batt_low = true;
        s_note_pending = false;
        s_resume_conversation = false;
        const bool was_recording = test_wav_running() || session_rec_running();
        recorder_stop();
        session_rec_stop();
        ESP_LOGW(TAG, "battery low (%d%%, %d mV): %s; new recordings refused until %d%% or USB",
                 b.percent, b.millivolts, was_recording ? "recording stopped" : "not recording",
                 s_batt_guard.lim.resume_percent);
        board_haptic_pulse(3);
        show_message("BATTERY LOW", was_recording ? "RECORDING STOPPED" : "CHARGE ME");
        ble_sync_kick();
        break;
    }
    case BG_LEAVE_LOW:
        s_batt_low = false;
        ESP_LOGI(TAG, "battery OK again (%d%%, %d mV%s): recording allowed", b.percent,
                 b.millivolts, b.vbus ? ", USB" : "");
        refresh_screen_if_shown();
        break;
    case BG_NONE:
        break;
    }
}

void app_main(void)
{
    const bool from_standby = esp_sleep_get_wakeup_cause() != ESP_SLEEP_WAKEUP_UNDEFINED;
    ESP_LOGI(TAG, "odysseus-sound recorder %s; reset reason: %s", FW_VERSION, reset_reason_name());
    if (from_standby) {
        /* EXT1 = the board's wake line (PWR, USB power); anything else is a bug. */
        ESP_LOGI(TAG, "woken from standby, wake cause %d", (int)esp_sleep_get_wakeup_cause());
    }

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
    bg_limits_t lim;
    board_battery_limits(&lim);
    battery_guard_init(&s_batt_guard, &lim);
    log_battery();
    check_battery();

    /* The queue survives reboots: rebuild it from the card (recovers a chunk cut by a power
     * loss). Without a card the queue starts empty; a session start retries. */
    const uint32_t boot_id = esp_random();
    err = chunk_store_init(boot_id);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "chunk queue not read (%s); is the SD card in?", esp_err_to_name(err));
    }
    chunk_store_set_listener(on_queue_changed, NULL);
    const ble_sync_hooks_t hooks = {
        .fw_version = FW_VERSION, .reset_reason = reset_reason_name(),
        .get_state = ble_get_state, .on_time = ble_on_time, .on_passkey = ble_on_passkey};
    ESP_ERROR_CHECK(ble_sync_init(&hooks));

    /* After a power-on one buzz: haptics work and the recorder starts unmuted. Out of
     * standby no buzz, just the status screen: the PWR press that woke the watch does
     * nothing else (it doesn't start a recording). */
    if (from_standby) {
        show_screen();
    } else {
        board_haptic_pulse(1);
    }

    if (power_log_init(power_sample, power_card_busy) != ESP_OK) {
        ESP_LOGW(TAG, "power log: no memory");
    }

    /* A crash or a watchdog restart cut a conversation off: record again. A power cut,
     * brownout or reboot by hand doesn't (s_rec_resume is not kept, or not wanted). */
    const esp_reset_reason_t rr = esp_reset_reason();
    const bool crashed = rr == ESP_RST_PANIC || rr == ESP_RST_TASK_WDT ||
                         rr == ESP_RST_INT_WDT || rr == ESP_RST_WDT;
    const bool resume = crashed && s_rec_resume == REC_RESUME_MAGIC;
    s_rec_resume = 0;
    if (resume) {
        ESP_LOGW(TAG, "restarted by %s while recording: starting a new session",
                 reset_reason_name());
        start_conversation(true);
    }

    ESP_ERROR_CHECK(console_start());
    ESP_LOGI(TAG, "ready: PWR = start/stop recording, PWR held = voice note, BOOT = status "
                  "screen, BOOT x2 = mute, BOOT held = BLE sync window; type `help` on the USB "
                  "console");

    /* Wake only for events and the periodic work (battery guard, battery and power logs),
     * so the chip can light sleep in between. */
    int64_t last_batt = now_ms(), last_check = last_batt;
    for (;;) {
        const int64_t wait = last_check + BATTERY_CHECK_PERIOD_MS - now_ms();
        app_event_t ev;
        if (xQueueReceive(s_events, &ev, pdMS_TO_TICKS(wait > 10 ? wait : 10)) == pdTRUE) {
            handle(&ev);
        }
        power_log_poll();
        if (now_ms() - last_check >= BATTERY_CHECK_PERIOD_MS) {
            last_check = now_ms();
            check_battery();
        }
        if (now_ms() - last_batt >= BATTERY_LOG_PERIOD_MS) {
            last_batt = now_ms();
            log_battery();
        }
    }
}
