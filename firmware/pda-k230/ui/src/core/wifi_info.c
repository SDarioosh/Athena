#include "wifi_info.h"

#include <arpa/inet.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

bool wifi_parse_wireless(const char *text, const char *ifname, int *level_dbm)
{
    const size_t n = strlen(ifname);
    for (const char *line = text; line && *line;) {
        const char *s = line;
        while (*s == ' ' || *s == '\t') {
            s++;
        }
        if (strncmp(s, ifname, n) == 0 && s[n] == ':') {
            /* "wlan0: 0000   54.  -56.  -256 ..." : status, link, level, noise */
            char *end;
            const char *p = s + n + 1;
            strtol(p, &end, 16); /* status */
            if (end == p) {
                return false;
            }
            p = end;
            strtod(p, &end); /* link quality */
            if (end == p) {
                return false;
            }
            p = end;
            const double level = strtod(p, &end);
            if (end == p) {
                return false;
            }
            int v = (int)level;
            if (v > 0) {
                v -= 256; /* an unsigned 8-bit dBm */
            }
            *level_dbm = v;
            return true;
        }
        line = strchr(line, '\n');
        if (line) {
            line++;
        }
    }
    return false;
}

const char *wifi_signal_word(int level_dbm)
{
    return level_dbm >= -60 ? "Strong" : level_dbm >= -70 ? "Good" : level_dbm >= -80 ? "Fair" : "Weak";
}

static bool read_small(const char *path, char *buf, size_t cap)
{
    FILE *f = fopen(path, "rb");
    if (!f) {
        return false;
    }
    const size_t n = fread(buf, 1, cap - 1, f);
    fclose(f);
    buf[n] = 0;
    return true;
}

void wifi_read(const char *proc, const char *sys, wifi_info_t *out)
{
    *out = (wifi_info_t){0};
    char path[256], buf[2048];
    snprintf(path, sizeof(path), "%s/%s/operstate", sys, WIFI_IF);
    if (read_small(path, buf, sizeof(buf))) {
        out->present = true;
        out->up = strncmp(buf, "up", 2) == 0;
    }
    if (read_small(proc, buf, sizeof(buf)) && wifi_parse_wireless(buf, WIFI_IF, &out->level_dbm)) {
        out->have_level = true;
        out->present = true;
    }
    struct ifaddrs *ifa = NULL;
    if (out->present && getifaddrs(&ifa) == 0) {
        for (struct ifaddrs *i = ifa; i; i = i->ifa_next) {
            if (i->ifa_addr && i->ifa_addr->sa_family == AF_INET && strcmp(i->ifa_name, WIFI_IF) == 0) {
                const struct sockaddr_in *sa = (const struct sockaddr_in *)i->ifa_addr;
                inet_ntop(AF_INET, &sa->sin_addr, out->ip, sizeof(out->ip));
                break;
            }
        }
        freeifaddrs(ifa);
    }
}
