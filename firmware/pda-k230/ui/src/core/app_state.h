/* The PDA's state on Linux (docs/k230-pda-plan.md "Storage", T5 plan §2, §4): the feed, the
 * outbox and what it means for the screens (pending and failed actions), the detail cache,
 * the clock's display offset, the link and sync state from syncd, the vision state and the
 * UI settings (ui.json). The Linux counterpart of the T5's pda_app.c; it implements the
 * pda_store_t that pda_proto.c drives, so glance.sock runs the T5's protocol code unchanged.
 *
 * One thread: the UI loop calls everything here (no locks). Changes are announced to the
 * listeners (app_add_listener) synchronously, after the state is updated. No LVGL here, so
 * the host tests link it alone. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "feed.h"
#include "outbox.h"
#include "pda_actions.h"
#include "pda_proto.h"

#ifdef __cplusplus
extern "C" {
#endif

#define APP_FW_VERSION "0.1.0"
#define APP_DIR_DEFAULT "/var/lib/athena-pda"
#define APP_RUN_DEFAULT "/run/athena-pda"
/* outbox.h keeps its directory in 48 bytes: "<dir>/ob" must fit */
#define APP_DIR_MAX (OUTBOX_DIR_MAX - 4)
#define APP_FAILED_MAX 16
#define APP_ACTIVE_MS 60000 /* status bit4: a touch in the last minute */
#define APP_LISTENERS_MAX 8

typedef enum {
    APP_EV_FEED,        /* a new feed (or none after a wipe) */
    APP_EV_OUTBOX,      /* the outbox changed; arg 1: an action was rejected (APP_EV_FAILED follows) */
    APP_EV_FAILED,      /* the server refused an action for good; arg: index in app_failed() */
    APP_EV_DETAIL,      /* a detail blob was stored; arg: crc32 of its ref */
    APP_EV_CLOCK,       /* TIME changed the display offset (or the clock override moved) */
    APP_EV_LINK,        /* syncd connected or went; arg 1 up, 0 down */
    APP_EV_NET,         /* a LOCAL `net` message */
    APP_EV_TAILNET,     /* a LOCAL `tailnet` message */
    APP_EV_PAIR_RESULT, /* a LOCAL `pair_result`; see app_pair_result() */
    APP_EV_VISION,      /* vision connected, went, or sent `state` / `presence` */
    APP_EV_SETTINGS,    /* ui.json changed */
    APP_EV_POWER,       /* battery or charging changed */
} app_ev_t;

typedef void (*app_listener_t)(void *ctx, app_ev_t ev, int32_t arg);

/* One queued action, parsed, for the pending marks. */
typedef struct {
    uint32_t id;
    bool listed; /* already handed to syncd in an OB_LIST: can't be cancelled any more */
    pda_action_info_t info;
} app_pending_t;

/* An action the server refused for good (OB_ACK result != 0): "couldn't sync" until the
 * next feed. `what` matches like the T5's pda_failed_t (a todo's note id, a cal_add's
 * title, else the ref); `label` is what the toast names (the item's text or title). */
typedef struct {
    pda_action_type_t type;
    int64_t item_id;
    char what[PDA_REF_MAX];
    char label[96];
} app_failed_t;

/* LOCAL `net` (Contract 1) */
typedef enum {
    APP_NET_UNKNOWN = 0, /* syncd hasn't said yet (or isn't connected) */
    APP_NET_UNPAIRED,
    APP_NET_OFFLINE,
    APP_NET_SYNCING,
    APP_NET_OK,
    APP_NET_AUTH_FAILED,
} app_net_state_t;

typedef struct {
    app_net_state_t state;
    char hub[128]; /* "" = null */
    int64_t last_ok; /* epoch s, 0 never */
    char msg[128];
} app_net_t;

/* LOCAL `tailnet` */
typedef enum {
    APP_TAILNET_UNKNOWN = 0,
    APP_TAILNET_STOPPED,
    APP_TAILNET_NEEDS_LOGIN,
    APP_TAILNET_RUNNING,
} app_tailnet_state_t;

typedef struct {
    app_tailnet_state_t state;
    char url[256];  /* the login link while needs_login, else "" */
    char name[128]; /* this device's tailnet name, or "" */
} app_tailnet_t;

/* vision.sock `state` and `presence` (Contract 2) */
typedef enum {
    APP_CAMERA_OFF = 0,
    APP_CAMERA_ON,
    APP_CAMERA_ERROR,
} app_camera_t;

typedef struct {
    bool connected; /* athena-vision is connected */
    app_camera_t camera;
    double fps;
    char msg[96];
    bool present; /* the last `presence` */
} app_vision_t;

/* ui.json */
typedef struct {
    bool hands_free;    /* gestures from the camera drive the UI (plan's gesture table) */
    bool presence_wake; /* wake when someone appears, ambient after they leave */
    int brightness;     /* 5..100 % */
    int dim_after_s;    /* no touch or gesture this long: the ambient screen; 0 never */
    int off_after_s;    /* the ambient screen this long: the screen off; 0 never */
} app_settings_t;

#define APP_DIM_AFTER_DEFAULT 120
#define APP_OFF_AFTER_DEFAULT 900
#define APP_TIMEOUT_MAX (24 * 3600)

/* ---- life cycle ---- */

/* Load ui.json, the feed and the outbox from dir (created if missing). id_seed: the first
 * outbox id of a brand-new outbox. 0, or -1 (dir too long / not creatable), -3 (I/O). */
