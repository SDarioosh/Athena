/* Host test for main/ble_proto.c: the §8 vectors of docs/twatch-ble-protocol.md byte for
 * byte, command parsing and its error paths, the busy rule, and the handler against a fake
 * store (LIST paging, READ framing at several MTUs, ACK idempotency, TIME, BYE). */
#include "ble_proto.h"
#include "check.h"

#include <stdio.h>
#include <string.h>

/* ---- helpers ---- */

static size_t unhex(const char *hex, uint8_t *out, size_t cap)
{
    size_t n = 0;
    int hi = -1;
    for (const char *p = hex; *p; p++) {
        int v;
        if (*p >= '0' && *p <= '9') {
            v = *p - '0';
        } else if (*p >= 'a' && *p <= 'f') {
            v = *p - 'a' + 10;
        } else if (*p >= 'A' && *p <= 'F') {
            v = *p - 'A' + 10;
        } else {
            continue; /* spaces */
        }
        if (hi < 0) {
            hi = v;
        } else {
            if (n < cap) {
                out[n] = (uint8_t)(hi << 4 | v);
            }
            n++;
            hi = -1;
        }
    }
    return n;
}

#define CHECK_BYTES(buf, len, hex)                                                         \
    do {                                                                                   \
        uint8_t _want[64];                                                                 \
        const size_t _n = unhex(hex, _want, sizeof(_want));                                \
        CHECK_EQ((len), _n);                                                               \
        if ((size_t)(len) == _n && memcmp((buf), _want, _n) != 0) {                        \
            fprintf(stderr, "%s:%d: bytes differ from %s\n", __FILE__, __LINE__, hex);     \
            checks_failed++;                                                               \
        }                                                                                  \
    } while (0)

static uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static uint16_t le16(const uint8_t *p)
{
    return (uint16_t)(p[0] | p[1] << 8);
}

/* ---- fake store ---- */

#define MAX_CHUNKS 64

typedef struct {
    ble_proto_entry_t e[MAX_CHUNKS];
    bool present[MAX_CHUNKS];
    size_t n;
    uint32_t short_by;      /* the file is this many bytes shorter than the index says */
    uint32_t fail_at;       /* read at or past this offset fails with io_error (0 = never) */
    bool ack_fails;
    int time_rc;
    uint16_t time_fixed;
    int64_t time_set;
    int reads;
} fake_store_t;

static uint8_t content(uint32_t chunk_id, uint32_t off)
{
    return (uint8_t)(chunk_id * 7u + off * 13u + (off >> 8));
}

static void store_add(fake_store_t *s, uint32_t id, uint32_t bytes)
{
    ble_proto_entry_t *e = &s->e[s->n];
    memset(e, 0, sizeof(*e));
    e->chunk_id = id;
    e->bytes = bytes;
    e->crc32 = 0xC0DE0000u + id;
    e->sequence = (uint32_t)s->n;
    e->duration_ms = 30000;
    e->start_ms = 1792411200000ULL + 30000ULL * s->n;
    e->session_id[0] = (uint8_t)id;
    s->present[s->n] = true;
    s->n++;
}

static int store_find(fake_store_t *s, uint32_t id)
{
    for (size_t i = 0; i < s->n; i++) {
        if (s->present[i] && s->e[i].chunk_id == id) {
            return (int)i;
        }
    }
    return -1;
}

static size_t f_list(void *ctx, uint32_t after, ble_proto_entry_t *out, size_t max,
                     uint32_t *remaining)
{
    fake_store_t *s = ctx;
    size_t got = 0;
    uint32_t rest = 0;
    for (size_t i = 0; i < s->n; i++) { /* ids are added in ascending order */
        if (!s->present[i] || s->e[i].chunk_id <= after) {
            continue;
        }
        if (got < max) {
            out[got++] = s->e[i];
        } else {
            rest++;
        }
    }
    *remaining = rest;
    return got;
}

static bool f_get(void *ctx, uint32_t id, ble_proto_entry_t *out)
{
    fake_store_t *s = ctx;
    const int i = store_find(s, id);
    if (i < 0) {
        return false;
    }
    *out = s->e[i];
    return true;
}

