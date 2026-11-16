#include "board.h"

#include <dirent.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static long read_long(const char *dir, const char *attr)
{
    char p[512];
    snprintf(p, sizeof(p), "%s/%s", dir, attr);
    FILE *f = fopen(p, "r");
    long v = -1;
    if (f) {
        if (fscanf(f, "%ld", &v) != 1) {
            v = -1;
        }
        fclose(f);
    }
    return v;
}

bool board_backlight_dir(const char *root, char *out, size_t cap)
{
    char p[512];
    snprintf(p, sizeof(p), "%s/rm69a10/brightness", root);
    if (access(p, F_OK) == 0) {
        snprintf(out, cap, "%s/rm69a10", root);
        return true;
    }
    DIR *d = opendir(root);
    if (!d) {
        return false;
    }
    bool found = false;
    struct dirent *e;
    while (!found && (e = readdir(d)) != NULL) {
        if (e->d_name[0] == '.') {
            continue;
        }
        snprintf(p, sizeof(p), "%s/%s/brightness", root, e->d_name);
        if (access(p, F_OK) == 0) {
            snprintf(out, cap, "%s/%s", root, e->d_name);
            found = true;
        }
    }
    closedir(d);
    return found;
}

int board_set_brightness(const char *root, int percent)
{
    char dir[400];
    if (!board_backlight_dir(root, dir, sizeof(dir))) {
        return -1;
    }
    long max = read_long(dir, "max_brightness");
    if (max <= 0) {
        max = 255;
    }
    percent = percent < 0 ? 0 : percent > 100 ? 100 : percent;
    long v = (max * percent + 50) / 100;
    if (percent > 0 && v < 1) {
        v = 1;
    }
    char p[512];
    snprintf(p, sizeof(p), "%s/brightness", dir);
    FILE *f = fopen(p, "w");
    if (!f) {
        return -1;
    }
    fprintf(f, "%ld\n", v);
    return fclose(f) == 0 ? 0 : -1;
}

bool board_find_touch(const char *root, char *out, size_t cap)
{
    DIR *d = opendir(root);
    if (!d) {
        return false;
    }
    bool found = false;
    struct dirent *e;
    while (!found && (e = readdir(d)) != NULL) {
        if (strncmp(e->d_name, "event", 5) != 0) {
            continue;
        }
        char p[512], name[128] = "";
        snprintf(p, sizeof(p), "%s/%s/device/name", root, e->d_name);
        FILE *f = fopen(p, "r");
        if (!f) {
            continue;
        }
        if (fgets(name, sizeof(name), f)) {
            for (char *c = name; *c; c++) {
                *c = (char)(*c >= 'A' && *c <= 'Z' ? *c + 32 : *c);
            }
            if (strstr(name, "goodix") || strstr(name, "gt9895")) {
                snprintf(out, cap, "/dev/input/%s", e->d_name);
                found = true;
            }
        }
        fclose(f);
    }
    closedir(d);
    return found;
}

bool board_parse_battery(const char *text, power_info_t *out)
{
    *out = (power_info_t){.battery_pct = -1};
    const char *g = strstr(text, "gauge=bq27220");
    const char *soc = g ? strstr(g, "soc=") : NULL;
    if (!soc) {
        return false;
    }
    char *end;
    const long v = strtol(soc + 4, &end, 10);
    if (end == soc + 4 || v < 0 || v > 100) {
        return false;
    }
    out->battery_pct = (int8_t)v;
    const char *c = strstr(text, "charger=bq25896");
    if (c) {
        out->usb = strstr(c, "power_good=1") != NULL;
        const char *ch = strstr(c, "charging=");
        out->charging = ch && (strncmp(ch + 9, "pre", 3) == 0 || strncmp(ch + 9, "fast", 4) == 0);
    }
    return true;
}

void board_read_battery(const char *helper, power_info_t *out)
{
    *out = (power_info_t){.battery_pct = -1};
    if (access(helper, X_OK) != 0) {
        return;
    }
    char cmd[300];
    snprintf(cmd, sizeof(cmd), "%s battery 2>/dev/null", helper);
    FILE *p = popen(cmd, "r");
    if (!p) {
        return;
    }
    char buf[512];
    const size_t n = fread(buf, 1, sizeof(buf) - 1, p);
    buf[n] = 0;
    pclose(p);
    board_parse_battery(buf, out);
}

int board_start_launcher(const char *run_dir)
{
    char p[512];
    snprintf(p, sizeof(p), "%s/start-launcher", run_dir);
    const int fd = open(p, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0) {
        return -1;
    }
    return close(fd) == 0 ? 0 : -1;
}