int app_init(const char *dir, uint32_t id_seed);
void app_deinit(void);
const char *app_dir(void);

void app_add_listener(app_listener_t fn, void *ctx);
void app_remove_listener(app_listener_t fn, void *ctx);

/* ---- feed, outbox, details ---- */

const feed_t *app_feed(void); /* NULL: no feed yet */
int app_pending(const app_pending_t **out);
int app_failed(const app_failed_t **out);
const app_pending_t *app_pending_item(int64_t item_id);
const app_pending_t *app_pending_fetch(const char *ref);
/* The newest pending todo_toggle for a checklist item (note id, hub index `i`), or NULL. */
const app_pending_t *app_pending_todo(const char *note, int32_t i);
const app_failed_t *app_failed_todo(const char *note, int32_t i);
const app_failed_t *app_failed_item(int64_t item_id);

/* Queue an action from a tap: 0, or -1 outbox full, -2 too long, -3 I/O. */
int app_queue(const char *json, size_t len);
/* Undo a queued action syncd hasn't listed yet: 0, -1 already listed (or gone). */
int app_cancel(uint32_t action_id);
int app_outbox_count(void);
const outbox_t *app_outbox(void);

/* A cached detail by ref; free with detail_free() + free(). NULL if not cached. */
detail_t *app_load_detail(const char *ref);

/* Store a complete blob (the checks of a PUT_END): 0 or a PDA_E_* code. */
int app_commit_blob(uint8_t kind, uint32_t blob_id, const uint8_t *data, size_t len);

/* Delete the feed, the details and the outbox (Status screen "Wipe"). */
int app_wipe(void);

const pda_store_t *app_store(void);

/* ---- checklist items (Today and Tasks share these, T5 plan §4.4) ---- */

typedef struct {
    bool done;    /* a queued todo_toggle wins over the feed */
    bool pending; /* a todo_toggle is queued for it */
    bool failed;  /* the relay couldn't apply the last toggle */
} app_todo_view_t;

void app_todo_view(int note_idx, int item_idx, app_todo_view_t *out);

typedef enum {
    APP_TODO_QUEUED,  /* a todo_toggle is queued */
    APP_TODO_UNDONE,  /* the queued toggle was taken back before syncd listed it */
    APP_TODO_REVERSE, /* syncd had it already: the reverse toggle is queued */
    APP_TODO_FULL,    /* outbox full */
    APP_TODO_ERROR,   /* couldn't save, or no such item */
} app_todo_result_t;

/* A tap on a checklist item: toggle it at once; a second tap undoes the pending toggle,
 * or, once syncd has it, queues the reverse (T5 ui_pda.c on_todo). */
app_todo_result_t app_todo_tap(int note_idx, int item_idx);

/* ---- clock ---- */

int64_t app_now(void);       /* epoch s: the system clock (NTP), or the override */
bool app_time_valid(void);   /* the clock is past 2024-01-01 */
int app_tz_min(void);        /* the display offset of the last TIME (kept in ui.json) */
int32_t app_today(void);     /* pda_local_day(app_now(), app_tz_min()) */
/* TIME: the display offset only (the system clock is NTP's, plan Contract 1). */
int app_set_time(int64_t epoch_ms, int16_t tz_min);
/* Tests and the screenshot tool: a fixed clock (epoch s), 0 = the real one. */
void app_set_clock_override(int64_t epoch_s);
/* Monotonic ms (for activity); app_set_mono_override(0) = the real one. */
int64_t app_mono_ms(void);
void app_set_mono_override(int64_t ms);

/* ---- status (glance §3.2) ---- */

void app_touch_activity(void);
bool app_user_active(void);
int64_t app_last_activity_ms(void); /* app_mono_ms() of the last touch (or gesture) */
/* Battery % (-1 unknown) and power flags, from the board when known. */
void app_set_power(int8_t battery_pct, bool charging, bool usb);
int8_t app_battery_pct(void);
bool app_charging(void);
void app_status(pda_status_t *out);
uint32_t app_boot_id(void);

/* ---- syncd (glance.sock) ---- */

void app_set_link(bool up);
bool app_link_up(void);
int64_t app_last_link_s(void); /* last time syncd was connected (epoch s), 0 never */
const app_net_t *app_net(void);
void app_set_net(const app_net_t *n);
const app_tailnet_t *app_tailnet(void);
void app_set_tailnet(const app_tailnet_t *t);
/* The last pair_result: ok and msg. */
void app_set_pair_result(bool ok, const char *msg);
bool app_pair_result(const char **msg);

/* ---- vision (vision.sock) ---- */

const app_vision_t *app_vision(void);
void app_set_vision(const app_vision_t *v);

/* What vision should run (the `mode` message): gestures while Hands-free is on, presence
 * while presence wake is on, qr while app_set_qr_scan(true) (the pairing screen). While the
 * screen is fully off (app_set_screen_off(true), the ambient screen's last stage) only
 * presence wake keeps the camera on: then presence, and gestures if Hands-free is on too;
 * without presence wake all three are false and the camera stops. */
void app_set_qr_scan(bool on);
void app_set_screen_off(bool off);
void app_vision_mode(bool *gestures, bool *presence, bool *qr);

/* ---- settings (ui.json) ---- */

const app_settings_t *app_settings(void);
/* Save and announce (APP_EV_SETTINGS). 0 or -3. */
int app_set_settings(const app_settings_t *s);

#ifdef __cplusplus
}
#endif
