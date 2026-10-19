/* Application events, posted from button callbacks, the console and the recorder. */
#pragma once

#include <stdbool.h>

typedef enum {
    APP_EV_PWR_SHORT,
    APP_EV_PWR_LONG,
    APP_EV_BOOT_SHORT,
    APP_EV_BOOT_LONG,
    APP_EV_SCREEN_TIMEOUT,
    APP_EV_SCREEN_SHOW,   /* console: screen */
    APP_EV_MUTE_TOGGLE,   /* console: mute */
    APP_EV_RECORD,        /* console: rec <seconds>; arg = seconds */
    APP_EV_REC_STOP,      /* console: stop */
    APP_EV_SYNC_NOW,      /* console: sync (placeholder) */
    APP_EV_REC_TICK,      /* recorder progress; arg = elapsed seconds */
    APP_EV_REC_DONE,      /* recorder finished; arg = esp_err_t */
} app_event_type_t;

void app_post(app_event_type_t type, int arg);
bool app_is_muted(void);
/* Print the status summary (console `status`). */
void app_print_status(void);
