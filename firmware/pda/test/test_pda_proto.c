/* Glance protocol v1 (docs/t5-pda-plan.md §3): frames, parsing, the gate and the handler. */
#include <stdlib.h>
#include <string.h>

#include "check.h"
#include "crc32.h"
#include "pda_proto.h"

/* ---- capture of sent frames ---- */
#define MAX_FRAMES 64
static uint8_t frames[MAX_FRAMES][PDA_PROTO_FRAME_MAX];
static size_t frame_len[MAX_FRAMES];
static int n_frames;
static int fail_after = -1; /* the send that fails (link lost), -1 never */

static int cap_send(void *ctx, const uint8_t *f, size_t len)
{
    (void)ctx;
    if (fail_after >= 0 && n_frames >= fail_after) {
        return -1;
    }
    memcpy(frames[n_frames], f, len);
    frame_len[n_frames++] = len;
    return 0;
}

static void reset_frames(void)
{
    n_frames = 0;
    fail_after = -1;
}

/* ---- fake store ---- */
typedef struct {
    int64_t time_ms;
    int16_t tz;
    int commits;
    uint8_t kind;
    uint32_t blob_id;
    uint8_t blob[4096];
    size_t blob_len;
    int commit_rc;
    uint32_t ids[10];
    int n;
    uint8_t last_result;
} store_t;

static int st_time(void *c, int64_t ms, int16_t tz)
{
    store_t *s = c;
    s->time_ms = ms;
    s->tz = tz;
    return 0;
}

static int st_commit(void *c, uint8_t kind, uint32_t id, const uint8_t *d, size_t len)
{
    store_t *s = c;
    if (s->commit_rc) {
        return s->commit_rc;
    }
    s->commits++;
    s->kind = kind;
    s->blob_id = id;
    memcpy(s->blob, d, len);
    s->blob_len = len;
    return 0;
}

static size_t st_list(void *c, uint32_t after, pda_ob_entry_t *out, size_t max, uint32_t *rem)
{
    store_t *s = c;
    size_t n = 0;
    int i = 0;
    while (i < s->n && s->ids[i] <= after) {
        i++;
    }
    for (; i < s->n && n < max; i++, n++) {
        out[n].action_id = s->ids[i];
        out[n].created_ms = 1000ull * s->ids[i];
        out[n].len = (uint16_t)snprintf(out[n].json, sizeof(out[n].json),
                                        "{\"t\":\"item_status\",\"id\":%u,\"status\":\"done\"}",
                                        (unsigned)s->ids[i]);
    }
    *rem = (uint32_t)(s->n - i);
    return n;
}

static int st_ack(void *c, uint32_t id, uint8_t result, bool *was)
{
    store_t *s = c;
    *was = false;
    s->last_result = result;
    for (int i = 0; i < s->n; i++) {
        if (s->ids[i] == id) {
            memmove(&s->ids[i], &s->ids[i + 1], sizeof(s->ids[0]) * (size_t)(s->n - i - 1));
            s->n--;
            *was = true;
        }
    }
    return 0;
}

static const pda_store_t STORE = {st_time, st_commit, st_list, st_ack};

static uint8_t fbuf[PDA_PROTO_FRAME_MAX];

/* Parse + gate + execute, like ble_pda.c does. */
static pda_result_t run(store_t *s, pda_put_t *put, const uint8_t *cmd, size_t len)
{
    pda_gate_t gate = {0};
    pda_cmd_t c;
    uint8_t err[PDA_PROTO_ERROR_LEN];
    if (!pda_gate_submit(&gate, cmd, len, 517, &c, err)) {
        cap_send(NULL, err, sizeof(err));
        return PDA_DONE;
    }
    const pda_result_t r = pda_execute(&c, put, &STORE, s, cap_send, NULL, fbuf);
    pda_gate_done(&gate);
    return r;
}

static void le32(uint8_t *p, uint32_t v)
{
    for (int i = 0; i < 4; i++) {
        p[i] = (uint8_t)(v >> (8 * i));
    }
}

static uint32_t rd32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void put_begin(uint8_t *b, uint8_t kind, uint32_t id, uint32_t total, uint32_t crc)
{
    memset(b, 0, 16);
    b[0] = PDA_OP_PUT_BEGIN;
    b[1] = kind;
    le32(b + 4, id);
    le32(b + 8, total);
    le32(b + 12, crc);
}

