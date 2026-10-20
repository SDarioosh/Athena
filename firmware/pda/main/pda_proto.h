/* BLE "glance" protocol v1 (docs/t5-pda-plan.md §3): byte layouts, command parsing, the
 * one-command-at-a-time gate, the PUT state machine and the command handler. Pure C;
 * host-tested (test/test_pda_proto.c). ble_pda.c is the NimBLE glue around it, in the
 * style of the recorder's ble_proto.c / ble_sync.c (docs/twatch-ble-protocol.md §5).
 *
 * A PUT is buffered in RAM (PSRAM on the T5; blobs are <= 256 KB) and handed to the store
 * only after PUT_END has checked the size and CRC-32, so a disconnect or a bad CRC never
 * touches the stored feed. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PDA_PROTO_VERSION 1
#define PDA_PROTO_MIN_MTU 420
#define PDA_PROTO_MAX_MTU 517
/* Largest notification / write value: ATT MTU minus the 3-byte ATT header. */
#define PDA_PROTO_FRAME_MAX (PDA_PROTO_MAX_MTU - 3)
#define PDA_PROTO_BLOB_MAX (256u * 1024u)
#define PDA_PROTO_OB_JSON_MAX 400
#define PDA_PROTO_OB_LIST_MAX 16
/* The handler asks the store for at most this many outbox entries per call. */
#define PDA_PROTO_OB_BATCH 2

/* Blob kinds (§3.1) */
#define PDA_KIND_FEED 1
#define PDA_KIND_DETAIL 2

/* Commands (phone -> ctrl) */
#define PDA_OP_TIME 0x04
#define PDA_OP_BYE 0x05
#define PDA_OP_PUT_BEGIN 0x10
#define PDA_OP_PUT_DATA 0x11
#define PDA_OP_PUT_END 0x12
#define PDA_OP_OB_LIST 0x20
#define PDA_OP_OB_ACK 0x21
/* Responses (data notifications) */
#define PDA_OP_TIME_OK 0x86
#define PDA_OP_BYE_OK 0x87
#define PDA_OP_PUT_READY 0x90
#define PDA_OP_PUT_OK 0x91
#define PDA_OP_OB_ENTRY 0xA0
#define PDA_OP_OB_END 0xA1
#define PDA_OP_OB_ACKED 0xA2
#define PDA_OP_ERROR 0xEE

/* Lengths */
#define PDA_PROTO_STATUS_LEN 16
#define PDA_PROTO_TIME_OK_LEN 4
#define PDA_PROTO_BYE_OK_LEN 1
#define PDA_PROTO_PUT_READY_LEN 8
#define PDA_PROTO_PUT_OK_LEN 8
#define PDA_PROTO_OB_ENTRY_HDR_LEN 16
#define PDA_PROTO_OB_END_LEN 8
#define PDA_PROTO_OB_ACKED_LEN 8
#define PDA_PROTO_ERROR_LEN 8
#define PDA_PROTO_PUT_DATA_HDR_LEN 5

/* Error codes: watch protocol §5.6 plus §3.3's 8..10. */
#define PDA_E_BAD_LENGTH 1
#define PDA_E_UNKNOWN_CMD 2
#define PDA_E_NOT_FOUND 3
#define PDA_E_BUSY 4
#define PDA_E_MTU_TOO_SMALL 5
#define PDA_E_IO 6
#define PDA_E_BAD_TIME 7
#define PDA_E_BAD_CRC 8
#define PDA_E_TOO_LARGE 9
#define PDA_E_BAD_STATE 10

/* TIME bounds: [2024-01-01, 2100-01-01) UTC in epoch ms; offset within +-14 h. */
#define PDA_TIME_MIN_MS 1704067200000LL
#define PDA_TIME_END_MS 4102444800000LL
#define PDA_TZ_MAX_MIN (14 * 60)

/* status flags (§3.2) */
#define PDA_STATUS_TIME_VALID 0x01
#define PDA_STATUS_CHARGING 0x02
#define PDA_STATUS_USB 0x04
#define PDA_STATUS_WINDOW 0x08
#define PDA_STATUS_ACTIVE 0x10

/* Scan response (plan §3.5): 128-bit Service Data for the service UUID with one flags
 * byte, sent while the outbox has entries. */
#define PDA_ADV_OUTBOX 0x01

typedef struct {
    uint8_t flags;       /* PDA_STATUS_* */
    int8_t battery_pct;  /* -1 if unknown */
    uint16_t outbox;     /* entries waiting */
    uint32_t feed_version;
    uint32_t boot_id;
} pda_status_t;

typedef struct {
    uint8_t op;
    int64_t epoch_ms;     /* TIME */
    int16_t tz_min;       /* TIME */
    uint8_t kind;         /* PUT_BEGIN */
    uint32_t blob_id;     /* PUT_BEGIN */
    uint32_t total;       /* PUT_BEGIN */
    uint32_t crc32;       /* PUT_BEGIN */
    uint32_t offset;      /* PUT_DATA */
    const uint8_t *data;  /* PUT_DATA payload: points into the parsed buffer */
    size_t data_len;      /* PUT_DATA */
    uint32_t after_id;    /* OB_LIST */
    uint16_t max;         /* OB_LIST, capped at PDA_PROTO_OB_LIST_MAX */
    uint32_t action_id;   /* OB_ACK */
    uint8_t result;       /* OB_ACK: 0 done, anything else = rejected for good */
} pda_cmd_t;

