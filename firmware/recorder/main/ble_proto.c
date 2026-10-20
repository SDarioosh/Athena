#include "ble_proto.h"

#include <string.h>

static void put_u16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}

static void put_u32(uint8_t *p, uint32_t v)
{
    for (int i = 0; i < 4; i++) {
        p[i] = (uint8_t)(v >> (8 * i));
    }
}

static void put_u64(uint8_t *p, uint64_t v)
{
    for (int i = 0; i < 8; i++) {
        p[i] = (uint8_t)(v >> (8 * i));
    }
}

static uint16_t get_u16(const uint8_t *p)
{
    return (uint16_t)(p[0] | (p[1] << 8));
}

static uint32_t get_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static uint64_t get_u64(const uint8_t *p)
{
    return (uint64_t)get_u32(p) | ((uint64_t)get_u32(p + 4) << 32);
}

size_t ble_proto_encode_status(const ble_proto_status_t *s, uint8_t out[BLE_PROTO_STATUS_LEN])
{
    out[0] = BLE_PROTO_VERSION;
    out[1] = s->flags;
    out[2] = (uint8_t)s->battery_pct;
    out[3] = 0;
    put_u16(out + 4, s->battery_mv);
    put_u16(out + 6, s->queued_chunks);
    put_u32(out + 8, s->queued_bytes);
    put_u32(out + 12, s->queued_ms);
    put_u32(out + 16, s->clock_s);
    put_u32(out + 20, s->boot_id);
    return BLE_PROTO_STATUS_LEN;
}

size_t ble_proto_encode_entry(const ble_proto_entry_t *e, uint8_t out[BLE_PROTO_ENTRY_LEN])
{
    out[0] = BLE_OP_ENTRY;
    out[1] = e->flags;
    put_u16(out + 2, 0);
    put_u32(out + 4, e->chunk_id);
    memcpy(out + 8, e->session_id, 8);
    put_u32(out + 16, e->sequence);
    put_u64(out + 20, e->start_ms);
    put_u32(out + 28, e->duration_ms);
    put_u32(out + 32, e->bytes);
    put_u32(out + 36, e->crc32);
    return BLE_PROTO_ENTRY_LEN;
}

size_t ble_proto_encode_data_header(uint32_t chunk_id, uint32_t offset,
                                    uint8_t out[BLE_PROTO_DATA_HDR_LEN])
{
    out[0] = BLE_OP_DATA;
    put_u32(out + 1, chunk_id);
    put_u32(out + 5, offset);
    return BLE_PROTO_DATA_HDR_LEN;
}

size_t ble_proto_encode_read_end(uint8_t status, uint32_t chunk_id, uint32_t total_bytes,
                                 uint32_t crc32, uint8_t out[BLE_PROTO_READ_END_LEN])
{
    out[0] = BLE_OP_READ_END;
    out[1] = status;
    put_u16(out + 2, 0);
    put_u32(out + 4, chunk_id);
    put_u32(out + 8, total_bytes);
    put_u32(out + 12, crc32);
    return BLE_PROTO_READ_END_LEN;
}

size_t ble_proto_encode_acked(uint8_t result, uint32_t chunk_id, uint8_t out[BLE_PROTO_ACKED_LEN])
{
    out[0] = BLE_OP_ACKED;
    out[1] = result;
    put_u16(out + 2, 0);
    put_u32(out + 4, chunk_id);
    return BLE_PROTO_ACKED_LEN;
}

size_t ble_proto_encode_list_end(uint16_t count, uint32_t remaining,
                                 uint8_t out[BLE_PROTO_LIST_END_LEN])
{
    out[0] = BLE_OP_LIST_END;
    out[1] = 0;
    put_u16(out + 2, count);
    put_u32(out + 4, remaining);
    return BLE_PROTO_LIST_END_LEN;
}

size_t ble_proto_encode_time_ok(uint16_t fixed, uint8_t out[BLE_PROTO_TIME_OK_LEN])
{
    out[0] = BLE_OP_TIME_OK;
    out[1] = 0;
    put_u16(out + 2, fixed);
    return BLE_PROTO_TIME_OK_LEN;
}

size_t ble_proto_encode_bye_ok(uint8_t out[BLE_PROTO_BYE_OK_LEN])
{
    out[0] = BLE_OP_BYE_OK;
    return BLE_PROTO_BYE_OK_LEN;
}

size_t ble_proto_encode_error(uint8_t cmd, uint8_t code, uint32_t arg,
                              uint8_t out[BLE_PROTO_ERROR_LEN])
{
    out[0] = BLE_OP_ERROR;
    out[1] = cmd;
    out[2] = code;
    out[3] = 0;
    put_u32(out + 4, arg);
    return BLE_PROTO_ERROR_LEN;
}

static size_t cmd_len(uint8_t op)
{
    switch (op) {
    case BLE_OP_LIST: return 7;
    case BLE_OP_READ: return 13;
    case BLE_OP_ACK: return 5;
    case BLE_OP_TIME: return 12;
    case BLE_OP_BYE: return 1;
    default: return 0;
    }
}