static int f_read(void *ctx, uint32_t id, uint32_t off, uint8_t *buf, size_t len, size_t *got)
{
    fake_store_t *s = ctx;
    s->reads++;
    const int i = store_find(s, id);
    if (i < 0) {
        return BLE_PROTO_E_NOT_FOUND;
    }
    if (s->fail_at && off + len > s->fail_at) {
        return BLE_PROTO_E_IO;
    }
    const uint32_t size = s->e[i].bytes - s->short_by;
    size_t n = 0;
    while (n < len && off + n < size) {
        buf[n] = content(id, (uint32_t)(off + n));
        n++;
    }
    *got = n;
    return 0;
}

static int f_ack(void *ctx, uint32_t id, bool *was_queued)
{
    fake_store_t *s = ctx;
    if (s->ack_fails) {
        return BLE_PROTO_E_IO;
    }
    const int i = store_find(s, id);
    *was_queued = i >= 0;
    if (i >= 0) {
        s->present[i] = false;
    }
    return 0;
}

static int f_set_time(void *ctx, int64_t ms, uint16_t *fixed)
{
    fake_store_t *s = ctx;
    s->time_set = ms;
    *fixed = s->time_fixed;
    return s->time_rc;
}

static const ble_proto_store_t STORE = {
    .list = f_list, .get = f_get, .read = f_read, .ack = f_ack, .set_time = f_set_time,
};

/* ---- captured notifications ---- */

#define MAX_FRAMES 512

typedef struct {
    uint8_t buf[MAX_FRAMES][BLE_PROTO_FRAME_MAX];
    size_t len[MAX_FRAMES];
    size_t n;
    size_t fail_after; /* the send with this index fails (0 = never) */
} sink_t;

static int sink_send(void *ctx, const uint8_t *frame, size_t len)
{
    sink_t *k = ctx;
    if (k->fail_after && k->n + 1 >= k->fail_after) {
        return -1;
    }
    if (k->n >= MAX_FRAMES || len > BLE_PROTO_FRAME_MAX) {
        checks_failed++;
        return -1;
    }
    memcpy(k->buf[k->n], frame, len);
    k->len[k->n] = len;
    k->n++;
    return 0;
}

static sink_t s_sink;
static uint8_t s_frame[BLE_PROTO_FRAME_MAX];

static ble_proto_result_t run(fake_store_t *st, const char *cmd_hex, uint16_t mtu)
{
    uint8_t raw[BLE_PROTO_CMD_MAX + 4];
    const size_t n = unhex(cmd_hex, raw, sizeof(raw));
    ble_proto_cmd_t cmd;
    CHECK_EQ(ble_proto_parse_cmd(raw, n, &cmd), 0);
    memset(&s_sink, 0, sizeof(s_sink));
    return ble_proto_execute(&cmd, mtu, &STORE, st, sink_send, &s_sink, s_frame);
}

/* ---- tests ---- */

