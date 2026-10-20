#include "pda_proto.h"

#include <stdlib.h>
#include <string.h>

#include "crc32.h"

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

/* ---- encoders ---- */

size_t pda_encode_status(const pda_status_t *s, uint8_t out[PDA_PROTO_STATUS_LEN])
{
    out[0] = PDA_PROTO_VERSION;
    out[1] = s->flags;
    out[2] = (uint8_t)s->battery_pct;
    out[3] = 0;
    put_u16(out + 4, s->outbox);
    put_u16(out + 6, 0);
    put_u32(out + 8, s->feed_version);
    put_u32(out + 12, s->boot_id);
    return PDA_PROTO_STATUS_LEN;
}

size_t pda_encode_time_ok(uint8_t out[PDA_PROTO_TIME_OK_LEN])
{
    out[0] = PDA_OP_TIME_OK;
    out[1] = out[2] = out[3] = 0;
    return PDA_PROTO_TIME_OK_LEN;
}

size_t pda_encode_bye_ok(uint8_t out[PDA_PROTO_BYE_OK_LEN])
{
    out[0] = PDA_OP_BYE_OK;
    return PDA_PROTO_BYE_OK_LEN;
}

static size_t encode_kind_blob(uint8_t op, uint8_t kind, uint32_t blob_id, uint8_t *out)
{
    out[0] = op;
    out[1] = kind;
    out[2] = out[3] = 0;
    put_u32(out + 4, blob_id);
    return 8;
}

size_t pda_encode_put_ready(uint8_t kind, uint32_t blob_id, uint8_t out[PDA_PROTO_PUT_READY_LEN])
{
    return encode_kind_blob(PDA_OP_PUT_READY, kind, blob_id, out);
}

size_t pda_encode_put_ok(uint8_t kind, uint32_t blob_id, uint8_t out[PDA_PROTO_PUT_OK_LEN])
{
    return encode_kind_blob(PDA_OP_PUT_OK, kind, blob_id, out);
}

size_t pda_encode_ob_entry(const pda_ob_entry_t *e, uint8_t *out)
{
    const uint16_t len = e->len > PDA_PROTO_OB_JSON_MAX ? PDA_PROTO_OB_JSON_MAX : e->len;
    out[0] = PDA_OP_OB_ENTRY;
    out[1] = 0;
    put_u16(out + 2, len);
    put_u32(out + 4, e->action_id);
    put_u64(out + 8, e->created_ms);
    memcpy(out + PDA_PROTO_OB_ENTRY_HDR_LEN, e->json, len);
    return PDA_PROTO_OB_ENTRY_HDR_LEN + len;
}

size_t pda_encode_ob_end(uint16_t count, uint32_t remaining, uint8_t out[PDA_PROTO_OB_END_LEN])
{
    out[0] = PDA_OP_OB_END;
    out[1] = 0;
    put_u16(out + 2, count);
    put_u32(out + 4, remaining);
    return PDA_PROTO_OB_END_LEN;
}

size_t pda_encode_ob_acked(uint8_t result, uint32_t action_id, uint8_t out[PDA_PROTO_OB_ACKED_LEN])
{
    out[0] = PDA_OP_OB_ACKED;
    out[1] = result;
    out[2] = out[3] = 0;
    put_u32(out + 4, action_id);
    return PDA_PROTO_OB_ACKED_LEN;
}

size_t pda_encode_error(uint8_t cmd, uint8_t code, uint32_t arg, uint8_t out[PDA_PROTO_ERROR_LEN])
{
    out[0] = PDA_OP_ERROR;
    out[1] = cmd;
    out[2] = code;
    out[3] = 0;
    put_u32(out + 4, arg);
    return PDA_PROTO_ERROR_LEN;
}

size_t pda_encode_adv_mfg(uint8_t flags, uint8_t out[PDA_ADV_MFG_LEN])
{
    put_u16(out, PDA_ADV_COMPANY);
    out[2] = flags;
    return PDA_ADV_MFG_LEN;
}

/* ---- parsing ---- */