static void check_error(int i, uint8_t cmd, uint8_t code, uint32_t arg)
{
    CHECK_EQ(frame_len[i], PDA_PROTO_ERROR_LEN);
    CHECK_EQ(frames[i][0], PDA_OP_ERROR);
    CHECK_EQ(frames[i][1], cmd);
    CHECK_EQ(frames[i][2], code);
    CHECK_EQ(rd32(frames[i] + 4), arg);
}

static void test_encoders(void)
{
    uint8_t b[32];
    const pda_status_t s = {.flags = PDA_STATUS_TIME_VALID | PDA_STATUS_ACTIVE, .battery_pct = -1,
                            .outbox = 3, .feed_version = 0x11223344, .boot_id = 0xA1B2C3D4};
    CHECK_EQ(pda_encode_status(&s, b), 16);
    static const uint8_t want_status[16] = {1,    0x11, 0xFF, 0,    3,    0,    0,    0,
                                            0x44, 0x33, 0x22, 0x11, 0xD4, 0xC3, 0xB2, 0xA1};
    CHECK(memcmp(b, want_status, 16) == 0);

    CHECK_EQ(pda_encode_time_ok(b), 4);
    CHECK(b[0] == 0x86 && b[1] == 0 && b[2] == 0 && b[3] == 0);
    CHECK_EQ(pda_encode_bye_ok(b), 1);
    CHECK_EQ(b[0], 0x87);
    CHECK_EQ(pda_encode_put_ready(1, 0x01020304, b), 8);
    static const uint8_t want_ready[8] = {0x90, 1, 0, 0, 4, 3, 2, 1};
    CHECK(memcmp(b, want_ready, 8) == 0);
    CHECK_EQ(pda_encode_put_ok(2, 7, b), 8);
    CHECK(b[0] == 0x91 && b[1] == 2 && b[4] == 7);
    CHECK_EQ(pda_encode_ob_end(2, 5, b), 8);
    static const uint8_t want_end[8] = {0xA1, 0, 2, 0, 5, 0, 0, 0};
    CHECK(memcmp(b, want_end, 8) == 0);
    CHECK_EQ(pda_encode_ob_acked(1, 0x2A, b), 8);
    static const uint8_t want_acked[8] = {0xA2, 1, 0, 0, 0x2A, 0, 0, 0};
    CHECK(memcmp(b, want_acked, 8) == 0);
    CHECK_EQ(pda_encode_error(0x12, PDA_E_BAD_CRC, 0xDEADBEEF, b), 8);
    static const uint8_t want_err[8] = {0xEE, 0x12, 8, 0, 0xEF, 0xBE, 0xAD, 0xDE};
    CHECK(memcmp(b, want_err, 8) == 0);
    CHECK_EQ(pda_adv_flags(0), 0);
    CHECK_EQ(pda_adv_flags(3), PDA_ADV_OUTBOX);

    uint8_t big[PDA_PROTO_FRAME_MAX];
    pda_ob_entry_t e = {.action_id = 0x2A, .created_ms = 0x0102030405060708ull, .len = 2};
    memcpy(e.json, "{}", 2);
    CHECK_EQ(pda_encode_ob_entry(&e, big), 18);
    static const uint8_t want_entry[18] = {0xA0, 0, 2, 0, 0x2A, 0, 0, 0, 8, 7, 6, 5, 4, 3, 2, 1,
                                           '{', '}'};
    CHECK(memcmp(big, want_entry, 18) == 0);
    /* A 400-byte action fits one notification at MTU 420. */
    e.len = PDA_PROTO_OB_JSON_MAX;
    CHECK(pda_encode_ob_entry(&e, big) <= PDA_PROTO_MIN_MTU - 3);
    CHECK_EQ(pda_put_payload_max(517), 509);
    CHECK_EQ(pda_put_payload_max(420), 412);
}