static void test_vectors(void)
{
    uint8_t raw[32];
    ble_proto_cmd_t c;
    size_t n;

    n = unhex("01 00000000 2000", raw, sizeof(raw));
    CHECK_EQ(ble_proto_parse_cmd(raw, n, &c), 0);
    CHECK_EQ(c.op, BLE_OP_LIST);
    CHECK_EQ(c.after_id, 0);
    CHECK_EQ(c.max, 32);

    n = unhex("02 07000000 00000000 00000000", raw, sizeof(raw));
    CHECK_EQ(ble_proto_parse_cmd(raw, n, &c), 0);
    CHECK_EQ(c.op, BLE_OP_READ);
    CHECK_EQ(c.chunk_id, 7);
    CHECK_EQ(c.offset, 0);
    CHECK_EQ(c.length, 0);

    n = unhex("03 07000000", raw, sizeof(raw));
    CHECK_EQ(ble_proto_parse_cmd(raw, n, &c), 0);
    CHECK_EQ(c.op, BLE_OP_ACK);
    CHECK_EQ(c.chunk_id, 7);

    n = unhex("04 000000 006a0854a1010000", raw, sizeof(raw));
    CHECK_EQ(ble_proto_parse_cmd(raw, n, &c), 0);
    CHECK_EQ(c.op, BLE_OP_TIME);
    CHECK_EQ(c.epoch_ms, 1792411200000LL);

    n = unhex("05", raw, sizeof(raw));
    CHECK_EQ(ble_proto_parse_cmd(raw, n, &c), 0);
    CHECK_EQ(c.op, BLE_OP_BYE);

    uint8_t out[64];
    const ble_proto_entry_t e = {
        .chunk_id = 7,
        .flags = BLE_ENTRY_LAST,
        .session_id = {0x3f, 0x9a, 0x0c, 0x11, 0xd2, 0xe4, 0xb5, 0x07},
        .sequence = 2,
        .start_ms = 1792411260000ULL,
        .duration_ms = 12340,
        .bytes = 24680,
        .crc32 = 0xcbf43926u,
    };
    n = ble_proto_encode_entry(&e, out);
    CHECK_BYTES(out, n, "81 01 0000 07000000 3f9a0c11d2e4b507 02000000 60540954a1010000 "
                        "34300000 68600000 2639f4cb");

    /* Voice note (contract vector): chunk 8, LAST + NOTE, seq 0. */
    CHECK_EQ(BLE_ENTRY_NOTE, 0x04);
    const ble_proto_entry_t note = {
        .chunk_id = 8,
        .flags = BLE_ENTRY_LAST | BLE_ENTRY_NOTE,
        .session_id = {0x3f, 0x9a, 0x0c, 0x11, 0xd2, 0xe4, 0xb5, 0x07},
        .sequence = 0,
        .start_ms = 1792411260000ULL,
        .duration_ms = 12340,
        .bytes = 24680,
        .crc32 = 0xcbf43926u,
    };
    n = ble_proto_encode_entry(&note, out);
    CHECK_BYTES(out, n, "81 05 0000 08000000 3f9a0c11d2e4b507 00000000 60540954a1010000 "
                        "34300000 68600000 2639f4cb");

    n = ble_proto_encode_data_header(7, 505, out);
    out[n++] = 0xaa;
    out[n++] = 0xbb;
    CHECK_BYTES(out, n, "82 07000000 f9010000 aabb");

    n = ble_proto_encode_read_end(0, 7, 24680, 0xcbf43926u, out);
    CHECK_BYTES(out, n, "83 00 0000 07000000 68600000 2639f4cb");

    n = ble_proto_encode_acked(0, 7, out);
    CHECK_BYTES(out, n, "84 00 0000 07000000");

    n = ble_proto_encode_list_end(3, 5, out);
    CHECK_BYTES(out, n, "85 00 0300 05000000");

    n = ble_proto_encode_time_ok(2, out);
    CHECK_BYTES(out, n, "86 00 0200");

    n = ble_proto_encode_bye_ok(out);
    CHECK_BYTES(out, n, "87");

    n = ble_proto_encode_error(BLE_OP_READ, BLE_PROTO_E_NOT_FOUND, 9, out);
    CHECK_BYTES(out, n, "ee 02 03 00 09000000");

    const ble_proto_status_t st = {
        .flags = BLE_STATUS_RECORDING | BLE_STATUS_TIME_VALID | BLE_STATUS_SD_OK,
        .battery_pct = 81,
        .battery_mv = 3950,
        .queued_chunks = 2,
        .queued_bytes = 120000,
        .queued_ms = 60000,
        .clock_s = 1792411200u,
        .boot_id = 0x01020304u,
    };
    n = ble_proto_encode_status(&st, out);
    CHECK_BYTES(out, n, "01 25 51 00 6e0f 0200 c0d40100 60ea0000 4006d66a 04030201");

    const ble_proto_status_t unknown = {.battery_pct = -1};
    ble_proto_encode_status(&unknown, out);
    CHECK_EQ(out[2], 0xff);
}