int pda_parse_cmd(const uint8_t *buf, size_t len, pda_cmd_t *out)
{
    memset(out, 0, sizeof(*out));
    if (len < 1) {
        return PDA_E_BAD_LENGTH;
    }
    out->op = buf[0];
    switch (buf[0]) {
    case PDA_OP_TIME:
        if (len != 14) {
            return PDA_E_BAD_LENGTH;
        }
        out->epoch_ms = (int64_t)get_u64(buf + 4);
        out->tz_min = (int16_t)get_u16(buf + 12);
        if (out->epoch_ms < PDA_TIME_MIN_MS || out->epoch_ms >= PDA_TIME_END_MS ||
            out->tz_min < -PDA_TZ_MAX_MIN || out->tz_min > PDA_TZ_MAX_MIN) {
            return PDA_E_BAD_TIME;
        }
        return 0;
    case PDA_OP_BYE:
    case PDA_OP_PUT_END:
        return len == 1 ? 0 : PDA_E_BAD_LENGTH;
    case PDA_OP_PUT_BEGIN:
        if (len != 16) {
            return PDA_E_BAD_LENGTH;
        }
        out->kind = buf[1];
        out->blob_id = get_u32(buf + 4);
        out->total = get_u32(buf + 8);
        out->crc32 = get_u32(buf + 12);
        return 0;
    case PDA_OP_PUT_DATA:
        if (len <= PDA_PROTO_PUT_DATA_HDR_LEN) {
            return PDA_E_BAD_LENGTH; /* no payload */
        }
        out->offset = get_u32(buf + 1);
        out->data = buf + PDA_PROTO_PUT_DATA_HDR_LEN;
        out->data_len = len - PDA_PROTO_PUT_DATA_HDR_LEN;
        return 0;
    case PDA_OP_OB_LIST:
        if (len != 7) {
            return PDA_E_BAD_LENGTH;
        }
        out->after_id = get_u32(buf + 1);
        out->max = get_u16(buf + 5);
        if (out->max > PDA_PROTO_OB_LIST_MAX) {
            out->max = PDA_PROTO_OB_LIST_MAX;
        }
        return 0;
    case PDA_OP_OB_ACK:
        if (len != 6) {
            return PDA_E_BAD_LENGTH;
        }
        out->action_id = get_u32(buf + 1);
        out->result = buf[5];
        return 0;
    default:
        return PDA_E_UNKNOWN_CMD;
    }
}

bool pda_gate_submit(pda_gate_t *gate, const uint8_t *buf, size_t len, uint16_t mtu,
                     pda_cmd_t *cmd, uint8_t err_frame[PDA_PROTO_ERROR_LEN])
{
    const uint8_t op = len > 0 ? buf[0] : 0;
    if (gate->busy) {
        pda_encode_error(op, PDA_E_BUSY, 0, err_frame);
        return false;
    }
    if (mtu < PDA_PROTO_MIN_MTU) {
        pda_encode_error(op, PDA_E_MTU_TOO_SMALL, mtu, err_frame);
        return false;
    }
    const int rc = pda_parse_cmd(buf, len, cmd);
    if (rc != 0) {
        pda_encode_error(op, (uint8_t)rc, 0, err_frame);
        return false;
    }
    gate->busy = true;
    return true;
}

void pda_gate_done(pda_gate_t *gate)
{
    gate->busy = false;
}

size_t pda_put_payload_max(uint16_t mtu)
{
    if (mtu > PDA_PROTO_MAX_MTU) {
        mtu = PDA_PROTO_MAX_MTU;
    }
    return mtu > 3 + PDA_PROTO_PUT_DATA_HDR_LEN ? (size_t)mtu - 3 - PDA_PROTO_PUT_DATA_HDR_LEN : 0;
}

void pda_put_abort(pda_put_t *put)
{
    free(put->buf);
    memset(put, 0, sizeof(*put));
}

/* ---- the handler ---- */

static pda_result_t send_frame(pda_send_t send, void *ctx, const uint8_t *f, size_t len)
{
    return send(ctx, f, len) == 0 ? PDA_DONE : PDA_LINK_LOST;
}

static pda_result_t send_error(pda_send_t send, void *ctx, uint8_t op, uint8_t code, uint32_t arg)
{
    uint8_t f[PDA_PROTO_ERROR_LEN];
    pda_encode_error(op, code, arg, f);
    return send_frame(send, ctx, f, sizeof(f));
}

static pda_result_t do_put_begin(const pda_cmd_t *c, pda_put_t *put, pda_send_t send, void *sctx,
                                 uint8_t *f)
{
    pda_put_abort(put); /* a new PUT replaces one in progress */
    if (c->kind != PDA_KIND_FEED && c->kind != PDA_KIND_DETAIL) {
        return send_error(send, sctx, c->op, PDA_E_BAD_STATE, c->kind);
    }
    if (c->total == 0) {
        return send_error(send, sctx, c->op, PDA_E_BAD_LENGTH, 0);
    }
    if (c->total > PDA_PROTO_BLOB_MAX) {
        return send_error(send, sctx, c->op, PDA_E_TOO_LARGE, c->total);
    }
    put->buf = malloc(c->total);
    if (!put->buf) {
        return send_error(send, sctx, c->op, PDA_E_IO, c->total);
    }
    put->active = true;
    put->kind = c->kind;
    put->blob_id = c->blob_id;
    put->total = c->total;
    put->crc32 = c->crc32;
    put->received = 0;
    return send_frame(send, sctx, f, pda_encode_put_ready(c->kind, c->blob_id, f));
}

