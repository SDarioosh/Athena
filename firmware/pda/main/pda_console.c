/* USB console of the PDA (firmware/pda/README.md lists the commands). */
#include "pda_console.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>

#include "crc32.h"
#include "esp_console.h"
#include "esp_log.h"
#include "esp_system.h"
#include "isotime.h"
#include "mbedtls/base64.h"
#include "pda_epd.h"
#include "t5_board.h"
#include "touch_gt911.h"
#include "pda_app.h"

static int cmd_i2c(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    t5_board_log_i2c();
    return 0;
}

static int cmd_batt(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    t5_battery_t b;
    const esp_err_t err = t5_battery_read(&b);
    printf("battery: %s, %d mV, %d mA, %d%%, %s, USB %s%s\n", b.present ? "present" : "absent",
           b.millivolts, b.milliamps, b.percent, b.charging ? "charging" : "not charging",
           b.vbus ? "in" : "out", err == ESP_OK ? "" : " (read error)");
    return 0;
}

static int cmd_rtc(int argc, char **argv)
{
    if (argc >= 3 && strcmp(argv[1], "set") == 0) {
        int64_t t;
        if (!isotime_parse(argv[2], &t)) {
            printf("usage: rtc set <epoch | YYYY-MM-DDTHH:MM:SSZ>\n");
            return 1;
        }
        const esp_err_t err = t5_rtc_set(t);
        printf("rtc set: %s\n", esp_err_to_name(err));
    }
    int64_t t = 0;
    bool valid = false;
    const esp_err_t err = t5_rtc_get(&t, &valid);
    char buf[24];
    isotime_format(t, buf, sizeof(buf));
    printf("rtc: %s %s (%s)\n", buf, valid ? "valid" : "NOT valid", esp_err_to_name(err));
    return 0;
}

static const char *const KIND_NAME[] = {"partial (GL16)", "full (GC16)", "clean (clear + GC16)"};

static void print_epd_stats(void)
{
    pda_epd_stats_t s;
    pda_epd_get_stats(&s);
    for (int k = 0; k < 3; k++) {
        printf("epd %-22s %4" PRIu32 " updates, last %d ms\n", KIND_NAME[k], s.count[k],
               s.last_ms[k]);
    }
    printf("epd updates since clean: %d (clean after %d)\n", s.since_clean,
           PDA_EPD_PARTIAL_LIMIT);
}

static int bench_one(pda_epd_kind_t kind, const EpdRect *area, const char *what)
{
    const pda_epd_kind_t done = area ? pda_epd_update_area(*area) : pda_epd_update(kind);
    pda_epd_stats_t s;
    pda_epd_get_stats(&s);
    printf("%-28s %s: %d ms\n", what, KIND_NAME[done], s.last_ms[done]);
    return 0;
}

static int cmd_epd(int argc, char **argv)
{
    if (argc >= 2 && strcmp(argv[1], "bench") == 0) {
        uint8_t *fb = pda_epd_fb();
        bench_one(PDA_EPD_CLEAN, NULL, "clean refresh");
        bench_one(PDA_EPD_PARTIAL, NULL, "nothing changed");
        const EpdRect sq = {.x = 100, .y = 320, .width = 100, .height = 100};
        epd_fill_rect(sq, EPD_BLACK, fb);
        bench_one(PDA_EPD_PARTIAL, &sq, "100x100 area changed");
        const EpdRect row = {.x = 0, .y = 700, .width = PDA_SCR_W, .height = 60};
        epd_fill_rect(row, EPD_GREY, fb);
        bench_one(PDA_EPD_PARTIAL, &row, "960x60 row changed");
        for (int i = 0; i < PDA_SCR_W; i += 8) {
            epd_draw_vline(i, 0, PDA_SCR_H, (i / 8) & 1 ? EPD_BLACK : EPD_LIGHT, fb);
        }
        bench_one(PDA_EPD_PARTIAL, NULL, "whole screen changed");
        pda_epd_clear_fb();
        epd_fill_rect(sq, EPD_DARK, fb);
        bench_one(PDA_EPD_FULL, NULL, "new screen (mostly white)");
        const pda_ev_t ev = {.type = PDA_EV_REDRAW};
        pda_app_post(&ev); /* the UI draws its screen again */
        print_epd_stats();
        return 0;
    }
    if (argc >= 2 && strcmp(argv[1], "clean") == 0) {
        pda_epd_update(PDA_EPD_CLEAN);
    }
    print_epd_stats();
    return 0;
}

static int cmd_touch(int argc, char **argv)
{
    if (argc >= 3 && strcmp(argv[1], "trace") == 0) {
        touch_trace(atoi(argv[2]) * 1000);
        printf("tracing raw touch for %s s\n", argv[2]);
        return 0;
    }
    touch_print();
    return 0;
}

/* The framebuffer as base64 between markers; tools/fbdump.py turns it into a PNG. */
static int cmd_fb(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    const uint8_t *fb = pda_epd_fb();
    const size_t total = PDA_EPD_W / 2 * PDA_EPD_H;
    /* the physical (landscape) buffer, and the rotation to view it upright */
    printf("FB BEGIN %d %d %08" PRIx32 " %d\n", PDA_EPD_W, PDA_EPD_H, crc32_update(0, fb, total),
           pda_epd_rotation());
    unsigned char line[160];
    for (size_t off = 0; off < total; off += 96) {
        const size_t n = total - off < 96 ? total - off : 96;
        size_t olen = 0;
        mbedtls_base64_encode(line, sizeof(line), &olen, fb + off, n);
        line[olen] = 0;
        printf("%s\n", line);
    }
    printf("FB END\n");
    return 0;
}

static int cmd_reboot(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    esp_restart();
    return 0;
}

void pda_console_register(const char *name, const char *help, esp_console_cmd_func_t fn)
{
    const esp_console_cmd_t c = {.command = name, .help = help, .func = fn};
    ESP_ERROR_CHECK(esp_console_cmd_register(&c));
}

void pda_console_start(void)
{
    esp_console_repl_t *repl = NULL;
    esp_console_repl_config_t cfg = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
    cfg.prompt = "pda> ";
    cfg.max_cmdline_length = 512;
    esp_console_dev_usb_serial_jtag_config_t dev = ESP_CONSOLE_DEV_USB_SERIAL_JTAG_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_console_new_repl_usb_serial_jtag(&dev, &cfg, &repl));
    esp_console_register_help_command();
    pda_console_register("i2c", "Probe the board's I2C chips", cmd_i2c);
    pda_console_register("batt", "Battery gauge and charger", cmd_batt);
    pda_console_register("rtc", "rtc [set <epoch|ISO>]: read or set the RTC", cmd_rtc);
    pda_console_register("epd", "epd [bench|clean]: refresh stats, timing test, clean refresh",
                         cmd_epd);
    pda_console_register("touch", "touch [trace <s>]: touch stats, raw sample log", cmd_touch);
    pda_console_register("fb", "Dump the framebuffer (tools/fbdump.py)", cmd_fb);
    pda_console_register("reboot", "Restart", cmd_reboot);
    pda_console_register_app();
    ESP_ERROR_CHECK(esp_console_start_repl(repl));
}