static void test_parse_errors(void)
{
    uint8_t raw[32] = {0};
    ble_proto_cmd_t c;
    size_t n;

    CHECK_EQ(ble_proto_parse_cmd(raw, 0, &c), BLE_PROTO_E_BAD_LENGTH);
    const uint8_t unknown[] = {0x00, 0x06, 0x81, 0xee, 0xff};
    for (size_t i = 0; i < sizeof(unknown); i++) {
        CHECK_EQ(ble_proto_parse_cmd(&unknown[i], 1, &c), BLE_PROTO_E_UNKNOWN_CMD);
    }

    /* every command one byte short and one byte long */
    const char *good[] = {"01 00000000 2000", "02 07000000 00000000 00000000", "03 07000000",
                          "04 000000 006a0854a1010000", "05"};
    for (size_t i = 0; i < 5; i++) {
        n = unhex(good[i], raw, sizeof(raw));
        CHECK_EQ(ble_proto_parse_cmd(raw, n, &c), 0);
        if (n > 1) {
            CHECK_EQ(ble_proto_parse_cmd(raw, n - 1, &c), BLE_PROTO_E_BAD_LENGTH);
        }
        raw[n] = 0;
        CHECK_EQ(ble_proto_parse_cmd(raw, n + 1, &c), BLE_PROTO_E_BAD_LENGTH);
    }
    n = unhex("01 00000000", raw, sizeof(raw));
    CHECK_EQ(ble_proto_parse_cmd(raw, n, &c), BLE_PROTO_E_BAD_LENGTH);

    /* LIST max is capped at 32 */
    n = unhex("01 05000000 e803", raw, sizeof(raw));
    CHECK_EQ(ble_proto_parse_cmd(raw, n, &c), 0);
    CHECK_EQ(c.after_id, 5);
    CHECK_EQ(c.max, 32);

    /* READ fields */
    n = unhex("02 0a000000 64000000 32000000", raw, sizeof(raw));
    CHECK_EQ(ble_proto_parse_cmd(raw, n, &c), 0);
    CHECK_EQ(c.chunk_id, 10);
    CHECK_EQ(c.offset, 100);
    CHECK_EQ(c.length, 50);

    /* TIME bounds: 2024-01-01T00:00:00Z ok, a ms before not; 2100-01-01 not, a ms before ok */
    n = unhex("04 000000 00f451c28c010000", raw, sizeof(raw)); /* 1704067200000 */
    CHECK_EQ(ble_proto_parse_cmd(raw, n, &c), 0);
    CHECK_EQ(c.epoch_ms, 1704067200000LL);
    n = unhex("04 000000 fff351c28c010000", raw, sizeof(raw));
    CHECK_EQ(ble_proto_parse_cmd(raw, n, &c), BLE_PROTO_E_BAD_TIME);
    n = unhex("04 000000 00d8c32cbb030000", raw, sizeof(raw)); /* 4102444800000 */
    CHECK_EQ(ble_proto_parse_cmd(raw, n, &c), BLE_PROTO_E_BAD_TIME);
    n = unhex("04 000000 ffd7c32cbb030000", raw, sizeof(raw));
    CHECK_EQ(ble_proto_parse_cmd(raw, n, &c), 0);
    n = unhex("04 000000 0000000000000000", raw, sizeof(raw));
    CHECK_EQ(ble_proto_parse_cmd(raw, n, &c), BLE_PROTO_E_BAD_TIME);
    n = unhex("04 000000 ffffffffffffffff", raw, sizeof(raw)); /* negative as i64 */
    CHECK_EQ(ble_proto_parse_cmd(raw, n, &c), BLE_PROTO_E_BAD_TIME);
    /* padding bytes are ignored */
    n = unhex("04 ffffff 006a0854a1010000", raw, sizeof(raw));
    CHECK_EQ(ble_proto_parse_cmd(raw, n, &c), 0);
}

