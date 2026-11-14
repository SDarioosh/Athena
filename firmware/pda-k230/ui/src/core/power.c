#include "power.h"

#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static bool read_line(const char *root, const char *dev, const char *attr, char *out, size_t cap)
{
    char path[512];
    snprintf(path, sizeof(path), "%s/%s/%s", root, dev, attr);
    FILE *f = fopen(path, "r");
    if (!f) {
        return false;
    }
    const bool ok = fgets(out, (int)cap, f) != NULL;
    fclose(f);
    if (ok) {
        out[strcspn(out, "\r\n")] = 0;
    }
    return ok;
}

void power_read(const char *root, power_info_t *out)
{
    *out = (power_info_t){.battery_pct = -1};
    DIR *d = opendir(root);
    if (!d) {
        return;
    }
    bool have_batt = false;
    struct dirent *de;
    while ((de = readdir(d)) != NULL) {
        if (de->d_name[0] == '.') {
            continue;
        }
        char type[32], v[32];
        if (!read_line(root, de->d_name, "type", type, sizeof(type))) {
            continue;
        }
        if (strcmp(type, "Battery") == 0 && !have_batt) {
            if (read_line(root, de->d_name, "capacity", v, sizeof(v))) {
                const long c = strtol(v, NULL, 10);
                out->battery_pct = (int8_t)(c < 0 ? 0 : c > 100 ? 100 : c);
                have_batt = true;
            }
            if (read_line(root, de->d_name, "status", v, sizeof(v))) {
                out->charging = strcmp(v, "Charging") == 0;
            }
        } else if (strcmp(type, "USB") == 0 || strcmp(type, "Mains") == 0) {
            if (read_line(root, de->d_name, "online", v, sizeof(v)) && strcmp(v, "1") == 0) {
                out->usb = true;
            }
        }
    }
    closedir(d);
}
