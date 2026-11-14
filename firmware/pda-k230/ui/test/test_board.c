/* board.c over fake sysfs trees: the backlight (rm69a10 first, 0..255), the goodix touch
 * node, the athena-board battery output, the launcher hand-over file. */
#include <sys/stat.h>
#include <unistd.h>

#include "board.h"
#include "check.h"
#include "test_util.h"

static void put(const char *path, const char *text)
{
    FILE *f = fopen(path, "w");
    if (!f) {
        perror(path);
        exit(2);
    }
    fputs(text, f);
    fclose(f);
}

static long get(const char *path)
{
    FILE *f = fopen(path, "r");
    long v = -1;
    if (f) {
        if (fscanf(f, "%ld", &v) != 1) {
            v = -1;
        }
        fclose(f);
    }
    return v;
}

int main(void)
{
    const char *dir = temp_dir();
    char p[128], q[256];

    /* backlight: rm69a10 wins over an earlier entry */
    snprintf(p, sizeof(p), "%s/bl", dir);
    mkdir(p, 0755);
    snprintf(q, sizeof(q), "%s/bl/aaa", dir);
    mkdir(q, 0755);
    snprintf(q, sizeof(q), "%s/bl/aaa/brightness", dir);
    put(q, "7\n");
    snprintf(q, sizeof(q), "%s/bl/rm69a10", dir);
    mkdir(q, 0755);
    snprintf(q, sizeof(q), "%s/bl/rm69a10/max_brightness", dir);
    put(q, "255\n");
    snprintf(q, sizeof(q), "%s/bl/rm69a10/brightness", dir);
    put(q, "254\n");
    char bl[400];
    CHECK(board_backlight_dir(p, bl, sizeof(bl)));
    CHECK(strstr(bl, "rm69a10") != NULL);
    CHECK_EQ(board_set_brightness(p, 80), 0);
    CHECK_EQ(get(q), 204);
    CHECK_EQ(board_set_brightness(p, 0), 0); /* screen off */
    CHECK_EQ(get(q), 0);
    CHECK_EQ(board_set_brightness(p, 100), 0);
    CHECK_EQ(get(q), 255);
    snprintf(q, sizeof(q), "%s/none", dir);
    CHECK_EQ(board_set_brightness(q, 50), -1);

    /* touch: the input device named goodix_ts */
    snprintf(p, sizeof(p), "%s/input", dir);
    mkdir(p, 0755);
    const char *names[][2] = {{"event0", "k230-pmu-pwrkey\n"}, {"event1", "goodix_ts\n"}};
    for (int i = 0; i < 2; i++) {
        snprintf(q, sizeof(q), "%s/%s", p, names[i][0]);
        mkdir(q, 0755);
        snprintf(q, sizeof(q), "%s/%s/device", p, names[i][0]);
        mkdir(q, 0755);
        snprintf(q, sizeof(q), "%s/%s/device/name", p, names[i][0]);
        put(q, names[i][1]);
    }
    char ev[64];
    CHECK(board_find_touch(p, ev, sizeof(ev)));
    CHECK(strcmp(ev, "/dev/input/event1") == 0);
    snprintf(q, sizeof(q), "%s/bl", dir);
    CHECK(!board_find_touch(q, ev, sizeof(ev)));

    /* battery: only from the helper's output */
    power_info_t pi;
    CHECK(board_parse_battery("gauge=bq27220 bus=0 soc=76% voltage=3912mV\n"
                              "charger=bq25896 power_good=1 charging=fast\n",
                              &pi));
    CHECK_EQ(pi.battery_pct, 76);
    CHECK(pi.charging && pi.usb);
    CHECK(board_parse_battery("gauge=bq27220 bus=0 soc=100% voltage=4200mV\n"
                              "charger=bq25896 power_good=1 charging=done\n",
                              &pi));
    CHECK(!pi.charging && pi.usb);
    CHECK(!board_parse_battery("gauge=none bus=0\ncharger=none bus=0\n", &pi));
    CHECK_EQ(pi.battery_pct, -1);
    board_read_battery("/nonexistent/athena-board", &pi);
    CHECK_EQ(pi.battery_pct, -1);
    /* a sysfs tree without a battery (this image): unknown */
    snprintf(q, sizeof(q), "%s/input", dir);
    power_read(q, &pi);
    CHECK_EQ(pi.battery_pct, -1);

    /* the launcher hand-over */
    CHECK_EQ(board_start_launcher(dir), 0);
    snprintf(q, sizeof(q), "%s/start-launcher", dir);
    struct stat st;
    CHECK(stat(q, &st) == 0 && st.st_size == 0);
    snprintf(q, sizeof(q), "%s/missing", dir);
    CHECK_EQ(board_start_launcher(q), -1);
    return checks_done("test_board");
}