static void test_gate(void)
{
    ble_proto_gate_t g = {0};
    ble_proto_cmd_t c;
    uint8_t err[BLE_PROTO_ERROR_LEN];
    uint8_t raw[32];
    size_t n = unhex("02 07000000 00000000 00000000", raw, sizeof(raw));

    /* MTU below 64: every command, arg = MTU */
    CHECK(!ble_proto_gate_submit(&g, raw, n, 23, &c, err));
    CHECK_BYTES(err, sizeof(err), "ee 02 05 00 17000000");
    CHECK(!g.busy);
    CHECK(!ble_proto_gate_submit(&g, raw, n, 63, &c, err));
    CHECK_BYTES(err, sizeof(err), "ee 02 05 00 3f000000");

    /* accepted at 64; busy until done */
    CHECK(ble_proto_gate_submit(&g, raw, n, 64, &c, err));
    CHECK(g.busy);
    CHECK_EQ(c.op, BLE_OP_READ);
    uint8_t ack[8];
    const size_t an = unhex("03 07000000", ack, sizeof(ack));
    CHECK(!ble_proto_gate_submit(&g, ack, an, 517, &c, err));
    CHECK_BYTES(err, sizeof(err), "ee 03 04 00 00000000");
    /* busy wins over a bad command and a small MTU */
    CHECK(!ble_proto_gate_submit(&g, ack, 2, 23, &c, err));
    CHECK_BYTES(err, sizeof(err), "ee 03 04 00 00000000");
    CHECK(g.busy);
    ble_proto_gate_done(&g);
    CHECK(!g.busy);

    /* parse errors don't make the gate busy */
    CHECK(!ble_proto_gate_submit(&g, ack, 2, 517, &c, err));
    CHECK_BYTES(err, sizeof(err), "ee 03 01 00 00000000");
    CHECK(!g.busy);
    const uint8_t bad = 0x42;
    CHECK(!ble_proto_gate_submit(&g, &bad, 1, 517, &c, err));
    CHECK_BYTES(err, sizeof(err), "ee 42 02 00 00000000");
    CHECK(!ble_proto_gate_submit(&g, raw, 0, 517, &c, err));
    CHECK_BYTES(err, sizeof(err), "ee 00 01 00 00000000");
    n = unhex("04 000000 0000000000000000", raw, sizeof(raw));
    CHECK(!ble_proto_gate_submit(&g, raw, n, 517, &c, err));
    CHECK_BYTES(err, sizeof(err), "ee 04 07 00 00000000");
    CHECK(!g.busy);
    n = unhex("05", raw, sizeof(raw));
    CHECK(ble_proto_gate_submit(&g, raw, n, 517, &c, err));
}

static void check_list_end(size_t frame, uint16_t count, uint32_t remaining)
{
    CHECK_EQ(s_sink.len[frame], BLE_PROTO_LIST_END_LEN);
    CHECK_EQ(s_sink.buf[frame][0], BLE_OP_LIST_END);
    CHECK_EQ(le16(s_sink.buf[frame] + 2), count);
    CHECK_EQ(le32(s_sink.buf[frame] + 4), remaining);
}

static void test_list(void)
{
    fake_store_t st = {0};
    for (uint32_t i = 0; i < 50; i++) {
        store_add(&st, 3 + 2 * i, 1000 + i); /* ids 3, 5, ..., 101 */
    }

    CHECK_EQ(run(&st, "01 00000000 2000", 517), BLE_PROTO_DONE);
    CHECK_EQ(s_sink.n, 33);
    for (size_t i = 0; i < 32; i++) {
        CHECK_EQ(s_sink.len[i], BLE_PROTO_ENTRY_LEN);
        CHECK_EQ(s_sink.buf[i][0], BLE_OP_ENTRY);
        CHECK_EQ(le32(s_sink.buf[i] + 4), 3 + 2 * i);
        CHECK_EQ(le32(s_sink.buf[i] + 32), 1000 + i);
    }
    check_list_end(32, 32, 18);
    const uint32_t last = le32(s_sink.buf[31] + 4);
    CHECK_EQ(last, 65);

    /* continue after the last id: the rest, remaining 0 */
    CHECK_EQ(run(&st, "01 41000000 2000", 517), BLE_PROTO_DONE);
    CHECK_EQ(s_sink.n, 19);
    CHECK_EQ(le32(s_sink.buf[0] + 4), 67);
    CHECK_EQ(le32(s_sink.buf[17] + 4), 101);
    check_list_end(18, 18, 0);

    /* a larger max is capped at 32 */
    CHECK_EQ(run(&st, "01 00000000 ffff", 517), BLE_PROTO_DONE);
    check_list_end(32, 32, 18);

    /* small pages, an after_id between ids, max 0, past the end */
    CHECK_EQ(run(&st, "01 04000000 0500", 517), BLE_PROTO_DONE);
    CHECK_EQ(s_sink.n, 6);
    CHECK_EQ(le32(s_sink.buf[0] + 4), 5);
    check_list_end(5, 5, 44);
    CHECK_EQ(run(&st, "01 00000000 0000", 517), BLE_PROTO_DONE);
    CHECK_EQ(s_sink.n, 1);
    check_list_end(0, 0, 50);
    CHECK_EQ(run(&st, "01 65000000 2000", 517), BLE_PROTO_DONE);
    CHECK_EQ(s_sink.n, 1);
    check_list_end(0, 0, 0);

    /* exactly 8 (one batch) and 9 */
    CHECK_EQ(run(&st, "01 00000000 0800", 517), BLE_PROTO_DONE);
    check_list_end(8, 8, 42);
    CHECK_EQ(run(&st, "01 00000000 0900", 517), BLE_PROTO_DONE);
    check_list_end(9, 9, 41);

    /* empty queue */
    fake_store_t empty = {0};
    CHECK_EQ(run(&empty, "01 00000000 2000", 517), BLE_PROTO_DONE);
    CHECK_EQ(s_sink.n, 1);
    check_list_end(0, 0, 0);

    /* acked chunks drop out */
    st.present[0] = false;
    CHECK_EQ(run(&st, "01 00000000 0100", 517), BLE_PROTO_DONE);
    CHECK_EQ(le32(s_sink.buf[0] + 4), 5);
    check_list_end(1, 1, 48);
}