static pda_result_t do_put_data(const pda_cmd_t *c, pda_put_t *put, pda_send_t send, void *sctx)
{
    if (!put->active) {
        return send_error(send, sctx, c->op, PDA_E_BAD_STATE, 0);
    }
    /* Writes go in order; rewriting bytes already received is allowed, a gap is not. */
    if (c->offset > put->received) {
        const uint32_t expected = put->received;
        pda_put_abort(put);
        return send_error(send, sctx, c->op, PDA_E_BAD_STATE, expected);
    }
    if ((uint64_t)c->offset + c->data_len > put->total) {
        pda_put_abort(put);
        return send_error(send, sctx, c->op, PDA_E_TOO_LARGE, c->offset);
    }
    memcpy(put->buf + c->offset, c->data, c->data_len);
    const uint32_t end = c->offset + (uint32_t)c->data_len;
    if (end > put->received) {
        put->received = end;
    }
    return PDA_DONE;
}

static pda_result_t do_put_end(const pda_cmd_t *c, pda_put_t *put, const pda_store_t *store,
                               void *ctx, pda_send_t send, void *sctx, uint8_t *f)
{
    if (!put->active) {
        return send_error(send, sctx, c->op, PDA_E_BAD_STATE, 0);
    }
    if (put->received != put->total) {
        const uint32_t got = put->received;
        pda_put_abort(put);
        return send_error(send, sctx, c->op, PDA_E_BAD_STATE, got);
    }
    const uint32_t crc = crc32_update(0, put->buf, put->total);
    if (crc != put->crc32) {
        pda_put_abort(put);
        return send_error(send, sctx, c->op, PDA_E_BAD_CRC, crc);
    }
    const uint8_t kind = put->kind;
    const uint32_t blob_id = put->blob_id;
    const int rc = store->put_commit(ctx, kind, blob_id, put->buf, put->total);
    pda_put_abort(put);
    if (rc != 0) {
        return send_error(send, sctx, c->op, (uint8_t)rc, blob_id);
    }
    return send_frame(send, sctx, f, pda_encode_put_ok(kind, blob_id, f));
}

static pda_result_t do_ob_list(const pda_cmd_t *c, const pda_store_t *store, void *ctx,
                               pda_send_t send, void *sctx, uint8_t *f)
{
    pda_ob_entry_t batch[PDA_PROTO_OB_BATCH];
    uint32_t after = c->after_id, remaining = 0;
    uint16_t sent = 0;
    if (c->max == 0) {
        store->ob_list(ctx, after, batch, 0, &remaining);
    }
    while (sent < c->max) {
        size_t want = c->max - sent;
        if (want > PDA_PROTO_OB_BATCH) {
            want = PDA_PROTO_OB_BATCH;
        }
        const size_t n = store->ob_list(ctx, after, batch, want, &remaining);
        for (size_t i = 0; i < n; i++) {
            if (send(sctx, f, pda_encode_ob_entry(&batch[i], f)) != 0) {
                return PDA_LINK_LOST;
            }
            sent++;
            after = batch[i].action_id;
        }
        if (n < want) {
            break; /* nothing more: remaining is 0 */
        }
    }
    return send_frame(send, sctx, f, pda_encode_ob_end(sent, remaining, f));
}

pda_result_t pda_execute(const pda_cmd_t *cmd, pda_put_t *put, const pda_store_t *store,
                         void *store_ctx, pda_send_t send, void *send_ctx, uint8_t *frame_buf)
{
    uint8_t *f = frame_buf;
    switch (cmd->op) {
    case PDA_OP_TIME: {
        const int rc = store->set_time(store_ctx, cmd->epoch_ms, cmd->tz_min);
        if (rc != 0) {
            return send_error(send, send_ctx, cmd->op, (uint8_t)rc, 0);
        }
        return send_frame(send, send_ctx, f, pda_encode_time_ok(f));
    }
    case PDA_OP_BYE: {
        const pda_result_t r = send_frame(send, send_ctx, f, pda_encode_bye_ok(f));
        return r == PDA_DONE ? PDA_BYE : r;
    }
    case PDA_OP_PUT_BEGIN:
        return do_put_begin(cmd, put, send, send_ctx, f);
    case PDA_OP_PUT_DATA:
        return do_put_data(cmd, put, send, send_ctx);
    case PDA_OP_PUT_END:
        return do_put_end(cmd, put, store, store_ctx, send, send_ctx, f);
    case PDA_OP_OB_LIST:
        return do_ob_list(cmd, store, store_ctx, send, send_ctx, f);
    case PDA_OP_OB_ACK: {
        bool was_queued = false;
        const int rc = store->ob_ack(store_ctx, cmd->action_id, cmd->result, &was_queued);
        if (rc != 0) {
            return send_error(send, send_ctx, cmd->op, (uint8_t)rc, cmd->action_id);
        }
        return send_frame(send, send_ctx, f,
                          pda_encode_ob_acked(was_queued ? 0 : 1, cmd->action_id, f));
    }
    default:
        return send_error(send, send_ctx, cmd->op, PDA_E_UNKNOWN_CMD, 0);
    }
}
