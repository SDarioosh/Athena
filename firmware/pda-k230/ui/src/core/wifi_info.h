/* Wi-Fi as the Status screen shows it, read-only and without root: whether wlan0 is up and
 * associated, its signal (/proc/net/wireless) and its IPv4 address. The network itself is
 * chosen with `athena-wifi` over adb (system/; root only, the UI never changes it), and
 * its name isn't readable without root (wpa_supplicant's control socket), so it isn't
 * shown. Host-tested (test/test_ambient.c). */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define WIFI_IF "wlan0"

typedef struct {
    bool present;   /* the interface exists */
    bool up;        /* operstate "up" (associated, the driver's word) */
    bool have_level;
    int level_dbm;  /* signal level from /proc/net/wireless */
    char ip[48];    /* "" if none */
} wifi_info_t;

/* /proc/net/wireless's text: the signal level (dBm) of ifname. false if the line isn't
 * there. Levels the driver gives as 0..255 ("-56." or "200") are read as dBm. */
bool wifi_parse_wireless(const char *text, const char *ifname, int *level_dbm);

/* "Strong", "Good", "Fair", "Weak" */
const char *wifi_signal_word(int level_dbm);

/* proc: normally "/proc/net/wireless"; sys: "/sys/class/net" (tests pass fakes). */
void wifi_read(const char *proc, const char *sys, wifi_info_t *out);

#ifdef __cplusplus
}
#endif
