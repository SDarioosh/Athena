/* The persistent outbox (§2, §4.2) and the action JSON built from taps. */
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "check.h"
#include "outbox.h"
#include "json.h"
#include "pda_actions.h"

static char dir[256];
static outbox_t ob;

static void rm_rf(const char *d)
{
    char cmd[300];
    snprintf(cmd, sizeof(cmd), "rm -rf '%s'", d);
    if (system(cmd) != 0) {
        exit(1);
    }
}

static void test_actions(void)
{
    char b[PDA_ACTION_BUF];
    CHECK_EQ(pda_action_item_status(b, 42, "done"), strlen(b));
    CHECK(strcmp(b, "{\"t\":\"item_status\",\"id\":42,\"status\":\"done\"}") == 0);
    pda_action_cal_add(b, "Call \"Tom\"", "2026-10-21T09:00:00+02:00", "2026-10-21T09:30:00+02:00",
                       false, 12);
    CHECK(strcmp(b, "{\"t\":\"cal_add\",\"title\":\"Call \\\"Tom\\\"\",\"start\":"
                    "\"2026-10-21T09:00:00+02:00\",\"end\":\"2026-10-21T09:30:00+02:00\","
                    "\"from_item\":12}") == 0);
    pda_action_cal_add(b, "Reminder", "2026-10-23", NULL, true, -1);
    CHECK(strcmp(b, "{\"t\":\"cal_add\",\"title\":\"Reminder\",\"start\":\"2026-10-23\","
                    "\"all_day\":true}") == 0);
    pda_action_fetch(b, "transcript:c_1@600-1200");
    CHECK(strcmp(b, "{\"t\":\"fetch\",\"ref\":\"transcript:c_1@600-1200\"}") == 0);
    pda_action_digest_refresh(b, "2026-10-19");
    CHECK(strcmp(b, "{\"t\":\"digest_refresh\",\"date\":\"2026-10-19\"}") == 0);
    pda_action_cal_delete(b, "ev_9");
    CHECK(strcmp(b, "{\"t\":\"cal_delete\",\"id\":\"ev_9\"}") == 0);

    /* a long title is cut to keep the action <= 400 bytes and valid */
    char title[600];
    for (int i = 0; i < 299; i++) {
        memcpy(title + 2 * i, "\xc3\xa9", 2); /* é */
    }
    title[598] = 0;
    const size_t n = pda_action_cal_add(b, title, "2026-10-21T09:00:00+02:00",
                                        "2026-10-21T10:00:00+02:00", false, 123456);
    CHECK(n > 300 && n <= PDA_PROTO_OB_JSON_MAX);
    CHECK_EQ(strlen(b), n);
    pda_action_info_t a;
    CHECK(pda_action_parse(b, n, &a));
    CHECK_EQ(a.type, PDA_ACT_CAL_ADD);
    CHECK_EQ(a.item_id, 123456);
    CHECK(strcmp(a.end, "2026-10-21T10:00:00+02:00") == 0);

    /* parse back */
    pda_action_item_status(b, 7, "dismissed");
    CHECK(pda_action_parse(b, strlen(b), &a));
    CHECK(a.type == PDA_ACT_ITEM_STATUS && a.item_id == 7 && strcmp(a.status, "dismissed") == 0);
    pda_action_fetch(b, "conv:c_9");
    CHECK(pda_action_parse(b, strlen(b), &a) && a.type == PDA_ACT_FETCH);
    CHECK(strcmp(a.ref, "conv:c_9") == 0);
    pda_action_cal_add(b, "X", "2026-10-23", NULL, true, -1);
    CHECK(pda_action_parse(b, strlen(b), &a) && a.all_day && a.item_id == -1);
    /* todo_toggle (§4.4) */
    size_t tn = pda_action_todo_toggle(b, "note_7", 3, "Milk", true);
    CHECK(strcmp(b, "{\"t\":\"todo_toggle\",\"note\":\"note_7\",\"i\":3,\"text\":\"Milk\",\"done\":true}") == 0);
    CHECK(pda_action_parse(b, tn, &a));
    CHECK(a.type == PDA_ACT_TODO_TOGGLE && a.item_id == 3 && a.done && strcmp(a.ref, "note_7") == 0);
    pda_action_todo_toggle(b, "n", 0, "Say \"hi\"", false);
    CHECK(strcmp(b, "{\"t\":\"todo_toggle\",\"note\":\"n\",\"i\":0,\"text\":\"Say \\\"hi\\\"\",\"done\":false}") == 0);
    /* text: the first 80 characters, counted in code points (ä is 2 bytes) */
    char long_text[400];
    size_t lw = 0;
    for (int k = 0; k < 100; k++) {
        memcpy(long_text + lw, "\xc3\xa4", 2); /* ä */
        lw += 2;
    }
    long_text[lw] = 0;
    tn = pda_action_todo_toggle(b, "n", 1, long_text, true);
    CHECK(pda_action_parse(b, tn, &a));
    {
        json_tok_t tk[16];
        const int nt = json_parse(b, tn, tk, 16);
        const json_doc_t d = {.js = b, .t = tk, .n = nt};
        char got[400];
        CHECK_EQ(json_str(&d, json_get(&d, 0, "text"), got, sizeof(got)), 160); /* 80 x 2 bytes */
    }
    /* 80 characters of a 4-byte script, a long note id: still <= 400 bytes and valid */
    char wide[400];
    lw = 0;
    for (int k = 0; k < 90; k++) {
        memcpy(wide + lw, "\xf0\x9f\x93\x8c", 4); /* U+1F4CC */
        lw += 4;
    }
    wide[lw] = 0;
    char note[120];
    memset(note, 'n', 100);
    note[100] = 0;
    tn = pda_action_todo_toggle(b, note, 39, wide, false);
    CHECK(tn > 0 && tn <= PDA_PROTO_OB_JSON_MAX && strlen(b) == tn);
    CHECK(pda_action_parse(b, tn, &a) && a.item_id == 39 && !a.done);
    /* a note id that can't fit is refused, never cut */
    char huge[500];
    memset(huge, 'x', 450);
    huge[450] = 0;
    CHECK_EQ(pda_action_todo_toggle(b, huge, 0, "x", true), 0);

    CHECK(!pda_action_parse("{\"t\":\"nope\"}", 12, &a));
    CHECK(!pda_action_parse("garbage", 7, &a));
}

