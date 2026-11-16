/* Battery and power from Linux's power_supply class (/sys/class/power_supply): the first
 * supply of type Battery gives capacity and Charging/Full; any online USB or Mains supply
 * means USB power. Nothing found: battery unknown (-1). */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int8_t battery_pct; /* -1 unknown */
    bool charging, usb;
} power_info_t;

/* root: normally "/sys/class/power_supply" (tests pass a fake tree). */
void power_read(const char *root, power_info_t *out);

#ifdef __cplusplus
}
#endif