static void test_parse(void)
{
    pda_cmd_t c;
    uint8_t t[14] = {0x04, 0, 0, 0};
    const uint64_t ms = 1792479600000ull; /* 2026-10-20T07:00Z */
    for (int i = 0; i < 8; i++) {
        t[4 + i] = (uint8_t)(ms >> (8 * i));
    }
    t[12] = 0x78; /* +120 min */
    t[13] = 0x00;
    CHECK_EQ(pda_parse_cmd(t, 14, &c), 0);
    CHECK_EQ(c.epoch_ms, (int64_t)ms);
    CHECK_EQ(c.tz_min, 120);
    t[12] = 0x88; /* -120 = 0xFF88 */
    t[13] = 0xFF;
    CHECK_EQ(pda_parse_cmd(t, 14, &c), 0);
    CHECK_EQ(c.tz_min, -120);
    CHECK_EQ(pda_parse_cmd(t, 13, &c), PDA_E_BAD_LENGTH);
    t[12] = 0x80; /* -896 > 14 h */
    t[13] = 0xFC;
    CHECK_EQ(pda_parse_cmd(t, 14, &c), PDA_E_BAD_TIME);
    t[12] = t[13] = 0;
    memset(t + 4, 0, 8); /* 1970 */
    CHECK_EQ(pda_parse_cmd(t, 14, &c), PDA_E_BAD_TIME);

    const uint8_t bye[1] = {0x05};
    CHECK_EQ(pda_parse_cmd(bye, 1, &c), 0);
    CHECK_EQ(pda_parse_cmd(bye, 0, &c), PDA_E_BAD_LENGTH);
    const uint8_t bye2[2] = {0x05, 0};
    CHECK_EQ(pda_parse_cmd(bye2, 2, &c), PDA_E_BAD_LENGTH);
    const uint8_t junk[1] = {0x99};
    CHECK_EQ(pda_parse_cmd(junk, 1, &c), PDA_E_UNKNOWN_CMD);

    uint8_t pb[16];
    put_begin(pb, 1, 9, 100, 0xCAFEBABE);
    CHECK_EQ(pda_parse_cmd(pb, 16, &c), 0);
    CHECK(c.kind == 1 && c.blob_id == 9 && c.total == 100 && c.crc32 == 0xCAFEBABE);
    CHECK_EQ(pda_parse_cmd(pb, 15, &c), PDA_E_BAD_LENGTH);

    const uint8_t pd[8] = {0x11, 0x10, 0, 0, 0, 'a', 'b', 'c'};
    CHECK_EQ(pda_parse_cmd(pd, 8, &c), 0);
    CHECK(c.offset == 16 && c.data_len == 3 && c.data == pd + 5);
    CHECK_EQ(pda_parse_cmd(pd, 5, &c), PDA_E_BAD_LENGTH); /* no payload */

    const uint8_t ol[7] = {0x20, 5, 0, 0, 0, 100, 0};
    CHECK_EQ(pda_parse_cmd(ol, 7, &c), 0);
    CHECK(c.after_id == 5 && c.max == PDA_PROTO_OB_LIST_MAX); /* capped */
    const uint8_t oa[6] = {0x21, 7, 0, 0, 0, 1};
    CHECK_EQ(pda_parse_cmd(oa, 6, &c), 0);
    CHECK(c.action_id == 7 && c.result == 1);
}

static void test_gate(void)
{
    pda_gate_t g = {0};
    pda_cmd_t c;
    uint8_t err[8];
    const uint8_t bye[1] = {0x05};
    CHECK(pda_gate_submit(&g, bye, 1, 517, &c, err));
    CHECK(!pda_gate_submit(&g, bye, 1, 517, &c, err));
    CHECK(err[0] == 0xEE && err[1] == 0x05 && err[2] == PDA_E_BUSY);
    pda_gate_done(&g);
    CHECK(!pda_gate_submit(&g, bye, 1, 419, &c, err));
    CHECK(err[2] == PDA_E_MTU_TOO_SMALL && err[4] == (419 & 0xFF) && err[5] == (419 >> 8));
    CHECK(!g.busy);
    CHECK(pda_gate_submit(&g, bye, 1, 420, &c, err));
}

static void test_time_bye(void)
{
    store_t s = {0};
    pda_put_t put = {0};
    reset_frames();
    uint8_t t[14] = {0x04};
    const uint64_t ms = 1792479600123ull;
    for (int i = 0; i < 8; i++) {
        t[4 + i] = (uint8_t)(ms >> (8 * i));
    }
    t[12] = 60;
    CHECK_EQ(run(&s, &put, t, 14), PDA_DONE);
    CHECK_EQ(s.time_ms, (int64_t)ms);
    CHECK_EQ(s.tz, 60);
    CHECK_EQ(n_frames, 1);
    CHECK(frame_len[0] == 4 && frames[0][0] == 0x86);
    reset_frames();
    const uint8_t bye[1] = {0x05};
    CHECK_EQ(run(&s, &put, bye, 1), PDA_BYE);
    CHECK(n_frames == 1 && frames[0][0] == 0x87);
}

