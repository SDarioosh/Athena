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
    APP_EV_SYNC_NOW,      /* console: sync / ble pair: open the BLE sync window */
    APP_EV_REC_TICK,      /* recorder progress; arg = elapsed seconds */
    APP_EV_REC_DONE,      /* recorder finished; arg = esp_err_t */
    APP_EV_SESSION_START, /* console: session start */
    APP_EV_SESSION_STOP,  /* console: session stop */
    APP_EV_SESSION_TICK,  /* session progress; arg = elapsed seconds */
    APP_EV_SESSION_DONE,  /* session ended; arg = session_rec_end_t */
    APP_EV_QUEUE_CHANGED, /* chunk added, acked or changed */
    APP_EV_PASSKEY,       /* BLE pairing code to show; arg = 0..999999, or -1 to hide */
} app_event_type_t;

void app_post(app_event_type_t type, int arg);
bool app_is_muted(void);
/* Print the status summary (console `status`). */
void app_print_status(void);
