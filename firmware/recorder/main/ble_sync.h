/* BLE sync: the NimBLE GATT server of the chunk-transfer protocol
 * (docs/twatch-ble-protocol.md). The watch is the peripheral; the relay (phone or PC)
 * connects, pulls closed chunks from chunk_store and acks them.
 *
 * Radio policy (§7): no advertising unless there is something to do (sync window, a
 * finished session, >= 10 min of audio, or chunks queued with no sync for 60 min); one
 * relay at a time; short bursts; the relay is dropped after 30 s without a command.
 * Pairing (§3.1): passkey shown on the watch, LE Secure Connections, bonding; new bonds
 * only in the 2-min sync window. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* App state for the status characteristic. */
typedef struct {
    bool recording;  /* a session is recording (not muted) */
    bool muted;
    bool time_valid; /* the system clock holds wall-clock time */
} ble_sync_app_state_t;

typedef struct {
    /* `fw` in the info characteristic (§4.1), e.g. esp_app_get_description()->version.
     * NULL reads as "unknown". The string must outlive ble_sync. */
    const char *fw_version;
    const char *reset_reason; /* `reset=` in info (protocol §4.1); NULL leaves it out */
    /* Fill the app state. Called from the NimBLE host task and the BLE task: copy flags
     * from RAM, never block. */
    void (*get_state)(ble_sync_app_state_t *out);
    /* The relay's TIME (UTC epoch ms, already checked to be in 2024..2100): set the system
     * clock and the RTC. Called from the BLE task; may take a few ms of I2C. ble_sync then
     * converts TIME_UNSET chunks itself (chunk_store_fix_time). */
    void (*on_time)(int64_t epoch_ms);
    /* Pairing (§3.1): show this 6-digit code on the screen until called with -1 (paired,
     * failed or disconnected). Called from the NimBLE host task: post an event, don't draw. */
    void (*on_passkey)(int32_t passkey);
} ble_sync_hooks_t;

/* Start the controller and NimBLE host, register the service, start the BLE task.
 * Call once after nvs_flash_init() (bonds live in NVS), board_init() and
 * chunk_store_init(). Advertising starts only when a rule in §7.1 holds. */
esp_err_t ble_sync_init(const ble_sync_hooks_t *hooks);

/* Open the 2-minute sync window: advertise now and accept new bonds (BOOT long press,
 * console `ble pair` / `sync`). Safe from any task. */
void ble_sync_open_window(void);
bool ble_sync_window_is_open(void);
/* A relay is connected. Safe from any task. */
bool ble_sync_is_connected(void);
/* The radio is advertising (power log). */
bool ble_sync_is_advertising(void);

/* Something changed (chunk added/acked, recording or mute toggled, card in/out):
 * re-evaluate advertising and notify status if a relay is subscribed (at most 1/s).
 * Cheap and safe from any task, including the chunk_store listener. */
void ble_sync_kick(void);

/* Console `ble`: state, peer, MTU, PHY, bonds, advertising reason, last sync, counters. */
void ble_sync_print(void);

/* Console `ble forget`: delete every bond (the current link, if any, stays up until it
 * disconnects). */
esp_err_t ble_sync_forget_bonds(void);

#ifdef __cplusplus
}
#endif
