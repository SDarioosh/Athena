/* T5 E-Paper Pro board layer (docs/t5-pda-plan.md §1, §2): the shared I2C bus, the
 * PCA9535 expander's non-display pins, the PCF8563 RTC, the BQ27220 fuel gauge and
 * BQ25896 charger, and the two buttons. The display is pda_epd.c (epdiy) and the touch
 * panel touch_gt911.c, both on the bus made here. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "driver/i2c_master.h"
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    bool present;   /* the gauge answered with a plausible voltage */
    int millivolts;
    int milliamps;  /* + charging, - discharging */
    int percent;    /* state of charge, -1 if unknown */
    bool charging;  /* charger status: pre-charge or fast charge */
    bool vbus;      /* USB power good */
} t5_battery_t;

typedef enum {
    T5_BUTTON_BOOT, /* GPIO0, next to USB */
    T5_BUTTON_SIDE, /* PCA9535 IO1_2: LilyGO's S3, next to BOOT, labelled IO48 on the board */
} t5_button_t;

typedef enum {
    T5_PRESS_SHORT,
    T5_PRESS_LONG, /* held >= 1 s; reported when the hold reaches 1 s */
} t5_press_t;

/* Called from the button task; post an event and return. */
typedef void (*t5_button_cb_t)(t5_button_t button, t5_press_t press, void *ctx);

/* Bus, backlight off, LoRa/GPS rail off. Call first. Also undoes what deep sleep left on
 * the pins (the BOOT pad as an RTC pin, the backlight pin held low). */
esp_err_t t5_board_init(void);
i2c_master_bus_handle_t t5_board_i2c(void);

/* After epdiy's init (which configures PCA9535 port 1 for itself): make the side-button
 * pin an input again and start the button task. */
esp_err_t t5_board_start_buttons(t5_button_cb_t cb, void *ctx);

/* Which chips answer, for the console. */
void t5_board_log_i2c(void);

esp_err_t t5_battery_read(t5_battery_t *out);

/* UTC epoch seconds. *valid is false if the RTC lost power (PCF8563 VL flag). */
esp_err_t t5_rtc_get(int64_t *epoch, bool *valid);
esp_err_t t5_rtc_set(int64_t epoch);

bool t5_button_is_down(t5_button_t button);

/* Front light (GPIO11, LED driver enable) as PWM: 0 = off .. 100 = full. */
void t5_backlight_set(int percent);

/* Deep sleep until BOOT is pressed (EXT0 on GPIO0, low) or, if timer_us > 0, that many
 * microseconds have passed (a sync wake, pda_sleep.c). Turns the front light off and holds
 * its pin low, waits for BOOT to be released (a held button would wake it at once), then
 * sleeps. Doesn't return: waking is a reset, and app_main starts again. */
void t5_board_deep_sleep(uint64_t timer_us);

typedef enum {
    T5_WAKE_RESET,  /* power on, reset button, flash, crash: not from our sleep */
    T5_WAKE_BUTTON, /* BOOT pressed in deep sleep, or during a timer wake (restart below) */
    T5_WAKE_TIMER,  /* the deep-sleep timer: a sync wake, no UI */
} t5_wake_t;

/* Why this boot happened. Valid from the start of app_main. */
t5_wake_t t5_board_wake_reason(void);
/* The last reset was a wake from t5_board_deep_sleep() (button or timer). */
bool t5_board_woke_from_sleep(void);

/* Timer wake: t5_board_init() leaves the front light's pin held low (no PWM, the light
 * can't come on, t5_backlight_set() does nothing) and the BOOT pad readable. */

/* After epdiy's init without the button task (a sync wake that redraws a line): the
 * side-button pin back to an input, as t5_board_start_buttons() does. */
void t5_board_side_button_input(void);

/* Leave a timer wake for the full UI: wait for BOOT to be released, then restart; the next
 * boot reports T5_WAKE_BUTTON and runs the normal path. Doesn't return. */
void t5_board_restart_awake(void);

#ifdef __cplusplus
}
#endif
