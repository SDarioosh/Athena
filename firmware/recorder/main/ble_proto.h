/* BLE chunk-transfer protocol v1 (docs/twatch-ble-protocol.md): byte layouts, command
 * parsing, the one-command-at-a-time gate, the command handler and the advertising rule.
 * Pure C; host-tested (test/test_ble_proto.c). ble_sync.c is the NimBLE glue around it.
 *
 * The handler never holds a whole chunk in RAM: it reads one DATA frame's payload at a time
 * from the store into a frame buffer and hands each frame to a send callback. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define BLE_PROTO_VERSION 1
#define BLE_PROTO_MIN_MTU 64
#define BLE_PROTO_MAX_MTU 517
/* Largest notification value: ATT MTU minus the 3-byte notification header. */
#define BLE_PROTO_FRAME_MAX (BLE_PROTO_MAX_MTU - 3)
#define BLE_PROTO_LIST_MAX 32
/* The handler asks the store for at most this many entries per list() call. */
#define BLE_PROTO_LIST_BATCH 8
#define BLE_PROTO_CMD_MAX 20

/* Commands (relay -> ctrl) */
#define BLE_OP_LIST 0x01
#define BLE_OP_READ 0x02
#define BLE_OP_ACK 0x03
#define BLE_OP_TIME 0x04
#define BLE_OP_BYE 0x05
/* Responses (data notifications) */
#define BLE_OP_ENTRY 0x81
#define BLE_OP_DATA 0x82
#define BLE_OP_READ_END 0x83
#define BLE_OP_ACKED 0x84
#define BLE_OP_LIST_END 0x85
#define BLE_OP_TIME_OK 0x86
#define BLE_OP_BYE_OK 0x87
#define BLE_OP_ERROR 0xEE

/* Frame lengths */
#define BLE_PROTO_STATUS_LEN 24
#define BLE_PROTO_ENTRY_LEN 40
#define BLE_PROTO_DATA_HDR_LEN 9
#define BLE_PROTO_READ_END_LEN 16
#define BLE_PROTO_ACKED_LEN 8
#define BLE_PROTO_LIST_END_LEN 8
#define BLE_PROTO_TIME_OK_LEN 4
#define BLE_PROTO_BYE_OK_LEN 1
#define BLE_PROTO_ERROR_LEN 8

/* Error codes (§5.6). 0 means success wherever these are returned. */
#define BLE_PROTO_E_BAD_LENGTH 1
#define BLE_PROTO_E_UNKNOWN_CMD 2
#define BLE_PROTO_E_NOT_FOUND 3
#define BLE_PROTO_E_BUSY 4
#define BLE_PROTO_E_MTU_TOO_SMALL 5
#define BLE_PROTO_E_IO 6
#define BLE_PROTO_E_BAD_TIME 7

/* TIME bounds (§5.5): [2024-01-01T00:00:00Z, 2100-01-01T00:00:00Z) in epoch ms. */
#define BLE_PROTO_TIME_MIN_MS 1704067200000LL
#define BLE_PROTO_TIME_END_MS 4102444800000LL

/* Status flag bits (§4.2) */
#define BLE_STATUS_RECORDING 0x01
#define BLE_STATUS_MUTED 0x02
#define BLE_STATUS_TIME_VALID 0x04
#define BLE_STATUS_CHARGING 0x08
#define BLE_STATUS_USB 0x10
#define BLE_STATUS_SD_OK 0x20
#define BLE_STATUS_WINDOW 0x40

/* Entry flag bits (§4.3), the same values as CHUNK_FLAG_* in chunk_store.h. */
#define BLE_ENTRY_LAST 0x01
#define BLE_ENTRY_TIME_UNSET 0x02

typedef struct {
    uint8_t flags;           /* BLE_STATUS_* */
    int8_t battery_pct;      /* -1 if unknown */
    uint16_t battery_mv;     /* 0 if unknown */
    uint16_t queued_chunks;
    uint32_t queued_bytes;
    uint32_t queued_ms;     /* audio queued since the last BYE */
    uint32_t clock_s;        /* UTC epoch seconds, 0 if not valid */
    uint32_t boot_id;
} ble_proto_status_t;

typedef struct {
    uint32_t chunk_id;
    uint8_t flags; /* BLE_ENTRY_* */
    uint8_t session_id[8];
    uint32_t sequence;
    uint64_t start_ms;
    uint32_t duration_ms;
    uint32_t bytes;
    uint32_t crc32;
} ble_proto_entry_t;

typedef struct {
    uint8_t op;          /* BLE_OP_LIST .. BLE_OP_BYE */
    uint32_t after_id;   /* LIST */
    uint16_t max;        /* LIST, capped at BLE_PROTO_LIST_MAX */
    uint32_t chunk_id;   /* READ, ACK */
    uint32_t offset;     /* READ */
    uint32_t length;     /* READ, 0 = to the end */
    int64_t epoch_ms;    /* TIME */
} ble_proto_cmd_t;

/* ---- encoders: each writes exactly its *_LEN bytes and returns that length ---- */
size_t ble_proto_encode_status(const ble_proto_status_t *s, uint8_t out[BLE_PROTO_STATUS_LEN]);
size_t ble_proto_encode_entry(const ble_proto_entry_t *e, uint8_t out[BLE_PROTO_ENTRY_LEN]);
size_t ble_proto_encode_data_header(uint32_t chunk_id, uint32_t offset,
                                    uint8_t out[BLE_PROTO_DATA_HDR_LEN]);