int ble_proto_parse_cmd(const uint8_t *buf, size_t len, ble_proto_cmd_t *out)
{
    memset(out, 0, sizeof(*out));
    if (len == 0) {
        return BLE_PROTO_E_BAD_LENGTH;
    }
    out->op = buf[0];
    const size_t want = cmd_len(buf[0]);
    if (want == 0) {
        return BLE_PROTO_E_UNKNOWN_CMD;
    }
    if (len != want) {
        return BLE_PROTO_E_BAD_LENGTH;
    }
    switch (buf[0]) {
    case BLE_OP_LIST:
        out->after_id = get_u32(buf + 1);
        out->max = get_u16(buf + 5);
        if (out->max > BLE_PROTO_LIST_MAX) {
            out->max = BLE_PROTO_LIST_MAX;
        }
        break;
    case BLE_OP_READ:
        out->chunk_id = get_u32(buf + 1);
        out->offset = get_u32(buf + 5);
        out->length = get_u32(buf + 9);
        break;
    case BLE_OP_ACK:
        out->chunk_id = get_u32(buf + 1);
        break;
    case BLE_OP_TIME: {
        /* bytes 1..3 are padding so epoch_ms is 4-byte aligned; their value is ignored */
        const uint64_t ms = get_u64(buf + 4);
        if (ms < (uint64_t)BLE_PROTO_TIME_MIN_MS || ms >= (uint64_t)BLE_PROTO_TIME_END_MS) {
            return BLE_PROTO_E_BAD_TIME;
        }
        out->epoch_ms = (int64_t)ms;
        break;
    }
    default:
        break;
    }
    return 0;
}

bool ble_proto_gate_submit(ble_proto_gate_t *gate, const uint8_t *buf, size_t len, uint16_t mtu,
                           ble_proto_cmd_t *cmd, uint8_t err_frame[BLE_PROTO_ERROR_LEN])
{
    const uint8_t op = len > 0 ? buf[0] : 0;
    if (gate->busy) {
        ble_proto_encode_error(op, BLE_PROTO_E_BUSY, 0, err_frame);
        return false;
    }
    if (mtu < BLE_PROTO_MIN_MTU) {
        ble_proto_encode_error(op, BLE_PROTO_E_MTU_TOO_SMALL, mtu, err_frame);
        return false;
    }
    const int rc = ble_proto_parse_cmd(buf, len, cmd);
    if (rc != 0) {
        ble_proto_encode_error(op, (uint8_t)rc, 0, err_frame);
        return false;
    }
    gate->busy = true;
    return true;
}

void ble_proto_gate_done(ble_proto_gate_t *gate)
{
    gate->busy = false;
}

size_t ble_proto_data_payload_max(uint16_t mtu)
{
    if (mtu > BLE_PROTO_MAX_MTU) {
        mtu = BLE_PROTO_MAX_MTU;
    }
    if (mtu < BLE_PROTO_MIN_MTU) {
        mtu = BLE_PROTO_MIN_MTU;
    }
    size_t frame = (size_t)mtu - 3;
    if (frame > BLE_PROTO_FRAME_MAX) {
        frame = BLE_PROTO_FRAME_MAX;
    }
    return frame - BLE_PROTO_DATA_HDR_LEN;
}

static ble_proto_result_t send_error(const ble_proto_cmd_t *cmd, int code, uint32_t arg,
                                     ble_proto_send_t send, void *send_ctx, uint8_t *frame)
{
    const size_t n = ble_proto_encode_error(cmd->op, (uint8_t)code, arg, frame);
    return send(send_ctx, frame, n) == 0 ? BLE_PROTO_DONE : BLE_PROTO_LINK_LOST;
}

static ble_proto_result_t do_list(const ble_proto_cmd_t *cmd, const ble_proto_store_t *store,
                                  void *store_ctx, ble_proto_send_t send, void *send_ctx,
                                  uint8_t *frame)
{
    ble_proto_entry_t batch[BLE_PROTO_LIST_BATCH];
    uint32_t after = cmd->after_id;
    uint32_t remaining = 0;
    uint16_t sent = 0;
    size_t left = cmd->max;
    do {
        const size_t want = left < BLE_PROTO_LIST_BATCH ? left : BLE_PROTO_LIST_BATCH;
        size_t n = store->list(store_ctx, after, batch, want, &remaining);
        if (n > want) {
            n = want;
        }
        for (size_t i = 0; i < n; i++) {
            ble_proto_encode_entry(&batch[i], frame);
            if (send(send_ctx, frame, BLE_PROTO_ENTRY_LEN) != 0) {
                return BLE_PROTO_LINK_LOST;
            }
            after = batch[i].chunk_id;
            sent++;
        }
        left -= n;
        if (n < want) {
            break; /* nothing more after `after` */
        }
    } while (left > 0 && remaining > 0);
    const size_t n = ble_proto_encode_list_end(sent, remaining, frame);
    return send(send_ctx, frame, n) == 0 ? BLE_PROTO_DONE : BLE_PROTO_LINK_LOST;
}

