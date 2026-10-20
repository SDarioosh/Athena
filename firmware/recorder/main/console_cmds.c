#include "console_cmds.h"

#include <dirent.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/time.h>

#include "app.h"
#include "audio_dsp.h"
#include "audio_stats.h"
#include "ble_sync.h"
#include "board.h"
#include "chunk_store.h"
#include "esp_check.h"
#include "esp_console.h"
#include "esp_log.h"
#include "esp_rom_crc.h"
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
    printf("level at %d dB gain, EQ %s: %s; limiter on %llu samples\n", mic_gain_db(),
           mic_eq_enabled() ? "on" : "off", line, (unsigned long long)mic_limited_samples());
    return 0;
}

static int cmd_stop(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    app_post(APP_EV_REC_STOP, 0);
    return 0;
}

/* Mount the card for a console command, unless a recording is using it. */
static const char *console_mount(void)
{
    recorder_state_t rs;
    recorder_get_state(&rs);
    if (rs.running) {
        printf("recording in progress; not touching the card\n");
        return NULL;
    }
    const char *mount = NULL;
    const esp_err_t err = board_storage_mount(&mount);
    if (err != ESP_OK) {
        printf("mount failed: %s\n", esp_err_to_name(err));
        return NULL;
    }
    return mount;
}

/* Only plain file names in the card's root or in the chunk queue folder `q/`: no other
 * paths, so `get` can't wander. */
static bool valid_name(const char *name)
{
    if (!name[0] || strlen(name) > 64) {
        return false;
    }
    if (strncmp(name, "q/", 2) == 0) {
        name += 2;
        if (!name[0]) {
            return false;
        }
    }
    for (const char *c = name; *c; c++) {
        if (*c == '/' || *c == '\\' || *c == ':' || (c == name && *c == '.')) {
            return false;
        }
    }
    return true;
}

/* `ls` lists the card's root; `ls q` the chunk queue (names printed as q/<file>, ready
 * for `get`). */
static int cmd_ls(int argc, char **argv)
{
    const bool queue = argc >= 2 && strcmp(argv[1], "q") == 0;
    if (argc >= 2 && !queue) {
        printf("usage: ls [q]\n");
        return 1;
    }
    const char *mount = console_mount();
    if (!mount) {
        return 1;
    }
    static char dirpath[64];
    snprintf(dirpath, sizeof(dirpath), "%s%s", mount, queue ? "/q" : "");
    DIR *dir = opendir(dirpath);
    int n = 0;
    if (dir) {
        struct dirent *e;
        static char path[330]; /* dirpath + a 255-byte FAT long name */
        while ((e = readdir(dir)) != NULL) {
            struct stat st;
            snprintf(path, sizeof(path), "%s/%s", dirpath, e->d_name);
            if (stat(path, &st) == 0 && S_ISREG(st.st_mode)) {
                printf("%10ld  %s%s\n", (long)st.st_size, queue ? "q/" : "", e->d_name);
                n++;
            }
        }
        closedir(dir);
    }
    printf("%d file(s)\n", n);
    board_storage_unmount();
    return 0;
}

