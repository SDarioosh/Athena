/* The PDA's state (docs/t5-pda-plan.md §2, §4): the current feed, the outbox and what it
 * means for the screens (pending and failed actions), the detail cache, the clock and its
 * display offset. It implements the BLE store (pda_store_t) and posts UI events.
 *
 * Everything here is shared by the UI task, the BLE task and the console: take
 * pda_app_lock() around reads of the feed or the pending lists. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "feed.h"
#include "outbox.h"
#include "pda_actions.h"
#include "pda_proto.h"

#ifdef __cplusplus
extern "C" {
#endif

#define PDA_MOUNT "/lfs"
#define PDA_FAILED_MAX 16
#define PDA_FW_VERSION "0.1.0"

/* UI events (pda_app_post) */
typedef enum {
    PDA_EV_GESTURE,  /* arg: packed gesture (pda_ev_t.g) */
    PDA_EV_BUTTON,   /* arg: button << 1 | long */
    PDA_EV_FEED,     /* a new feed is in */
    PDA_EV_DETAIL,   /* a detail arrived; arg: crc32 of its ref */
    PDA_EV_OUTBOX,   /* the outbox changed (add, ack) */
    PDA_EV_PASSKEY,  /* arg: code, -1 = hide */
    PDA_EV_LINK,     /* arg: 1 up, 0 down */
    PDA_EV_TICK,     /* minute tick */
    PDA_EV_REDRAW,   /* console: redraw the current screen; arg 1 = clean refresh */
    PDA_EV_SCREEN,   /* console: go to screen arg */
    PDA_EV_OPEN,     /* console: open the transcript ref given to ui_pda_open() */
} pda_ev_type_t;

typedef struct {
    pda_ev_type_t type;
    int32_t arg;
    int16_t x, y; /* gestures */
    uint8_t gesture;
} pda_ev_t;

/* One queued action, parsed, for the pending marks. */
typedef struct {
    uint32_t id;
    bool listed; /* already handed to a relay: can't be cancelled any more */
    pda_action_info_t info;
} pda_pending_t;

/* An action the server refused for good (OB_ACK result 1): "couldn't sync" until the next
 * feed. */
typedef struct {
    pda_action_type_t type;
    int64_t item_id;
    char what[PDA_REF_MAX]; /* a title or ref for the toast */
} pda_failed_t;

esp_err_t pda_app_init(void);
const pda_store_t *pda_app_store(void);

void pda_app_lock(void);
void pda_app_unlock(void);

/* Under the lock. NULL feed: none stored yet. */
const feed_t *pda_app_feed(void);
int pda_app_pending(const pda_pending_t **out);
int pda_app_failed(const pda_failed_t **out);
/* The pending item_status for an action item, or NULL. */
const pda_pending_t *pda_app_pending_item(int64_t item_id);
const pda_pending_t *pda_app_pending_fetch(const char *ref);
/* The newest pending todo_toggle for a checklist item (note id, hub index), or NULL. */
const pda_pending_t *pda_app_pending_todo(const char *note, int32_t i);

/* Queue an action from a tap. Returns 0, or -1 outbox full, -2 too long, -3 I/O. */
int pda_app_queue(const char *json, size_t len);
/* Undo a queued action the relay hasn't seen yet. 0, -1 already sent (or gone). */
int pda_app_cancel(uint32_t action_id);
int pda_app_outbox_count(void);
/* Lock-free copies for the BLE status (host task): */
uint16_t pda_app_outbox_waiting(void);
uint32_t pda_app_feed_version(void);
const outbox_t *pda_app_outbox(void); /* under the lock */

/* A cached detail by ref; caller frees with detail_free + free. NULL if not cached. */
detail_t *pda_app_load_detail(const char *ref);

/* Load a blob from the console (same checks as a BLE PUT). 0 or a PDA_E_* code. */
int pda_app_commit_blob(uint8_t kind, uint32_t blob_id, const uint8_t *data, size_t len);

/* Wipe the cached feed, details and outbox (Status screen). */
esp_err_t pda_app_wipe(void);

/* Clock */
bool pda_app_time_valid(void);
int pda_app_tz_min(void);
int64_t pda_app_now(void);
/* The relay's TIME: system clock, RTC, display offset (kept in NVS). */
int pda_app_set_time(int64_t epoch_ms, int16_t tz_min);

/* Screen use: marks the user as active for a minute (status bit4). */
void pda_app_touch_activity(void);
bool pda_app_user_active(void);
int64_t pda_app_last_link_s(void); /* last time a relay was connected, 0 never */
void pda_app_note_link(bool up);   /* ble_pda's on_link hook */

/* UI event queue */
void pda_app_post(const pda_ev_t *ev);
bool pda_app_wait_event(pda_ev_t *ev, uint32_t timeout_ms);

#ifdef __cplusplus
}
#endif