/* Send a blob in PUT_DATA pieces of `step` bytes. */
static void put_blob(store_t *s, pda_put_t *put, const uint8_t *data, uint32_t len, size_t step)
{
    uint8_t w[PDA_PROTO_FRAME_MAX];
    for (uint32_t off = 0; off < len; off += (uint32_t)step) {
        const size_t n = len - off < step ? len - off : step;
        w[0] = 0x11;
        le32(w + 1, off);
        memcpy(w + 5, data + off, n);
        run(s, put, w, 5 + n);
    }
}

static void test_put(void)
{
    store_t s = {0};
    pda_put_t put = {0};
    uint8_t blob[3000];
    for (size_t i = 0; i < sizeof(blob); i++) {
        blob[i] = (uint8_t)(i * 7);
    }
    const uint32_t crc = crc32_update(0, blob, sizeof(blob));
    uint8_t pb[16];
    const uint8_t end[1] = {0x12};

    /* the good path */
    reset_frames();
    put_begin(pb, PDA_KIND_FEED, 77, sizeof(blob), crc);
    run(&s, &put, pb, 16);
    CHECK_EQ(n_frames, 1);
    CHECK(frames[0][0] == 0x90 && frames[0][1] == 1 && rd32(frames[0] + 4) == 77);
    put_blob(&s, &put, blob, sizeof(blob), 509);
    CHECK_EQ(n_frames, 1); /* PUT_DATA answers nothing */
    run(&s, &put, end, 1);
    CHECK_EQ(n_frames, 2);
    CHECK(frames[1][0] == 0x91 && frames[1][1] == 1 && rd32(frames[1] + 4) == 77);
    CHECK_EQ(s.commits, 1);
    CHECK_EQ(s.blob_len, sizeof(blob));
    CHECK(memcmp(s.blob, blob, sizeof(blob)) == 0);
    CHECK(!put.active && put.buf == NULL);

    /* bad CRC: rejected, nothing stored */
    reset_frames();
    put_begin(pb, PDA_KIND_FEED, 78, sizeof(blob), crc ^ 1);
    run(&s, &put, pb, 16);
    put_blob(&s, &put, blob, sizeof(blob), 200);
    run(&s, &put, end, 1);
    CHECK_EQ(n_frames, 2);
    check_error(1, 0x12, PDA_E_BAD_CRC, crc);
    CHECK_EQ(s.commits, 1);
    CHECK(!put.active);

    /* short: PUT_END before all bytes */
    reset_frames();
    put_begin(pb, PDA_KIND_DETAIL, 79, sizeof(blob), crc);
    run(&s, &put, pb, 16);
    put_blob(&s, &put, blob, 1000, 200);
    run(&s, &put, end, 1);
    check_error(1, 0x12, PDA_E_BAD_STATE, 1000);

    /* a gap aborts the PUT; PUT_END then has nothing */
    reset_frames();
    run(&s, &put, pb, 16);
    uint8_t w[16] = {0x11};
    le32(w + 1, 100);
    run(&s, &put, w, 10);
    check_error(1, 0x11, PDA_E_BAD_STATE, 0);
    CHECK(!put.active);
    run(&s, &put, end, 1);
    check_error(2, 0x12, PDA_E_BAD_STATE, 0);

    /* a rewrite of bytes already received is fine (a retried write) */
    reset_frames();
    run(&s, &put, pb, 16);
    put_blob(&s, &put, blob, 1000, 500);
    w[0] = 0x11;
    le32(w + 1, 500);
    memcpy(w + 5, blob + 500, 11);
    run(&s, &put, w, 16);
    CHECK_EQ(put.received, 1000);
    pda_put_abort(&put); /* disconnect */
    CHECK(!put.active && put.buf == NULL);

    /* past the end */
    reset_frames();
    put_begin(pb, PDA_KIND_FEED, 80, 10, 0);
    run(&s, &put, pb, 16);
    le32(w + 1, 0);
    run(&s, &put, w, 16); /* 11 bytes into a 10-byte blob */
    check_error(1, 0x11, PDA_E_TOO_LARGE, 0);

    /* too large, unknown kind, empty, data without begin */
    reset_frames();
    put_begin(pb, PDA_KIND_FEED, 81, PDA_PROTO_BLOB_MAX + 1, 0);
    run(&s, &put, pb, 16);
    check_error(0, 0x10, PDA_E_TOO_LARGE, PDA_PROTO_BLOB_MAX + 1);
    put_begin(pb, 3, 81, 10, 0);
    run(&s, &put, pb, 16);
    check_error(1, 0x10, PDA_E_BAD_STATE, 3);
    put_begin(pb, 1, 81, 0, 0);
    run(&s, &put, pb, 16);
    check_error(2, 0x10, PDA_E_BAD_LENGTH, 0);
    run(&s, &put, w, 16);
    check_error(3, 0x11, PDA_E_BAD_STATE, 0);

    /* the store failing to write: io_error, PUT gone */
    reset_frames();
    s.commit_rc = PDA_E_IO;
    put_begin(pb, PDA_KIND_FEED, 82, 100, crc32_update(0, blob, 100));
    run(&s, &put, pb, 16);
    put_blob(&s, &put, blob, 100, 100);
    run(&s, &put, end, 1);
    check_error(1, 0x12, PDA_E_IO, 82);
    CHECK(!put.active);
    s.commit_rc = 0;

    /* a new PUT_BEGIN replaces one in progress */
    reset_frames();
    put_begin(pb, PDA_KIND_FEED, 83, 100, 0);
    run(&s, &put, pb, 16);
    put_blob(&s, &put, blob, 50, 50);
    put_begin(pb, PDA_KIND_FEED, 84, 100, crc32_update(0, blob, 100));
    run(&s, &put, pb, 16);
    CHECK_EQ(put.received, 0);
    put_blob(&s, &put, blob, 100, 60);
    run(&s, &put, end, 1);
    CHECK(frames[n_frames - 1][0] == 0x91 && rd32(frames[n_frames - 1] + 4) == 84);
}