/* Check a READ's DATA frames cover [off, end) contiguously with the right bytes, each
 * frame at most MTU - 3, then a READ_END. Returns the number of DATA frames. */
static size_t check_read(uint32_t id, uint32_t off, uint32_t end, uint16_t mtu, uint32_t total,
                         uint8_t status)
{
    uint32_t pos = off;
    size_t i = 0;
    for (; i + 1 < s_sink.n; i++) {
        const uint8_t *f = s_sink.buf[i];
        CHECK_EQ(f[0], BLE_OP_DATA);
        CHECK(s_sink.len[i] <= (size_t)mtu - 3);
        CHECK(s_sink.len[i] > BLE_PROTO_DATA_HDR_LEN);
        CHECK_EQ(le32(f + 1), id);
        CHECK_EQ(le32(f + 5), pos);
        const size_t pl = s_sink.len[i] - BLE_PROTO_DATA_HDR_LEN;
        for (size_t k = 0; k < pl; k++) {
            if (f[BLE_PROTO_DATA_HDR_LEN + k] != content(id, pos + (uint32_t)k)) {
                checks_failed++;
                fprintf(stderr, "content mismatch at %u\n", (unsigned)(pos + k));
                break;
            }
        }
        /* every frame but the last is full */
        if (i + 2 < s_sink.n) {
            CHECK_EQ(pl, ble_proto_data_payload_max(mtu));
        }
        pos += (uint32_t)pl;
    }
    if (status == 0) {
        CHECK_EQ(pos, end);
    }
    const uint8_t *f = s_sink.buf[s_sink.n - 1];
    CHECK_EQ(s_sink.len[s_sink.n - 1], BLE_PROTO_READ_END_LEN);
    CHECK_EQ(f[0], BLE_OP_READ_END);
    CHECK_EQ(f[1], status);
    CHECK_EQ(le32(f + 4), id);
    CHECK_EQ(le32(f + 8), total);
    CHECK_EQ(le32(f + 12), 0xC0DE0000u + id);
    return i;
}

