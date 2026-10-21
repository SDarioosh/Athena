/* BLE: the NimBLE GATT server of the glance protocol (docs/t5-pda-plan.md §3). The T5 is
 * the peripheral; the phone (android-relay PdaSync) or the PC relay (`ble_relay.py pda`)
 * connects, sends TIME, drains the outbox and PUTs the feed and details.
 *
 * Pairing and bonding as the recorder's ble_sync (watch protocol §3.1): passkey shown on
 * the T5, LE Secure Connections, authenticated keys only, new bonds only in the 2-minute
 * pairing window. The protocol itself is pda_proto.c. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "pda_proto.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    bool time_valid;
    bool user_active; /* the screen was used in the last minute */
    int8_t battery_pct;
    bool charging, usb;
    uint16_t outbox;
    uint32_t feed_version;
} ble_pda_state_t;

typedef struct {
    const char *fw_version; /* `fw` in info; must outlive ble_pda */
    /* Copy app state from RAM; called from the NimBLE host and BLE tasks, never block. */
    void (*get_state)(ble_pda_state_t *out);
    /* Pairing code to show until called with -1. Host task: post an event, don't draw. */
    void (*on_passkey)(int32_t passkey);
    /* A relay connected (true) or the link went down (false). Host task. */
    void (*on_link)(bool up);
} ble_pda_hooks_t;

/* Start NimBLE and the BLE task. The store callbacks run in the BLE task. Call after
 * nvs_flash_init(). */
esp_err_t ble_pda_init(const pda_store_t *store, void *store_ctx, const ble_pda_hooks_t *hooks);

void ble_pda_open_window(void);
bool ble_pda_window_is_open(void);
int ble_pda_window_left_s(void);
bool ble_pda_is_connected(void);
/* Unix seconds of the last BYE, 0 if none since boot. */
int64_t ble_pda_last_sync(void);

/* State changed (outbox, feed, user activity): notify status, refresh the scan response. */
void ble_pda_kick(void);

/* Advertise at the pairing window's fast interval without opening the window (bonded
 * peers only): a sync wake from deep sleep, so the phone's low-power scan hears us within
 * one scan window. Callable before ble_pda_init(). */
void ble_pda_set_fast_adv(bool fast);

void ble_pda_print(void);
esp_err_t ble_pda_forget_bonds(void);
int ble_pda_bond_count(void);

#ifdef __cplusplus
}
#endif