static void base64_line(const uint8_t *in, size_t n, char *out)
{
    static const char tbl[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    char *o = out;
    for (size_t i = 0; i < n; i += 3) {
        const uint32_t v = ((uint32_t)in[i] << 16) | (i + 1 < n ? (uint32_t)in[i + 1] << 8 : 0) |
                           (i + 2 < n ? in[i + 2] : 0);
        *o++ = tbl[(v >> 18) & 63];
        *o++ = tbl[(v >> 12) & 63];
        *o++ = i + 1 < n ? tbl[(v >> 6) & 63] : '=';
        *o++ = i + 2 < n ? tbl[v & 63] : '=';
    }
    *o = '\0';
}

/* `get <file>`: send a file from the card over the USB console as base64 lines between
 * "@@BEGIN <name> <size>" and "@@END <crc32>". firmware/recorder/tools/pull.py saves it. */
static int cmd_get(int argc, char **argv)
{
    if (argc < 2 || !valid_name(argv[1])) {
        printf("usage: get <file name from `ls`>\n");
        return 1;
    }
    const char *mount = console_mount();
    if (!mount) {
        return 1;
    }
    static char path[300];
    snprintf(path, sizeof(path), "%s/%s", mount, argv[1]);
    FILE *f = fopen(path, "rb");
    if (!f) {
        printf("no such file: %s\n", argv[1]);
        board_storage_unmount();
        return 1;
    }
    struct stat st;
    stat(path, &st);
    printf("@@BEGIN %s %ld\n", argv[1], (long)st.st_size);
    /* 57 bytes per line -> 76 base64 characters. Read 80 lines' worth per card access. */
    static uint8_t buf[57 * 80];
    char line[80];
    uint32_t crc = 0;
    size_t got;
    while ((got = fread(buf, 1, sizeof(buf), f)) > 0) {
        crc = esp_rom_crc32_le(crc, buf, got);
        for (size_t off = 0; off < got; off += 57) {
            base64_line(buf + off, got - off < 57 ? got - off : 57, line);
            printf("%s\n", line);
        }
    }
    const bool ok = !ferror(f);
    fclose(f);
    board_storage_unmount();
    printf("@@END %08" PRIx32 "%s\n", crc, ok ? "" : " READ_ERROR");
    return ok ? 0 : 1;
}

/* `gain [dB]`: show or set the mic gain (saved in NVS, used from the next recording). */
static int cmd_gain(int argc, char **argv)
{
    if (argc >= 2) {
        char *end;
        const long db = strtol(argv[1], &end, 10);
        if (*end || db < AUDIO_DSP_GAIN_MIN_DB || db > AUDIO_DSP_GAIN_MAX_DB) {
            printf("usage: gain [%d..%d dB]\n", AUDIO_DSP_GAIN_MIN_DB, AUDIO_DSP_GAIN_MAX_DB);
            return 1;
        }
        const esp_err_t err = mic_set_gain_db((int)db);
        if (err != ESP_OK) {
            printf("not set (%s); is a recording running?\n", esp_err_to_name(err));
            return 1;
        }
    }
    printf("mic gain %d dB (default %d), high-pass %d Hz, EQ %s (%d bands), limiter -1 dBFS\n",
           mic_gain_db(), MIC_DEFAULT_GAIN_DB, MIC_HIGHPASS_HZ, mic_eq_enabled() ? "on" : "off",
           mic_eq_band_count());
    return 0;
}

/* `eq [on|off]`: show or switch the board's mic EQ (saved in NVS, used from the next
 * recording). Lets the owner compare recordings with and without it. */
static int cmd_eq(int argc, char **argv)
{
    if (argc >= 2) {
        const bool on = strcmp(argv[1], "on") == 0;
        if (!on && strcmp(argv[1], "off") != 0) {
            printf("usage: eq [on|off]\n");
            return 1;
        }
        const esp_err_t err = mic_set_eq_enabled(on);
        if (err != ESP_OK) {
            printf("not set (%s); is a recording running?\n", esp_err_to_name(err));
            return 1;
        }
    }
    printf("mic EQ %s (%d bands from the board)\n", mic_eq_enabled() ? "on" : "off",
           mic_eq_band_count());
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

/* `session [start|stop]`: the Opus recording session (same as BOOT+PWR). */
static int cmd_session(int argc, char **argv)
{
    if (argc >= 2 && strcmp(argv[1], "start") == 0) {
        app_post(APP_EV_SESSION_START, 0);
    } else if (argc >= 2 && strcmp(argv[1], "stop") == 0) {
        app_post(APP_EV_SESSION_STOP, 0);
    } else if (argc >= 2) {
        printf("usage: session [start|stop]\n");
        return 1;
    } else {
        app_print_status();
    }
    return 0;
}

/* `queue [drop <id>]`: chunks waiting for the phone; drop deletes one without uploading. */
static int cmd_queue(int argc, char **argv)
{
    if (argc >= 3 && strcmp(argv[1], "drop") == 0) {
        char *end;
        const unsigned long id = strtoul(argv[2], &end, 0);
        if (*end || id == 0 || id > UINT32_MAX) {
            printf("usage: queue drop <chunk id>\n");
            return 1;
        }
        const esp_err_t err = chunk_store_drop((uint32_t)id);
        printf("drop %lu: %s\n", id, esp_err_to_name(err));
        return err == ESP_OK ? 0 : 1;
    }
    if (argc >= 2) {
        printf("usage: queue [drop <id>]\n");
        return 1;
    }
    chunk_store_print();
    return 0;
}

/* `ble [pair|forget]`: BLE state; open the sync window; delete every bond. */
static int cmd_ble(int argc, char **argv)
{
    if (argc >= 2 && strcmp(argv[1], "pair") == 0) {
        app_post(APP_EV_SYNC_NOW, 0);
        return 0;
    }
    if (argc >= 2 && strcmp(argv[1], "forget") == 0) {
        const esp_err_t err = ble_sync_forget_bonds();
        printf("bonds deleted: %s\n", esp_err_to_name(err));
        return err == ESP_OK ? 0 : 1;
    }
    if (argc >= 2) {
        printf("usage: ble [pair|forget]\n");
        return 1;
    }
    ble_sync_print();
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

static int cmd_standby(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    printf("standby: deep sleep until PWR is pressed or USB power is plugged in\n");
    fflush(stdout);
    app_post(APP_EV_STANDBY, 0);
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
    {.command = "session", .help = "Recording session to Opus chunks: `session [start|stop]` (= BOOT+PWR)", .func = cmd_session},
    {.command = "queue", .help = "Chunks waiting for the phone; `queue drop <id>` deletes one", .func = cmd_queue},
    {.command = "ble", .help = "BLE state; `ble pair` opens the 2 min sync window; `ble forget` deletes bonds", .func = cmd_ble},
    {.command = "rec", .help = "Test: record N seconds (default 60) from the mic to the SD card as WAV", .func = cmd_rec},
    {.command = "level", .help = "Print mic level for N s (default 5): `level [s]`", .func = cmd_level},
    {.command = "stop", .help = "Stop the test WAV recording early (keeps what was recorded)", .func = cmd_stop},
    {.command = "eq", .help = "Show or switch the mic EQ, saved: `eq [on|off]`", .func = cmd_eq},
    {.command = "gain",.help = "Show or set the mic gain in dB, saved: `gain [0..42]`", .func = cmd_gain},
    {.command = "mute", .help = "Toggle mute (same as a BOOT short press)", .func = cmd_mute},
    {.command = "screen", .help = "Show the status screen for ~5 s (same as a PWR short press)", .func = cmd_screen},
    {.command = "sync", .help = "Open the BLE sync window for 2 min (= BOOT long press)", .func = cmd_sync},
    {.command = "ls", .help = "List the files on the SD card; `ls q` the chunk queue", .func = cmd_ls},
    {.command = "get", .help = "Send a card file (or q/<chunk>) over USB as base64 (tools/pull.py saves it)", .func = cmd_get},
    {.command = "sd", .help = "Power, mount and report the SD card, then power it off", .func = cmd_sd},
    {.command = "standby", .help = "Deep sleep now; PWR or USB power wakes it", .func = cmd_standby},
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