static void test_read(void)
{
    fake_store_t st = {0};
    store_add(&st, 7, 24680);
    store_add(&st, 8, 505);
    store_add(&st, 9, 0);
    char cmd[64];

    CHECK_EQ(ble_proto_data_payload_max(64), 52);
    CHECK_EQ(ble_proto_data_payload_max(517), 505);
    CHECK_EQ(ble_proto_data_payload_max(2000), 505);

    const uint16_t mtus[] = {64, 65, 185, 247, 251, 512, 517, 1024};
    for (size_t m = 0; m < sizeof(mtus) / sizeof(mtus[0]); m++) {
        CHECK_EQ(run(&st, "02 07000000 00000000 00000000", mtus[m]), BLE_PROTO_DONE);
        const size_t frames = check_read(7, 0, 24680, mtus[m] > 517 ? 517 : mtus[m], 24680, 0);
        const size_t pl = ble_proto_data_payload_max(mtus[m]);
        CHECK_EQ(frames, (24680 + pl - 1) / pl);
    }
    /* §7.2: at 517 a frame carries 505 bytes */
    CHECK_EQ(run(&st, "02 08000000 00000000 00000000", 517), BLE_PROTO_DONE);
    CHECK_EQ(check_read(8, 0, 505, 517, 505, 0), 1);
    CHECK_EQ(s_sink.len[0], 514);

    /* offset + length inside the file */
    CHECK_EQ(run(&st, "02 07000000 64000000 32000000", 64), BLE_PROTO_DONE);
    CHECK_EQ(check_read(7, 100, 150, 64, 24680, 0), 1);
    /* resume from an offset to the end */
    snprintf(cmd, sizeof(cmd), "02 07000000 %02x%02x0000 00000000", 20000 & 0xff, 20000 >> 8);
    CHECK_EQ(run(&st, cmd, 247), BLE_PROTO_DONE);
    check_read(7, 20000, 24680, 247, 24680, 0);
    /* length past the end, and a length that would overflow offset + length */
    snprintf(cmd, sizeof(cmd), "02 07000000 %02x%02x0000 ffff0000", 24000 & 0xff, 24000 >> 8);
    CHECK_EQ(run(&st, cmd, 517), BLE_PROTO_DONE);
    check_read(7, 24000, 24680, 517, 24680, 0);
    CHECK_EQ(run(&st, "02 07000000 0a000000 ffffffff", 517), BLE_PROTO_DONE);
    check_read(7, 10, 24680, 517, 24680, 0);
    /* offset at and past the end: no DATA, READ_END ok */
    CHECK_EQ(run(&st, "02 07000000 68600000 00000000", 517), BLE_PROTO_DONE);
    CHECK_EQ(check_read(7, 24680, 24680, 517, 24680, 0), 0);
    CHECK_EQ(run(&st, "02 07000000 ffffffff 05000000", 517), BLE_PROTO_DONE);
    CHECK_EQ(s_sink.n, 1);
    /* empty file */
    CHECK_EQ(run(&st, "02 09000000 00000000 00000000", 517), BLE_PROTO_DONE);
    CHECK_EQ(check_read(9, 0, 0, 517, 0, 0), 0);

    /* unknown chunk: ERROR not_found, arg = id */
    CHECK_EQ(run(&st, "02 0b000000 00000000 00000000", 517), BLE_PROTO_DONE);
    CHECK_EQ(s_sink.n, 1);
    CHECK_BYTES(s_sink.buf[0], s_sink.len[0], "ee 02 03 00 0b000000");

    /* SD error mid-read: the frames so far, then READ_END status io_error */
    st.fail_at = 2000;
    CHECK_EQ(run(&st, "02 07000000 00000000 00000000", 517), BLE_PROTO_DONE);
    CHECK_EQ(check_read(7, 0, 0, 517, 24680, BLE_PROTO_E_IO), 3);
    st.fail_at = 0;

    /* file shorter than its index entry */
    st.short_by = 100;
    CHECK_EQ(run(&st, "02 07000000 00000000 00000000", 517), BLE_PROTO_DONE);
    check_read(7, 0, 0, 517, 24680, BLE_PROTO_E_IO);
    st.short_by = 0;

    /* link lost on the third frame: stop, nothing more */
    uint8_t raw[16];
    ble_proto_cmd_t c;
    ble_proto_parse_cmd(raw, unhex("02 07000000 00000000 00000000", raw, sizeof(raw)), &c);
    memset(&s_sink, 0, sizeof(s_sink));
    s_sink.fail_after = 3;
    st.reads = 0;
    CHECK_EQ(ble_proto_execute(&c, 517, &STORE, &st, sink_send, &s_sink, s_frame),
             BLE_PROTO_LINK_LOST);
    CHECK_EQ(s_sink.n, 2);
    CHECK_EQ(st.reads, 3);
}

