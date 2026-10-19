#include "console_cmds.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>

#include "app.h"
#include "audio_stats.h"
#include "board.h"
#include "esp_check.h"
#include "esp_console.h"
#include "esp_log.h"
#include "esp_system.h"
#include "isotime.h"
#include "mic.h"
#include "recorder.h"

static const char *TAG = "console";

static int cmd_status(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    app_print_status();
    return 0;
}

static int cmd_batt(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    board_battery_t b;
    if (board_battery_read(&b) != ESP_OK) {
        printf("battery: read failed\n");
        return 1;
    }
    printf("battery: %s, %d mV, %d%%, %s, USB %s\n", b.present ? "present" : "absent", b.millivolts,
           b.percent, b.charging ? "charging" : "not charging", b.vbus ? "in" : "out");
    return 0;
}

static int cmd_rails(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    board_log_power();
    return 0;
}

static int cmd_exp(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    board_log_expander();
    return 0;
}

static int cmd_time(int argc, char **argv)
{
    char buf[24];
    if (argc >= 2) {
        int64_t epoch;
        if (!isotime_parse(argv[1], &epoch)) {
            printf("usage: time [YYYY-MM-DDTHH:MM:SSZ | epoch]\n");
            return 1;
        }
        struct timeval tv = {.tv_sec = (time_t)epoch, .tv_usec = 0};
        settimeofday(&tv, NULL);
        const esp_err_t err = board_rtc_set(epoch);
        isotime_format(epoch, buf, sizeof(buf));
        printf("time set to %s; RTC write %s\n", buf, esp_err_to_name(err));
    }
    struct timeval now;
    gettimeofday(&now, NULL);
    isotime_format(now.tv_sec, buf, sizeof(buf));
    printf("system time: %s%s\n", buf, isotime_is_plausible(now.tv_sec) ? "" : " (not set)");
    int64_t rtc = 0;
    bool valid = false;
    if (board_rtc_get(&rtc, &valid) == ESP_OK) {
        isotime_format(rtc, buf, sizeof(buf));
        printf("rtc time:    %s (%s)\n", buf, valid ? "valid" : "not set or lost power");
    }
    return 0;
}

static int cmd_rec(int argc, char **argv)
{
    int seconds = 60;
    if (argc >= 2) {
        seconds = atoi(argv[1]);
        if (seconds <= 0 || seconds > 3600) {
            printf("usage: rec [seconds 1..3600]\n");
            return 1;
        }
    }
    app_post(APP_EV_RECORD, seconds);
    return 0;
}

/* `level [seconds]`: read the mic without the SD card and print its level once a second, then
 * a summary. Answers "is the mic delivering real audio" over USB. */
static int cmd_level(int argc, char **argv)
{
    int seconds = 5;
    if (argc >= 2) {
        seconds = atoi(argv[1]);
        if (seconds <= 0 || seconds > 60) {
            printf("usage: level [seconds 1..60]\n");
            return 1;
        }
    }
    const esp_err_t err = mic_start();
    if (err != ESP_OK) {
        printf("mic busy or failed (%s); is a recording running?\n", esp_err_to_name(err));
        return 1;
    }
    static int16_t buf[1024]; /* only the console task uses it */
    audio_stats_t total, second;
    audio_stats_reset(&total);
    audio_stats_reset(&second);
    printf("listening %d s; talk, then stay quiet\n", seconds);
    const uint64_t target = (uint64_t)seconds * MIC_SAMPLE_RATE;
    while (total.count < target) {
        size_t got = 0;
        mic_read(buf, sizeof(buf) / sizeof(buf[0]), &got, 500);
        if (got == 0) {
            printf("no audio from the mic\n");
            break;
        }
        audio_stats_add(&total, buf, got);
        audio_stats_add(&second, buf, got);
        if (second.count >= MIC_SAMPLE_RATE || total.count >= target) {
            printf("  %2d s: rms %5.1f dBFS, peak %5lu\n", (int)(total.count / MIC_SAMPLE_RATE),
                   (double)audio_stats_rms_dbfs(&second), (unsigned long)second.peak);
            audio_stats_reset(&second);
        }
    }
    mic_stop();
    char line[128];
    audio_stats_describe(&total, line, sizeof(line));
    printf("level: %s\n", line);
    return 0;
}

