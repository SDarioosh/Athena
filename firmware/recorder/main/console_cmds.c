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
#include "chunk_queue.h"
#include "chunk_store.h"
#include "esp_check.h"
#include "esp_console.h"
#include "esp_core_dump.h"
#include "esp_pm.h"
#include "esp_task_wdt.h"
#include "esp_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_rom_crc.h"
#include "esp_system.h"
#include "isotime.h"
#include "event_log_store.h"
#include "power_log.h"
#include "mic.h"
#include "recorder.h"
#include "session_rec.h"

static const char *TAG = "console";

static int cmd_status(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    app_print_status();
    return 0;
}

/* `batt`: the real battery. `batt sim <percent> [mV]` makes the app see that battery with
 * USB unplugged (to test the low-battery guard); `batt sim off` ends it. */
static int cmd_batt(int argc, char **argv)
{
    if (argc >= 2 && strcmp(argv[1], "sim") == 0) {
        if (argc == 3 && strcmp(argv[2], "off") == 0) {
            app_battery_simulate(false, 0, 0);
            printf("battery simulation off\n");
            return 0;
        }
        if (argc < 3 || argc > 4) {
            printf("usage: batt sim <percent> [mV] | batt sim off\n");
            return 1;
        }
        const int percent = atoi(argv[2]);
        const int mv = argc == 4 ? atoi(argv[3]) : 3800;
        app_battery_simulate(true, percent, mv);
        printf("simulating %d%%, %d mV, USB unplugged (checked every 15 s; 2 low readings "
               "in a row count)\n", percent, mv);
        return 0;
    }
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

/* `mics [seconds]`: each mic of an I2S array on its own, once a second (the mix is what
 * `level` shows). `mics auto` mixes every mic that answers; `mics <hex mask>` picks them
 * (bit 0 = A-L, 1 = A-R, 2 = B-L, 3 = B-R). */
static int cmd_mics(int argc, char **argv)
{
    const int nch = mic_array_channels();
    if (nch == 0) {
        printf("this board has one PDM mic; use `level`\n");
        return 1;
    }
    int seconds = 3;
    if (argc >= 2) {
        const bool is_auto = strcmp(argv[1], "auto") == 0;
        if (is_auto || strncmp(argv[1], "0x", 2) == 0) {
            const long m = is_auto ? MIC_MASK_AUTO : strtol(argv[1], NULL, 16);
            if (!is_auto && (m <= 0 || m >= (1L << nch))) {
                printf("mask 0x1..0x%lx\n", (1L << nch) - 1);
                return 1;
            }
            const esp_err_t err = mic_array_set_mask((uint8_t)m);
            printf("%s\n", err == ESP_OK ? "saved; the next recording uses it" : esp_err_to_name(err));
            return err == ESP_OK ? 0 : 1;
        }
        seconds = atoi(argv[1]);
        if (seconds <= 0 || seconds > 60) {
            printf("usage: mics [seconds 1..60] | mics auto | mics 0x<mask>\n");
            return 1;
        }
    }
    const esp_err_t err = mic_start();
    if (err != ESP_OK) {
        printf("mic busy or failed (%s); is a recording running?\n", esp_err_to_name(err));
        return 1;
    }
    const uint8_t alive = mic_array_alive_mask();
    const uint8_t mask = mic_array_mask();
    printf("%d mic slots; mixing mask 0x%x (%s)\n", nch, mask,
           mic_array_mask_setting() == MIC_MASK_AUTO ? "auto: the mics that answer" : "fixed");
    static int16_t buf[1024];
    mic_ch_stats_t st[MIC_MIX_MAX_CH];
    for (int s = 1; s <= seconds; s++) {
        mic_array_stats_enable(true);
        size_t total = 0;
        while (total < MIC_SAMPLE_RATE) {
            size_t got = 0;
            mic_read(buf, sizeof(buf) / sizeof(buf[0]), &got, 500);
            if (got == 0) {
                printf("no audio (no I2S clock?)\n");
                s = seconds;
                break;
            }
            total += got;
        }
        mic_array_stats(st);
        printf("  %2d s:", s);
        for (int ch = 0; ch < nch; ch++) {
            const int32_t peak = st[ch].count ? (st[ch].max > -st[ch].min ? st[ch].max : -st[ch].min) : 0;
            printf("  %s %6.1f dBFS pk %7ld%s", mic_array_channel_name(ch),
                   (double)mic_mix_rms_dbfs(&st[ch]), (long)peak,
                   mic_mix_alive(&st[ch]) ? "" : " (none)");
        }
        printf("\n");
    }
    mic_array_stats_enable(false);
    const uint32_t short_reads = mic_array_pair_b_short_reads();
    mic_stop();
    if (nch == 4) {
        printf("pair B: %s\n", short_reads ? "short reads: it isn't getting the shared clock"
                                            : "in step with pair A (shared clock ok)");
    }
    printf("answered at start:");
    for (int ch = 0; ch < nch; ch++) {
        printf(" %s %s", mic_array_channel_name(ch), (alive >> ch) & 1 ? "ok" : "--");
    }
    printf("\n(24-bit levels: an INMP441 idles near -85 dBFS, speech at 1 m ~ -60 dBFS)\n");
    return 0;
}

static int cmd_stop(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    app_post(APP_EV_REC_STOP, 0);
    return 0;
}

/* Whether a recording (`rec`, a session or a voice note) is using the card: console commands
 * then leave it alone (their reads would hold up the writer, and a write error power-cycles
 * the card under them). tools/pull.py waits for this exact message. */
static bool card_busy(void)
{
    recorder_state_t rs;
    recorder_get_state(&rs);
    if (rs.running || session_rec_running()) {
        printf("recording in progress; not touching the card\n");
        return true;
    }
    return false;
}

/* Mount the card for a console command, unless a recording is using it. */
static const char *console_mount(void)
{
    if (card_busy()) {
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

/* `power [log <minutes>|off | flush | locks]`: battery, state and drain now; the power log
 * (power_log.h); `locks` lists the power-management locks (what keeps the CPU awake). */
static int cmd_power(int argc, char **argv)
{
    if (argc == 1) {
        power_log_print();
        return 0;
    }
    if (strcmp(argv[1], "log") == 0 && argc == 3) {
        const int min = strcmp(argv[2], "off") == 0 ? 0 : atoi(argv[2]);
        const esp_err_t err = power_log_set_period(min);
        if (min) {
            printf("power log every %d min (%s)\n", min, esp_err_to_name(err));
        } else {
            printf("power log off (%s)\n", esp_err_to_name(err));
        }
        return err == ESP_OK ? 0 : 1;
    }
    if (strcmp(argv[1], "flush") == 0) {
        const esp_err_t err = power_log_flush();
        printf("power log written: %s\n", esp_err_to_name(err));
        return err == ESP_OK ? 0 : 1;
    }
    if (strcmp(argv[1], "locks") == 0) {
        esp_pm_dump_locks(stdout);
        return 0;
    }
    printf("usage: power [log <minutes>|off | flush | locks]\n");
    return 1;
}

/* `coredump [get|erase]`: the crash saved in the coredump partition. Without an argument,
 * a summary (task, PC, cause, backtrace); `get` sends the ELF over USB in `get`'s framing
 * (tools/pull.py --coredump saves it; decode with `idf.py coredump-info -c <file>`); `erase`
 * deletes it. The ELF holds task stacks, which can include a few ms of mic samples: keep the
 * file outside the repository and delete it after use. */
static int cmd_coredump(int argc, char **argv)
{
    if (argc >= 2 && strcmp(argv[1], "erase") == 0) {
        const esp_err_t err = esp_core_dump_image_erase();
        printf("core dump erase: %s\n", esp_err_to_name(err));
        return err == ESP_OK ? 0 : 1;
    }
    const esp_err_t check = esp_core_dump_image_check();
    if (check != ESP_OK) {
        printf("no core dump saved (%s)\n", esp_err_to_name(check));
        return 0;
    }
    size_t addr = 0, size = 0;
    if (esp_core_dump_image_get(&addr, &size) != ESP_OK) {
        printf("core dump: can't locate it\n");
        return 1;
    }
    if (argc >= 2 && strcmp(argv[1], "get") == 0) {
        printf("@@BEGIN coredump.elf %u\n", (unsigned)size);
        static uint8_t buf[57 * 16];
        char line[80];
        uint32_t crc = 0;
        bool ok = true;
        for (size_t off = 0; off < size; off += sizeof(buf)) {
            const size_t n = size - off < sizeof(buf) ? size - off : sizeof(buf);
            if (esp_flash_read(NULL, buf, addr + off, n) != ESP_OK) {
                ok = false;
                break;
            }
            crc = esp_rom_crc32_le(crc, buf, n);
            for (size_t i = 0; i < n; i += 57) {
                base64_line(buf + i, n - i < 57 ? n - i : 57, line);
                printf("%s\n", line);
            }
        }
        printf("@@END %08" PRIx32 "%s\n", crc, ok ? "" : " READ_ERROR");
        return ok ? 0 : 1;
    }
    if (argc >= 2) {
        printf("usage: coredump [get|erase]\n");
        return 1;
    }
    static esp_core_dump_summary_t sum; /* ~400 bytes: not on the console task's stack */
    const esp_err_t err = esp_core_dump_get_summary(&sum);
    printf("core dump: %u bytes at 0x%x\n", (unsigned)size, (unsigned)addr);
    if (err != ESP_OK) {
        printf("summary: %s\n", esp_err_to_name(err));
        return 1;
    }
    printf("task: %s\nPC: 0x%08" PRIx32 "\ncause: %" PRIu32 " (Xtensa EXCCAUSE), vaddr 0x%08" PRIx32
           "\nbacktrace:", sum.exc_task, sum.exc_pc, sum.ex_info.exc_cause,
           sum.ex_info.exc_vaddr);
    for (uint32_t i = 0; i < sum.exc_bt_info.depth; i++) {
        printf(" 0x%08" PRIx32, sum.exc_bt_info.bt[i]);
    }
    printf("%s\n", sum.exc_bt_info.corrupted ? " (corrupted)" : "");
    printf("decode: tools/pull.py --coredump, then idf.py coredump-info -c <file>\n");
    return 0;
}

/* `crash yes`: crash on purpose (abort) to test the core dump and the reset reason. */
static int cmd_crash(int argc, char **argv)
{
    if (argc == 2 && strcmp(argv[1], "wdt") == 0) {
        /* A task that stops feeding the task watchdog: it fires after
         * CONFIG_ESP_TASK_WDT_TIMEOUT_S and restarts the watch (reset=task_wdt). */
        printf("hanging the console task (task watchdog test)\n");
        fflush(stdout);
        esp_task_wdt_add(NULL);
        for (;;) {
            vTaskDelay(pdMS_TO_TICKS(100));
        }
    }
    if (argc != 2 || strcmp(argv[1], "yes") != 0) {
        printf("usage: crash yes (abort) | crash wdt (hang a watched task); the watch saves a "
               "core dump and restarts\n");
        return 1;
    }
    printf("crashing on purpose\n");
    fflush(stdout);
    vTaskDelay(pdMS_TO_TICKS(100));
    abort();
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

/* `session [start|stop]`: the Opus conversation recording session (same as a PWR press). */
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

/* `note [start|stop]`: a voice note (same as holding PWR ~1 s; a PWR press or hold stops it). */
static int cmd_note(int argc, char **argv)
{
    if (argc >= 2 && strcmp(argv[1], "start") == 0) {
        app_post(APP_EV_NOTE_START, 0);
    } else if (argc >= 2 && strcmp(argv[1], "stop") == 0) {
        app_post(APP_EV_NOTE_STOP, 0);
    } else if (argc >= 2) {
        printf("usage: note [start|stop]\n");
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
        uint32_t id;
        if (!cq_parse_id(argv[2], &id)) {
            printf("usage: queue drop <chunk id> (decimal as `queue` shows it, or 0x<hex>)\n");
            return 1;
        }
        const esp_err_t err = chunk_store_drop(id);
        printf("drop %" PRIu32 ": %s\n", id, esp_err_to_name(err));
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
    if (argc == 3 && strcmp(argv[1], "failwrite") == 0) {
        const int n = atoi(argv[2]);
        chunk_store_fail_writes(n);
        printf("the next %d chunk write(s) will fail (test)\n", n);
        return 0;
    }
    printf("card detect: %s\n", board_storage_card_present() ? "card in" : "no card");
    if (card_busy()) {
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

/* `events [clear]`: the flash event log (event_log.h): what started and ended recordings. */
static int cmd_events(int argc, char **argv)
{
    if (argc >= 2 && strcmp(argv[1], "clear") == 0) {
        event_log_clear();
    } else if (argc >= 2) {
        printf("usage: events [clear]\n");
        return 1;
    } else {
        event_log_print();
    }
    return 0;
}

static const esp_console_cmd_t cmds[] = {
    {.command = "status", .help = "Recorder state, battery, time, card", .func = cmd_status},
    {.command = "batt", .help = "Battery voltage, percent, charging; `batt sim <percent> [mV]` / `batt sim off` fakes it (test)", .func = cmd_batt},
    {.command = "rails", .help = "Read back every power rail from the PMU", .func = cmd_rails},
    {.command = "exp", .help = "Log the XL9555 expander pins (pin-map check)", .func = cmd_exp},
    {.command = "time", .help = "Show time; `time 2026-10-19T12:34:56Z` sets system + RTC", .func = cmd_time},
    {.command = "session", .help = "Recording session to Opus chunks: `session [start|stop]` (= PWR press)", .func = cmd_session},
    {.command = "note", .help = "Voice note, max 5 min: `note [start|stop]` (= PWR held ~1 s)", .func = cmd_note},
    {.command = "queue", .help = "Chunks waiting for the phone; `queue drop <id>` deletes one", .func = cmd_queue},
    {.command = "ble", .help = "BLE state; `ble pair` opens the 2 min sync window; `ble forget` deletes bonds", .func = cmd_ble},
    {.command = "rec", .help = "Test: record N seconds (default 60) from the mic to the SD card as WAV", .func = cmd_rec},
    {.command = "level", .help = "Print mic level for N s (default 5): `level [s]`", .func = cmd_level},
    {.command = "mics", .help = "Mic array: each mic's level for N s (default 3); `mics auto` / `mics 0x<mask>` picks the mics mixed, saved", .func = cmd_mics},
    {.command = "stop", .help = "Stop the test WAV recording early (keeps what was recorded)", .func = cmd_stop},
    {.command = "eq", .help = "Show or switch the mic EQ, saved: `eq [on|off]`", .func = cmd_eq},
    {.command = "gain",.help = "Show or set the mic gain in dB, saved: `gain [0..42]`", .func = cmd_gain},
    {.command = "mute", .help = "Toggle mute (same as a BOOT double press)", .func = cmd_mute},
    {.command = "screen", .help = "Show the status screen for ~5 s (same as a BOOT press)", .func = cmd_screen},
    {.command = "sync", .help = "Open the BLE sync window for 2 min (= BOOT long press)", .func = cmd_sync},
    {.command = "ls", .help = "List the files on the SD card; `ls q` the chunk queue", .func = cmd_ls},
    {.command = "get", .help = "Send a card file (or q/<chunk>) over USB as base64 (tools/pull.py saves it)", .func = cmd_get},
    {.command = "sd", .help = "Power, mount and report the SD card, then power it off; `sd failwrite <n>` makes the next n chunk writes fail (test)", .func = cmd_sd},
    {.command = "standby", .help = "Deep sleep now; PWR or USB power wakes it", .func = cmd_standby},
    {.command = "reboot", .help = "Restart", .func = cmd_reboot},
    {.command = "events", .help = "What started and ended recordings (buttons with their PMU bits, stops, ends, boots), kept in flash; `events clear`", .func = cmd_events},
    {.command = "power", .help = "Battery, state, drain rate and the power log; `power log <min>|off`, `power flush`, `power locks`", .func = cmd_power},
    {.command = "coredump", .help = "The last crash: summary; `coredump get` sends the ELF (tools/pull.py --coredump), `coredump erase`", .func = cmd_coredump},
    {.command = "crash", .help = "`crash yes` (abort) / `crash wdt` (hang a watched task): crash on purpose to test the core dump, watchdog and resume", .func = cmd_crash},
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