static void test_ack_time_bye(void)
{
    fake_store_t st = {0};
    store_add(&st, 7, 100);

    CHECK_EQ(run(&st, "03 07000000", 64), BLE_PROTO_DONE);
    CHECK_EQ(s_sink.n, 1);
    CHECK_BYTES(s_sink.buf[0], s_sink.len[0], "84 00 0000 07000000");
    /* again: already gone, result 1 (success too) */
    CHECK_EQ(run(&st, "03 07000000", 64), BLE_PROTO_DONE);
    CHECK_BYTES(s_sink.buf[0], s_sink.len[0], "84 01 0000 07000000");
    /* read after ack: not_found */
    CHECK_EQ(run(&st, "02 07000000 00000000 00000000", 64), BLE_PROTO_DONE);
    CHECK_BYTES(s_sink.buf[0], s_sink.len[0], "ee 02 03 00 07000000");
    /* SD failure on ack */
    st.ack_fails = true;
    CHECK_EQ(run(&st, "03 07000000", 64), BLE_PROTO_DONE);
    CHECK_BYTES(s_sink.buf[0], s_sink.len[0], "ee 03 06 00 07000000");

    st.time_fixed = 2;
    CHECK_EQ(run(&st, "04 000000 006a0854a1010000", 64), BLE_PROTO_DONE);
    CHECK_EQ(st.time_set, 1792411200000LL);
    CHECK_EQ(s_sink.n, 1);
    CHECK_BYTES(s_sink.buf[0], s_sink.len[0], "86 00 0200");
    st.time_rc = BLE_PROTO_E_IO;
    CHECK_EQ(run(&st, "04 000000 006a0854a1010000", 64), BLE_PROTO_DONE);
    CHECK_BYTES(s_sink.buf[0], s_sink.len[0], "ee 04 06 00 00000000");

    CHECK_EQ(run(&st, "05", 64), BLE_PROTO_BYE);
    CHECK_EQ(s_sink.n, 1);
    CHECK_BYTES(s_sink.buf[0], s_sink.len[0], "87");
}

static void test_adv(void)
{
    ble_adv_inputs_t in = {.ms_since_sync = -1};
    CHECK_EQ(ble_proto_adv_reason(&in), BLE_ADV_NONE);
    in.window_open = true;
    CHECK_EQ(ble_proto_adv_reason(&in), BLE_ADV_WINDOW);
    in.connected = true;
    CHECK_EQ(ble_proto_adv_reason(&in), BLE_ADV_NONE);
    in = (ble_adv_inputs_t){.queued_chunks = 1, .queued_ms = 30000, .has_last = true,
                            .ms_since_sync = 0};
    CHECK_EQ(ble_proto_adv_reason(&in), BLE_ADV_LAST);
    in.has_last = false;
    CHECK_EQ(ble_proto_adv_reason(&in), BLE_ADV_NONE);
    in.queued_ms = 10 * 60 * 1000 - 1;
    CHECK_EQ(ble_proto_adv_reason(&in), BLE_ADV_NONE);
    in.queued_ms = 10 * 60 * 1000;
    CHECK_EQ(ble_proto_adv_reason(&in), BLE_ADV_AUDIO);
    in.queued_ms = 30000;
    in.ms_since_sync = 60LL * 60 * 1000;
    CHECK_EQ(ble_proto_adv_reason(&in), BLE_ADV_NONE);
    in.ms_since_sync = 60LL * 60 * 1000 + 1;
    CHECK_EQ(ble_proto_adv_reason(&in), BLE_ADV_STALE);
    in.ms_since_sync = -1; /* never synced since boot */
    CHECK_EQ(ble_proto_adv_reason(&in), BLE_ADV_STALE);
    in.queued_chunks = 0;
    CHECK_EQ(ble_proto_adv_reason(&in), BLE_ADV_NONE);
    in.connected = true;
    in.queued_chunks = 3;
    in.has_last = true;
    CHECK_EQ(ble_proto_adv_reason(&in), BLE_ADV_NONE);
    CHECK(strcmp(ble_proto_adv_reason_name(BLE_ADV_NONE), "none") == 0);
}

int main(void)
{
    test_vectors();
    test_parse_errors();
    test_gate();
    test_list();
    test_read();
    test_ack_time_bye();
    test_adv();
    return checks_done("test_ble_proto");
}