static void ob_list_cmd(uint8_t *b, uint32_t after, uint16_t max)
{
    b[0] = 0x20;
    le32(b + 1, after);
    b[5] = (uint8_t)max;
    b[6] = (uint8_t)(max >> 8);
}

static void test_outbox_cmds(void)
{
    store_t s = {.ids = {3, 5, 9, 10, 11}, .n = 5};
    pda_put_t put = {0};
    uint8_t c[7];

    reset_frames();
    ob_list_cmd(c, 0, 3);
    run(&s, &put, c, 7);
    CHECK_EQ(n_frames, 4);
    CHECK(frames[0][0] == 0xA0 && rd32(frames[0] + 4) == 3);
    CHECK(rd32(frames[2] + 4) == 9);
    CHECK_EQ(frame_len[0], 16u + (frames[0][2] | (frames[0][3] << 8)));
    CHECK(memcmp(frames[0] + 16, "{\"t\":\"item_status\"", 18) == 0);
    CHECK(frames[3][0] == 0xA1 && frames[3][2] == 3 && rd32(frames[3] + 4) == 2);

    reset_frames();
    ob_list_cmd(c, 9, 16);
    run(&s, &put, c, 7);
    CHECK_EQ(n_frames, 3);
    CHECK(frames[2][0] == 0xA1 && frames[2][2] == 2 && rd32(frames[2] + 4) == 0);

    reset_frames();
    ob_list_cmd(c, 0, 0); /* count only */
    run(&s, &put, c, 7);
    CHECK_EQ(n_frames, 1);
    CHECK(frames[0][2] == 0 && rd32(frames[0] + 4) == 5);

    reset_frames();
    ob_list_cmd(c, 11, 4);
    run(&s, &put, c, 7);
    CHECK(n_frames == 1 && frames[0][2] == 0 && rd32(frames[0] + 4) == 0);

    /* link lost in the middle */
    reset_frames();
    fail_after = 1;
    ob_list_cmd(c, 0, 5);
    CHECK_EQ(run(&s, &put, c, 7), PDA_LINK_LOST);

    /* ack: removed, then again (lost OB_ACKED): result 1 = not queued, still fine */
    reset_frames();
    uint8_t a[6] = {0x21, 5, 0, 0, 0, 0};
    run(&s, &put, a, 6);
    CHECK(frames[0][0] == 0xA2 && frames[0][1] == 0 && rd32(frames[0] + 4) == 5);
    CHECK_EQ(s.n, 4);
    run(&s, &put, a, 6);
    CHECK(frames[1][0] == 0xA2 && frames[1][1] == 1);
    a[1] = 9;
    a[5] = 1; /* rejected for good */
    run(&s, &put, a, 6);
    CHECK_EQ(s.last_result, 1);
    CHECK(frames[2][1] == 0 && s.n == 3);
}

int main(void)
{
    test_encoders();
    test_parse();
    test_gate();
    test_time_bye();
    test_put();
    test_outbox_cmds();
    return checks_done("test_pda_proto");
}