typedef struct {
    uint32_t action_id;
    uint64_t created_ms;
    uint16_t len;
    char json[PDA_PROTO_OB_JSON_MAX];
} pda_ob_entry_t;

/* ---- encoders: each returns the frame length ---- */
size_t pda_encode_status(const pda_status_t *s, uint8_t out[PDA_PROTO_STATUS_LEN]);
size_t pda_encode_time_ok(uint8_t out[PDA_PROTO_TIME_OK_LEN]);
size_t pda_encode_bye_ok(uint8_t out[PDA_PROTO_BYE_OK_LEN]);
size_t pda_encode_put_ready(uint8_t kind, uint32_t blob_id, uint8_t out[PDA_PROTO_PUT_READY_LEN]);
size_t pda_encode_put_ok(uint8_t kind, uint32_t blob_id, uint8_t out[PDA_PROTO_PUT_OK_LEN]);
/* out must hold PDA_PROTO_OB_ENTRY_HDR_LEN + e->len bytes; e->len <= 400. */
size_t pda_encode_ob_entry(const pda_ob_entry_t *e, uint8_t *out);
size_t pda_encode_ob_end(uint16_t count, uint32_t remaining, uint8_t out[PDA_PROTO_OB_END_LEN]);
size_t pda_encode_ob_acked(uint8_t result, uint32_t action_id, uint8_t out[PDA_PROTO_OB_ACKED_LEN]);
size_t pda_encode_error(uint8_t cmd, uint8_t code, uint32_t arg, uint8_t out[PDA_PROTO_ERROR_LEN]);
/* The scan response's service data flags byte, or 0: no service data in the scan
 * response (it carries the complete name then; §3.5, 31-byte limit). */
uint8_t pda_adv_flags(uint16_t outbox_waiting);

/* Parse one ctrl write. 0 and *out filled, or BAD_LENGTH / UNKNOWN_CMD / BAD_TIME. */
int pda_parse_cmd(const uint8_t *buf, size_t len, pda_cmd_t *out);

/* ---- one command at a time ---- */
typedef struct {
    bool busy;
} pda_gate_t;

/* busy, then MTU < 420, then parsing. True: accepted, *cmd filled, the gate stays busy
 * until pda_gate_done(). False: err_frame holds the ERROR to send. */
bool pda_gate_submit(pda_gate_t *gate, const uint8_t *buf, size_t len, uint16_t mtu,
                     pda_cmd_t *cmd, uint8_t err_frame[PDA_PROTO_ERROR_LEN]);
void pda_gate_done(pda_gate_t *gate);

/* ---- PUT state (one blob at a time) ---- */
typedef struct {
    bool active;
    uint8_t kind;
    uint32_t blob_id, total, crc32, received;
    uint8_t *buf; /* malloc'd at PUT_BEGIN */
} pda_put_t;

/* Drop a PUT in progress (disconnect, a new PUT_BEGIN, an error). */
void pda_put_abort(pda_put_t *put);

/* ---- the T5 side as the handler sees it; return 0 or a PDA_E_* code ---- */
typedef struct {
    /* Set the clock (epoch ms UTC, already range-checked) and the display offset. */
    int (*set_time)(void *ctx, int64_t epoch_ms, int16_t tz_min);
    /* A complete, CRC-checked blob: store it (feed swap / detail cache) and redraw. */
    int (*put_commit)(void *ctx, uint8_t kind, uint32_t blob_id, const uint8_t *data, size_t len);
    /* Up to max (<= PDA_PROTO_OB_BATCH) entries with action_id > after_id, ascending;
     * *remaining = entries after the last one returned. */
    size_t (*ob_list)(void *ctx, uint32_t after_id, pda_ob_entry_t *out, size_t max,
                      uint32_t *remaining);
    /* Remove the action; result 0 = done, else rejected (show "couldn't sync").
     * *was_queued = false if it was already gone (still success). */
    int (*ob_ack)(void *ctx, uint32_t action_id, uint8_t result, bool *was_queued);
} pda_store_t;

typedef int (*pda_send_t)(void *ctx, const uint8_t *frame, size_t len);

typedef enum {
    PDA_DONE,      /* final response (or none, for PUT_DATA) sent */
    PDA_BYE,       /* BYE_OK sent: the caller disconnects after ~1 s */
    PDA_LINK_LOST, /* a send failed */
} pda_result_t;

/* Run an accepted command. frame_buf holds PDA_PROTO_FRAME_MAX bytes. PUT_DATA sends
 * nothing on success (the write response is the flow control) and an ERROR otherwise. */
pda_result_t pda_execute(const pda_cmd_t *cmd, pda_put_t *put, const pda_store_t *store,
                         void *store_ctx, pda_send_t send, void *send_ctx, uint8_t *frame_buf);

/* Payload bytes per PUT_DATA at this MTU. */
size_t pda_put_payload_max(uint16_t mtu);

#ifdef __cplusplus
}
#endif
