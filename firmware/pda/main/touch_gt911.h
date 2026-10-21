/* GT911 capacitive touch on the T5 E-Paper Pro (I2C 0x5D, INT GPIO3, RST GPIO9).
 * A task sleeps on the INT line (a level interrupt that also wakes the chip from light
 * sleep), polls the panel every 25 ms while a finger is down, runs gesture.c and calls the
 * callback with each tap, long press or swipe in screen coordinates (540x960, portrait,
 * origin top left, pda_epd's rotation). With no finger down a slow safety-net poll runs in
 * case INT never fires (touch_idle.h). */
#pragma once

#include <stdbool.h>

#include "driver/i2c_master.h"
#include "esp_err.h"
#include "gesture.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef void (*touch_cb_t)(const gesture_t *g, void *ctx);

/* Reset the chip into address 0x5D, read its id and resolution, start the task. Call after
 * pda_epd_init() (epdiy installs the GPIO ISR service). */
esp_err_t touch_init(i2c_master_bus_handle_t bus, touch_cb_t cb, void *ctx);

/* Before deep sleep: stop the task, turn off the INT interrupt and its wake-up, and put the
 * GT911 in its sleep mode. touch_init's reset wakes it after the T5 wakes. */
void touch_sleep(void);

/* Console: log every raw sample for this many ms (for the coordinate mapping check). */
void touch_trace(int ms);
void touch_print(void);

#ifdef __cplusplus
}
#endif
