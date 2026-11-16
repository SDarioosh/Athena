/* The T-Display K230's board nodes (firmware/pda-k230/system/BOARD.md): the backlight
 * (/sys/class/backlight/rm69a10, max 255; screen off = brightness 0), the touch screen
 * (the evdev device named goodix_ts), the battery (no sysfs on this image: only the
 * `athena-board battery` helper, with LilyGO's keyboard base) and the hand-over to
 * LilyGO's launcher. Paths are parameters so the host tests use fake trees
 * (test/test_board.c). */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "power.h"

#ifdef __cplusplus
extern "C" {
#endif

#define BOARD_BACKLIGHT_ROOT "/sys/class/backlight"
#define BOARD_INPUT_ROOT "/sys/class/input"
#define BOARD_HELPER "/usr/bin/athena-board"

/* root/rm69a10 if it is there, else the first backlight with a brightness file. */
bool board_backlight_dir(const char *root, char *out, size_t cap);
/* percent 0 = off (the panel's way to blank), 1..100 scaled to max_brightness. 0 or -1. */
int board_set_brightness(const char *root, int percent);

/* /dev/input/eventN of the touch screen: the input device whose name has goodix or
 * gt9895 (BOARD.md "Touch"), found under root (/sys/class/input). */
bool board_find_touch(const char *root, char *out, size_t cap);

/* The output of `athena-board battery`: "gauge=bq27220 ... soc=76% ..." and
 * "charger=bq25896 power_good=1 charging=fast". false (battery unknown) without a gauge. */
bool board_parse_battery(const char *text, power_info_t *out);
/* Run the helper (if installed); out->battery_pct -1 when it gives nothing. */
void board_read_battery(const char *helper, power_info_t *out);

/* "Start LilyGO's launcher": create the empty file <run_dir>/start-launcher; the program
 * then exits 0 and the supervisor (S92athena-pda) does the rest. 0 or -1. */
int board_start_launcher(const char *run_dir);

#ifdef __cplusplus
}
#endif