static int cmd_stop(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    app_post(APP_EV_REC_STOP, 0);
    return 0;
}

static int cmd_mute(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    app_post(APP_EV_MUTE_TOGGLE, 0);
    return 0;
}

static int cmd_screen(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    app_post(APP_EV_SCREEN_SHOW, 0);
    return 0;
}

static int cmd_sync(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    app_post(APP_EV_SYNC_NOW, 0);
    return 0;
}

static int cmd_sd(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    printf("card detect: %s\n", board_storage_card_present() ? "card in" : "no card");
    recorder_state_t rs;
    recorder_get_state(&rs);
    if (rs.running) {
        printf("recording in progress; not touching the card\n");
        return 0;
    }
    const char *mount = NULL;
    esp_err_t err = board_storage_mount(&mount);
    if (err != ESP_OK) {
        printf("mount failed: %s\n", esp_err_to_name(err));
        return 1;
    }
    uint64_t total = 0, freeb = 0;
    if (board_storage_info(&total, &freeb) == ESP_OK) {
        printf("mounted at %s: %llu MB total, %llu MB free\n", mount,
               (unsigned long long)(total >> 20), (unsigned long long)(freeb >> 20));
    }
    board_storage_unmount();
    return 0;
}

static int cmd_reboot(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    printf("rebooting\n");
    fflush(stdout);
    esp_restart();
    return 0;
}

static const esp_console_cmd_t cmds[] = {
    {.command = "status", .help = "Recorder state, battery, time, card", .func = cmd_status},
    {.command = "batt", .help = "Battery voltage, percent, charging", .func = cmd_batt},
    {.command = "rails", .help = "Read back every power rail from the PMU", .func = cmd_rails},
    {.command = "exp", .help = "Log the XL9555 expander pins (pin-map check)", .func = cmd_exp},
    {.command = "time", .help = "Show time; `time 2026-10-19T12:34:56Z` sets system + RTC", .func = cmd_time},
    {.command = "rec", .help = "Record N seconds (default 60) from the mic to the SD card as WAV", .func = cmd_rec},
    {.command = "level", .help = "Print mic level for N s (default 5): `level [s]`", .func = cmd_level},
    {.command = "stop", .help = "Stop the running recording early (keeps what was recorded)", .func = cmd_stop},
    {.command = "mute", .help = "Toggle mute (same as a BOOT short press)", .func = cmd_mute},
    {.command = "screen", .help = "Show the status screen for ~5 s (same as a PWR short press)", .func = cmd_screen},
    {.command = "sync", .help = "Log 'sync now' (BLE comes in a later milestone)", .func = cmd_sync},
    {.command = "sd", .help = "Power, mount and report the SD card, then power it off", .func = cmd_sd},
    {.command = "reboot", .help = "Restart", .func = cmd_reboot},
};

esp_err_t console_start(void)
{
    esp_console_repl_t *repl = NULL;
    esp_console_repl_config_t repl_cfg = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
    repl_cfg.prompt = "recorder>";
    repl_cfg.max_cmdline_length = 128;
    esp_console_dev_usb_serial_jtag_config_t hw = ESP_CONSOLE_DEV_USB_SERIAL_JTAG_CONFIG_DEFAULT();
    ESP_RETURN_ON_ERROR(esp_console_new_repl_usb_serial_jtag(&hw, &repl_cfg, &repl), TAG, "repl");
    ESP_RETURN_ON_ERROR(esp_console_register_help_command(), TAG, "help");
    for (unsigned i = 0; i < sizeof(cmds) / sizeof(cmds[0]); i++) {
        ESP_RETURN_ON_ERROR(esp_console_cmd_register(&cmds[i]), TAG, "register %s", cmds[i].command);
    }
    return esp_console_start_repl(repl);
}