static void add(const char *json, uint32_t *id)
{
    CHECK_EQ(outbox_add(&ob, json, strlen(json), 1700000000000ull, id), 0);
}

static void test_outbox(void)
{
    rm_rf(dir);
    CHECK_EQ(outbox_init(&ob, dir, 0x12345678), 0);
    CHECK_EQ(ob.n, 0);
    const uint32_t first = ob.next_id;
    CHECK_EQ(first, 0x12345679u); /* seed, made odd */
    uint32_t a, b, c;
    add("{\"t\":\"item_status\",\"id\":1,\"status\":\"done\"}", &a);
    add("{\"t\":\"item_status\",\"id\":2,\"status\":\"done\"}", &b);
    add("{\"t\":\"fetch\",\"ref\":\"conv:c\"}", &c);
    CHECK(a == first && b == first + 1 && c == first + 2);

    pda_ob_entry_t e[4];
    uint32_t rem;
    CHECK_EQ(outbox_list(&ob, 0, e, 2, &rem), 2);
    CHECK(e[0].action_id == a && e[1].action_id == b && rem == 1);
    CHECK(memcmp(e[0].json, "{\"t\":\"item_status\",\"id\":1", 25) == 0);
    CHECK_EQ(e[0].created_ms, 1700000000000ull);
    CHECK(outbox_find(&ob, a)->listed && !outbox_find(&ob, c)->listed);
    CHECK_EQ(outbox_list(&ob, b, e, 4, &rem), 1);
    CHECK(e[0].action_id == c && rem == 0);

    /* survives a reboot: same entries, ids keep counting */
    CHECK_EQ(outbox_init(&ob, dir, 999), 0);
    CHECK_EQ(ob.n, 3);
    CHECK_EQ(ob.next_id, first + 3);
    CHECK(!outbox_find(&ob, a)->listed); /* RAM only */

    /* ack: removed; again: not queued, still success */
    bool was;
    CHECK_EQ(outbox_remove(&ob, b, &was), 0);
    CHECK(was && ob.n == 2);
    CHECK_EQ(outbox_remove(&ob, b, &was), 0);
    CHECK(!was);
    CHECK_EQ(outbox_init(&ob, dir, 999), 0);
    CHECK_EQ(ob.n, 2);
    CHECK(outbox_find(&ob, b) == NULL);

    /* removing the newest never lets its id come back */
    CHECK_EQ(outbox_remove(&ob, c, &was), 0);
    CHECK_EQ(outbox_init(&ob, dir, 999), 0);
    uint32_t d;
    add("{\"t\":\"digest_refresh\",\"date\":\"2026-10-19\"}", &d);
    CHECK_EQ(d, first + 3);

    /* a torn write (.tmp) and a corrupt record are dropped at init */
    char path[400];
    snprintf(path, sizeof(path), "%s/%08x.act.tmp", dir, (unsigned)(first + 10));
    FILE *f = fopen(path, "wb");
    fputs("partial", f);
    fclose(f);
    snprintf(path, sizeof(path), "%s/%08x.act", dir, (unsigned)d);
    f = fopen(path, "r+b");
    fseek(f, 30, SEEK_SET);
    fputc('X', f); /* breaks the CRC */
    fclose(f);
    CHECK_EQ(outbox_init(&ob, dir, 999), 0);
    CHECK_EQ(ob.dropped_corrupt, 1);
    CHECK_EQ(ob.n, 1);
    CHECK(outbox_find(&ob, a) != NULL);
    snprintf(path, sizeof(path), "%s/%08x.act.tmp", dir, (unsigned)(first + 10));
    struct stat st;
    CHECK(stat(path, &st) != 0);
    /* the counter still never goes back */
    CHECK_EQ(ob.next_id, first + 4);

    /* a lost `next` file: ids continue after the highest on disk */
    snprintf(path, sizeof(path), "%s/next", dir);
    remove(path);
    CHECK_EQ(outbox_init(&ob, dir, 5), 0);
    CHECK_EQ(ob.next_id, a + 1);

    /* limits */
    CHECK_EQ(outbox_add(&ob, "", 0, 0, NULL), -2);
    char big[PDA_PROTO_OB_JSON_MAX + 2];
    memset(big, 'x', sizeof(big));
    CHECK_EQ(outbox_add(&ob, big, PDA_PROTO_OB_JSON_MAX + 1, 0, NULL), -2);
    CHECK_EQ(outbox_add(&ob, big, PDA_PROTO_OB_JSON_MAX, 0, NULL), 0);
    while (ob.n < OUTBOX_MAX) {
        CHECK_EQ(outbox_add(&ob, "{}", 2, 0, NULL), 0);
    }
    CHECK_EQ(outbox_add(&ob, "{}", 2, 0, NULL), -1);
    CHECK_EQ(outbox_init(&ob, dir, 5), 0);
    CHECK_EQ(ob.n, OUTBOX_MAX);
    pda_ob_entry_t one;
    const uint32_t id400 = ob.e[1].id;
    CHECK_EQ(outbox_read(&ob, id400, &one), 0);
    CHECK_EQ(one.len, PDA_PROTO_OB_JSON_MAX);
    CHECK_EQ(outbox_read(&ob, 1, &one), -1);

    CHECK_EQ(outbox_clear(&ob), 0);
    CHECK_EQ(ob.n, 0);
    const uint32_t next = ob.next_id;
    CHECK_EQ(outbox_init(&ob, dir, 5), 0);
    CHECK_EQ(ob.n, 0);
    CHECK_EQ(ob.next_id, next);
}

int main(void)
{
    const char *tmp = getenv("TEST_TMPDIR");
    snprintf(dir, sizeof(dir), "%s", tmp ? tmp : "/tmp/test_outbox");
    test_actions();
    test_outbox();
    return checks_done("test_outbox");
}