size_t ble_proto_encode_read_end(uint8_t status, uint32_t chunk_id, uint32_t total_bytes,
                                 uint32_t crc32, uint8_t out[BLE_PROTO_READ_END_LEN]);
size_t ble_proto_encode_acked(uint8_t result, uint32_t chunk_id, uint8_t out[BLE_PROTO_ACKED_LEN]);
size_t ble_proto_encode_list_end(uint16_t count, uint32_t remaining,
                                 uint8_t out[BLE_PROTO_LIST_END_LEN]);
size_t ble_proto_encode_time_ok(uint16_t fixed, uint8_t out[BLE_PROTO_TIME_OK_LEN]);
size_t ble_proto_encode_bye_ok(uint8_t out[BLE_PROTO_BYE_OK_LEN]);
size_t ble_proto_encode_error(uint8_t cmd, uint8_t code, uint32_t arg,
                              uint8_t out[BLE_PROTO_ERROR_LEN]);

/* Parse one ctrl write. Returns 0 and fills *out, or an error code (BAD_LENGTH,
 * UNKNOWN_CMD, BAD_TIME). Every command has an exact length; LIST's max is capped at 32. */
int ble_proto_parse_cmd(const uint8_t *buf, size_t len, ble_proto_cmd_t *out);

/* ---- one command at a time (§5) ---- */
typedef struct {
    bool busy;
} ble_proto_gate_t;

/* Decide on a ctrl write, in this order: busy, MTU < 64, then parsing. Returns true if the
 * command was accepted: *cmd is filled and the gate stays busy until ble_proto_gate_done().
 * Returns false with the ERROR frame to send in err_frame. The caller serialises calls. */
bool ble_proto_gate_submit(ble_proto_gate_t *gate, const uint8_t *buf, size_t len, uint16_t mtu,
                           ble_proto_cmd_t *cmd, uint8_t err_frame[BLE_PROTO_ERROR_LEN]);
void ble_proto_gate_done(ble_proto_gate_t *gate);

/* ---- command handler ---- */

/* The queue as the handler sees it (chunk_store.h, adapted). Return codes are 0 or a
 * BLE_PROTO_E_* code. */
typedef struct {
    /* Up to max (<= BLE_PROTO_LIST_BATCH) entries with chunk_id > after_id, ascending;
     * *remaining = listable chunks after the last one returned (after after_id when none
     * are). */
    size_t (*list)(void *ctx, uint32_t after_id, ble_proto_entry_t *out, size_t max,
                   uint32_t *remaining);
    bool (*get)(void *ctx, uint32_t chunk_id, ble_proto_entry_t *out);
    /* Up to len bytes at offset; *got = 0 at the end of the file. */
    int (*read)(void *ctx, uint32_t chunk_id, uint32_t offset, uint8_t *buf, size_t len,
                size_t *got);
    /* *was_queued = false if the chunk was already gone (still 0). */
    int (*ack)(void *ctx, uint32_t chunk_id, bool *was_queued);
    /* Set the clock and convert TIME_UNSET chunks; *fixed = how many. */
    int (*set_time)(void *ctx, int64_t epoch_ms, uint16_t *fixed);
} ble_proto_store_t;

/* Send one data notification. Returns 0, or nonzero if the link is gone (the handler then
 * stops without sending anything else). */
typedef int (*ble_proto_send_t)(void *ctx, const uint8_t *frame, size_t len);

typedef enum {
    BLE_PROTO_DONE,      /* final response sent */
    BLE_PROTO_BYE,       /* BYE_OK sent: the caller disconnects after ~1 s */
    BLE_PROTO_LINK_LOST, /* a send failed */
} ble_proto_result_t;

/* Run an accepted command. mtu is the current ATT MTU (>= 64; clamped to 517); frame_buf
 * must hold BLE_PROTO_FRAME_MAX bytes. */
ble_proto_result_t ble_proto_execute(const ble_proto_cmd_t *cmd, uint16_t mtu,
                                     const ble_proto_store_t *store, void *store_ctx,
                                     ble_proto_send_t send, void *send_ctx, uint8_t *frame_buf);

/* DATA payload bytes per frame at this MTU. */
size_t ble_proto_data_payload_max(uint16_t mtu);

/* ---- advertising rule (§7.1) ---- */
typedef enum {
    BLE_ADV_NONE,
    BLE_ADV_WINDOW, /* sync window open */
    BLE_ADV_LAST,   /* a finished session is queued */
    BLE_ADV_AUDIO,  /* >= 10 min of audio queued */
    BLE_ADV_STALE,  /* chunks queued, last BYE > 60 min ago (or never) */
} ble_adv_reason_t;

#define BLE_ADV_AUDIO_MS (10u * 60u * 1000u)
#define BLE_ADV_STALE_MS (60LL * 60 * 1000)

typedef struct {
    bool connected;
    bool window_open;
    bool has_last;          /* a LAST chunk queued since the last BYE */
    uint32_t queued_chunks;
    uint32_t queued_ms;     /* audio queued since the last BYE */
    int64_t ms_since_sync; /* < 0: no successful sync since boot */
} ble_adv_inputs_t;

ble_adv_reason_t ble_proto_adv_reason(const ble_adv_inputs_t *in);
const char *ble_proto_adv_reason_name(ble_adv_reason_t r);

#ifdef __cplusplus
}
#endif