static ble_proto_result_t do_read(const ble_proto_cmd_t *cmd, uint16_t mtu,
                                  const ble_proto_store_t *store, void *store_ctx,
                                  ble_proto_send_t send, void *send_ctx, uint8_t *frame)
{
    ble_proto_entry_t e;
    if (!store->get(store_ctx, cmd->chunk_id, &e)) {
        return send_error(cmd, BLE_PROTO_E_NOT_FOUND, cmd->chunk_id, send, send_ctx, frame);
    }
    /* [pos, end) of the file; an offset at or past the end sends no DATA. */
    uint32_t end = e.bytes;
    if (cmd->length != 0 && cmd->offset < e.bytes && cmd->length < e.bytes - cmd->offset) {
        end = cmd->offset + cmd->length;
    }
    uint32_t pos = cmd->offset;
    const size_t payload_max = ble_proto_data_payload_max(mtu);
    uint8_t status = 0;
    while (pos < end) {
        const uint32_t todo = end - pos;
        const size_t want = todo < payload_max ? todo : payload_max;
        size_t got = 0;
        const int rc = store->read(store_ctx, cmd->chunk_id, pos, frame + BLE_PROTO_DATA_HDR_LEN,
                                   want, &got);
        if (rc != 0 || got == 0 || got > want) {
            /* A read error, or the file is shorter than its index entry says. */
            status = rc != 0 ? (uint8_t)rc : BLE_PROTO_E_IO;
            break;
        }
        ble_proto_encode_data_header(cmd->chunk_id, pos, frame);
        if (send(send_ctx, frame, BLE_PROTO_DATA_HDR_LEN + got) != 0) {
            return BLE_PROTO_LINK_LOST;
        }
        pos += (uint32_t)got;
    }
    const size_t n = ble_proto_encode_read_end(status, cmd->chunk_id, e.bytes, e.crc32, frame);
    return send(send_ctx, frame, n) == 0 ? BLE_PROTO_DONE : BLE_PROTO_LINK_LOST;
}

ble_proto_result_t ble_proto_execute(const ble_proto_cmd_t *cmd, uint16_t mtu,
                                     const ble_proto_store_t *store, void *store_ctx,
                                     ble_proto_send_t send, void *send_ctx, uint8_t *frame_buf)
{
    switch (cmd->op) {
    case BLE_OP_LIST:
        return do_list(cmd, store, store_ctx, send, send_ctx, frame_buf);
    case BLE_OP_READ:
        return do_read(cmd, mtu, store, store_ctx, send, send_ctx, frame_buf);
    case BLE_OP_ACK: {
        bool was_queued = false;
        const int rc = store->ack(store_ctx, cmd->chunk_id, &was_queued);
        if (rc != 0) {
            return send_error(cmd, rc, cmd->chunk_id, send, send_ctx, frame_buf);
        }
        const size_t n = ble_proto_encode_acked(was_queued ? 0 : 1, cmd->chunk_id, frame_buf);
        return send(send_ctx, frame_buf, n) == 0 ? BLE_PROTO_DONE : BLE_PROTO_LINK_LOST;
    }
    case BLE_OP_TIME: {
        uint16_t fixed = 0;
        const int rc = store->set_time(store_ctx, cmd->epoch_ms, &fixed);
        if (rc != 0) {
            return send_error(cmd, rc, 0, send, send_ctx, frame_buf);
        }
        const size_t n = ble_proto_encode_time_ok(fixed, frame_buf);
        return send(send_ctx, frame_buf, n) == 0 ? BLE_PROTO_DONE : BLE_PROTO_LINK_LOST;
    }
    case BLE_OP_BYE: {
        const size_t n = ble_proto_encode_bye_ok(frame_buf);
        return send(send_ctx, frame_buf, n) == 0 ? BLE_PROTO_BYE : BLE_PROTO_LINK_LOST;
    }
    default:
        /* The gate only accepts parsed commands; keep the relay from waiting forever. */
        return send_error(cmd, BLE_PROTO_E_UNKNOWN_CMD, 0, send, send_ctx, frame_buf);
    }
}

ble_adv_reason_t ble_proto_adv_reason(const ble_adv_inputs_t *in)
{
    if (in->connected) {
        return BLE_ADV_NONE; /* one relay at a time */
    }
    if (in->window_open) {
        return BLE_ADV_WINDOW;
    }
    if (in->queued_chunks == 0) {
        return BLE_ADV_NONE;
    }
    if (in->has_last) {
        return BLE_ADV_LAST;
    }
    if (in->queued_ms >= BLE_ADV_AUDIO_MS) {
        return BLE_ADV_AUDIO;
    }
    if (in->ms_since_sync < 0 || in->ms_since_sync > BLE_ADV_STALE_MS) {
        return BLE_ADV_STALE;
    }
    return BLE_ADV_NONE;
}

const char *ble_proto_adv_reason_name(ble_adv_reason_t r)
{
    switch (r) {
    case BLE_ADV_WINDOW: return "sync window";
    case BLE_ADV_LAST: return "finished session queued";
    case BLE_ADV_AUDIO: return ">= 10 min audio queued";
    case BLE_ADV_STALE: return "queued, no sync for 60 min";
    default: return "none";
    }
}
