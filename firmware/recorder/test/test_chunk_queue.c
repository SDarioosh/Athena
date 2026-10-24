/* Chunk queue on a scratch directory: add, list, read, ack, reboot, power-cut recovery,
 * TIME fix-up, torn records. */
#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "check.h"
#include "chunk_queue.h"
#include "crc32.h"
#include "ogg_opus.h"

static char g_dir[256];

static void clear_dir(void)
{
    DIR *d = opendir(g_dir);
    if (!d) {
        return;
    }
    struct dirent *e;
    char path[512];
    while ((e = readdir(d)) != NULL) {
        if (e->d_name[0] != '.') {
            snprintf(path, sizeof(path), "%s/%s", g_dir, e->d_name);
            remove(path);
        }
    }
    closedir(d);
}

static void path_of(uint32_t id, const char *ext, char *out, size_t len)
{
    snprintf(out, len, "%s/%08x.%s", g_dir, (unsigned)id, ext);
}

static bool exists(uint32_t id, const char *ext)
{
    char path[512];
    path_of(id, ext, path, sizeof(path));
    return access(path, F_OK) == 0;
}

static long file_size(uint32_t id, const char *ext)
{
    char path[512];
    path_of(id, ext, path, sizeof(path));
    FILE *f = fopen(path, "rb");
    if (!f) {
        return -1;
    }
    fseek(f, 0, SEEK_END);
    const long n = ftell(f);
    fclose(f);
    return n;
}

static uint32_t file_crc(uint32_t id)
{
    char path[512];
    path_of(id, "opus", path, sizeof(path));
    FILE *f = fopen(path, "rb");
    uint8_t buf[256];
    size_t n;
    uint32_t crc = 0;
    while (f && (n = fread(buf, 1, sizeof(buf), f)) > 0) {
        crc = crc32_update(crc, buf, n);
    }
    if (f) {
        fclose(f);
    }
    return crc;
}

static void poke(uint32_t id, const char *ext, long off, uint8_t xor_mask)
{
    char path[512];
    path_of(id, ext, path, sizeof(path));
    FILE *f = fopen(path, "r+b");
    fseek(f, off, SEEK_SET);
    int c = fgetc(f);
    fseek(f, off, SEEK_SET);
    fputc(c ^ xor_mask, f);
    fclose(f);
}

static const uint8_t SESSION_A[8] = {0x3f, 0x9a, 0x0c, 0x11, 0xd2, 0xe4, 0xb5, 0x07};
static const uint8_t SESSION_B[8] = {1, 2, 3, 4, 5, 6, 7, 8};
static const uint8_t SESSION_C[8] = {9, 9, 9, 9, 9, 9, 9, 9};
static const uint8_t SESSION_D[8] = {4, 4, 4, 4, 4, 4, 4, 4};

/* ---- writing a chunk the way the recorder does ---- */

static int to_queue(void *ctx, const void *data, size_t len)
{
    return cq_append(ctx, data, len) == CQ_OK ? 0 : -1;
}

static ogg_opus_writer_t g_w;

static uint32_t begin(chunk_queue_t *q, const uint8_t *session, uint32_t seq, uint64_t start_ms,
                      uint8_t flags, uint32_t boot, uint64_t mono)
{
    cq_new_t c = {.sequence = seq, .start_ms = start_ms, .flags = flags, .boot_id = boot,
                  .mono_ms = mono};
    memcpy(c.session_id, session, 8);
    uint32_t id = 0;
    CHECK_EQ(cq_begin(q, &c, &id), CQ_OK);
    ogg_opus_head_t h = {.serial = id, .pre_skip = 312, .input_rate = 16000};
    CHECK_EQ(ogg_opus_begin(&g_w, &h, to_queue, q), OGG_OPUS_OK);
    return id;
}

static void packets(chunk_queue_t *q, int n)
{
    uint8_t pkt[40];
    for (int i = 0; i < n; i++) {
        memset(pkt, i, sizeof(pkt));
        CHECK_EQ(ogg_opus_add_packet(&g_w, pkt, sizeof(pkt), 960), OGG_OPUS_OK);
    }
    CHECK_EQ(cq_sync(q), CQ_OK);
}

/* A whole chunk: n packets, finished with EOS, closed. */
static uint32_t add_chunk(chunk_queue_t *q, const uint8_t *session, uint32_t seq, int n, bool last)
{
    const uint32_t id = begin(q, session, seq, 1792411200000ull + seq * 30000ull, 0, 7, 0);
    packets(q, n);
    CHECK_EQ(ogg_opus_finish(&g_w, 0), OGG_OPUS_OK);
    CHECK_EQ(cq_end(q, (uint32_t)n * 20, last), CQ_OK);
    return id;
}

static void reopen_queue(chunk_queue_t *q, cq_boot_report_t *rep)
{
    cq_deinit(q);
    CHECK_EQ(cq_init(q, g_dir, rep), CQ_OK);
}

static void test_record(void)
{
    cq_info_t a = {.chunk_id = 7, .sequence = 2, .start_ms = 1792411260000ull,
                   .duration_ms = 12340, .bytes = 24680, .crc32 = 0xcbf43926, .flags = 1,
                   .state = CQ_STATE_CLOSED, .boot_id = 0x01020304, .mono_ms = 123456789012ull};
    memcpy(a.session_id, SESSION_A, 8);
    uint8_t rec[CQ_RECORD_SIZE];
    cq_record_encode(&a, rec);
    cq_info_t b;
    CHECK(cq_record_decode(rec, &b));
    CHECK(memcmp(&a, &b, sizeof(a)) == 0);
    for (int i = 0; i < CQ_RECORD_SIZE; i++) {
        rec[i] ^= 0x01;
        CHECK(!cq_record_decode(rec, &b)); /* any flipped bit is caught */
        rec[i] ^= 0x01;
    }
    char hex[17];
    cq_session_hex(SESSION_A, hex);
    CHECK(strcmp(hex, "3f9a0c11d2e4b507") == 0);
}

static void test_basic(void)
{
    clear_dir();
    chunk_queue_t q;
    cq_boot_report_t rep;
    CHECK_EQ(cq_init(&q, g_dir, &rep), CQ_OK);
    CHECK_EQ(q.count, 0);
    CHECK_EQ(q.next_id, 1);

    const uint32_t a = add_chunk(&q, SESSION_A, 0, 1500, false);
    const uint32_t b = add_chunk(&q, SESSION_A, 1, 1500, false);
    /* The open chunk is not listed. */
    const uint32_t c = begin(&q, SESSION_A, 2, 0, 0, 7, 0);
    packets(&q, 120);
    cq_info_t list[8];
    uint32_t remaining = 99;
    CHECK_EQ(cq_list(&q, 0, list, 8, &remaining), 2);
    CHECK_EQ(remaining, 0);
    CHECK(cq_current(&q, NULL));
    CHECK_EQ(ogg_opus_finish(&g_w, 0), OGG_OPUS_OK);
    CHECK_EQ(cq_end(&q, 2400, true), CQ_OK);
    CHECK(!cq_current(&q, NULL));
    CHECK_EQ(a, 1);
    CHECK_EQ(b, 2);
    CHECK_EQ(c, 3);

    CHECK_EQ(cq_list(&q, 0, list, 8, &remaining), 3);
    CHECK_EQ(list[0].chunk_id, 1);
    CHECK_EQ(list[2].chunk_id, 3);
    CHECK_EQ(list[0].flags, 0);
    CHECK_EQ(list[2].flags, CQ_FLAG_LAST);
    CHECK_EQ(list[1].sequence, 1);
    CHECK_EQ(list[1].start_ms, 1792411230000ull);
    CHECK_EQ(list[2].duration_ms, 2400);
    CHECK(memcmp(list[1].session_id, SESSION_A, 8) == 0);
    for (int i = 0; i < 3; i++) {
        CHECK_EQ(list[i].bytes, file_size(list[i].chunk_id, "opus"));
        CHECK_EQ(list[i].crc32, file_crc(list[i].chunk_id));
        CHECK_EQ(list[i].state, CQ_STATE_CLOSED);
    }

    /* Paging: after_id and max, remaining counts what follows. */
    CHECK_EQ(cq_list(&q, 0, list, 1, &remaining), 1);
    CHECK_EQ(list[0].chunk_id, 1);
    CHECK_EQ(remaining, 2);
    CHECK_EQ(cq_list(&q, 1, list, 1, &remaining), 1);
    CHECK_EQ(list[0].chunk_id, 2);
    CHECK_EQ(remaining, 1);
    CHECK_EQ(cq_list(&q, 3, list, 8, &remaining), 0);
    CHECK_EQ(remaining, 0);
    CHECK_EQ(cq_list(&q, UINT32_MAX, list, 8, &remaining), 0);

    cq_stats_t st;
    cq_stats(&q, &st);
    CHECK_EQ(st.chunks, 3);
    CHECK_EQ(st.audio_ms, 30000 + 30000 + 2400);
    CHECK_EQ(st.bytes, file_size(1, "opus") + file_size(2, "opus") + file_size(3, "opus"));
    CHECK(st.has_last);
    CHECK_EQ(st.oldest_id, 1);

    /* Reads in small pieces give back the file, CRC and all. */
    cq_info_t info;
    CHECK(cq_get(&q, 2, &info));
    uint32_t crc = 0, off = 0;
    uint8_t buf[333];
    size_t got;
    do {
        CHECK_EQ(cq_read(&q, 2, off, buf, sizeof(buf), &got), CQ_OK);
        crc = crc32_update(crc, buf, got);
        off += (uint32_t)got;
    } while (got);
    CHECK_EQ(off, info.bytes);
    CHECK_EQ(crc, info.crc32);
    CHECK_EQ(cq_read(&q, 2, info.bytes + 10, buf, sizeof(buf), &got), CQ_OK);
    CHECK_EQ(got, 0);
    CHECK_EQ(cq_read(&q, 42, 0, buf, sizeof(buf), &got), CQ_ERR_NOT_FOUND);
    /* Switching chunks mid-read reopens the right file. */
    CHECK_EQ(cq_read(&q, 1, 0, buf, 4, &got), CQ_OK);
    CHECK(got == 4 && memcmp(buf, "OggS", 4) == 0);

    /* Ack: both files go; a second ack (lost ACKED) is still fine. */
    bool was = false;
    CHECK_EQ(cq_delete(&q, 1, &was), CQ_OK);
    CHECK(was);
    CHECK(!exists(1, "opus") && !exists(1, "met"));
    CHECK_EQ(cq_delete(&q, 1, &was), CQ_OK);
    CHECK(!was);
    CHECK_EQ(cq_delete(&q, 1234, &was), CQ_OK);
    CHECK(!was);
    CHECK(!cq_get(&q, 1, NULL));
    CHECK_EQ(cq_list(&q, 0, list, 8, &remaining), 2);

    /* Reboot: the index comes back from the records. */
    reopen_queue(&q, &rep);
    CHECK_EQ(rep.loaded, 2);
    CHECK_EQ(rep.recovered + rep.discarded + rep.corrupt + rep.cleaned, 0);
    CHECK_EQ(cq_list(&q, 0, list, 8, &remaining), 2);
    CHECK_EQ(list[0].chunk_id, 2);
    CHECK_EQ(list[1].flags, CQ_FLAG_LAST);
    CHECK_EQ(list[1].duration_ms, 2400);

    /* Ids are never reused, even after everything is acked and the card remounted. */
    CHECK_EQ(cq_delete(&q, 2, &was), CQ_OK);
    CHECK_EQ(cq_delete(&q, 3, &was), CQ_OK);
    reopen_queue(&q, &rep);
    CHECK_EQ(q.count, 0);
    CHECK_EQ(add_chunk(&q, SESSION_B, 0, 10, true), 4);

    /* A torn `next` file falls back to the highest id on the card. */
    char path[512];
    snprintf(path, sizeof(path), "%s/next", g_dir);
    FILE *f = fopen(path, "r+b");
    fputc(0x7f, f);
    fclose(f);
    reopen_queue(&q, &rep);
    CHECK_EQ(q.next_id, 5);
    cq_deinit(&q);
}

static void test_recover_with_audio(void)
{
    clear_dir();
    chunk_queue_t q;
    cq_boot_report_t rep;
    CHECK_EQ(cq_init(&q, g_dir, &rep), CQ_OK);
    add_chunk(&q, SESSION_A, 0, 1500, false);
    const uint32_t id = begin(&q, SESSION_A, 1, 1792411230000ull, 0, 7, 0);
    packets(&q, 120); /* 2 pages written, 20 packets buffered */
    /* Half a page more lands on the card before the power goes. */
    const uint8_t half[100] = "OggS\0garbage";
    CHECK_EQ(cq_append(&q, half, sizeof(half)), CQ_OK);
    CHECK_EQ(cq_sync(&q), CQ_OK);
    const long cut_size = file_size(id, "opus");

    reopen_queue(&q, &rep); /* power cut: no cq_end */
    CHECK_EQ(rep.loaded, 1);
    CHECK_EQ(rep.recovered, 1);
    cq_info_t c;
    CHECK(cq_get(&q, id, &c));
    CHECK_EQ(c.flags, CQ_FLAG_LAST);
    CHECK_EQ(c.state, CQ_STATE_CLOSED);
    CHECK_EQ(c.sequence, 1);
    CHECK_EQ(c.start_ms, 1792411230000ull);
    CHECK_EQ(c.bytes, cut_size - (long)sizeof(half));
    CHECK_EQ(c.bytes, file_size(id, "opus"));
    CHECK_EQ(c.crc32, file_crc(id));
    CHECK_EQ(c.duration_ms, (100 * 960 - 312) / 48); /* what decodes: 1993 ms */

    /* And it stays that way on the next boot. */
    reopen_queue(&q, &rep);
    CHECK_EQ(rep.loaded, 2);
    CHECK_EQ(rep.recovered, 0);
    CHECK(cq_get(&q, id, &c) && c.flags == CQ_FLAG_LAST);
    cq_deinit(&q);
}

static void test_recover_without_audio(void)
{
    clear_dir();
    chunk_queue_t q;
    cq_boot_report_t rep;
    CHECK_EQ(cq_init(&q, g_dir, &rep), CQ_OK);
    const uint32_t a0 = add_chunk(&q, SESSION_A, 0, 1500, false);
    const uint32_t a1 = add_chunk(&q, SESSION_A, 1, 1500, false);
    const uint32_t b0 = add_chunk(&q, SESSION_B, 0, 50, false); /* another session, untouched */
    /* Cut while the next chunk of A holds only its headers and 30 buffered packets. */
    const uint32_t a2 = begin(&q, SESSION_A, 2, 0, 0, 7, 0);
    packets(&q, 30);
    reopen_queue(&q, &rep);
    CHECK_EQ(rep.discarded, 1);
    CHECK(!exists(a2, "opus") && !exists(a2, "met"));
    cq_info_t c;
    CHECK(!cq_get(&q, a2, &c));
    CHECK(cq_get(&q, a1, &c) && c.flags == CQ_FLAG_LAST); /* the session still ends */
    CHECK(cq_get(&q, a0, &c) && c.flags == 0);
    CHECK(cq_get(&q, b0, &c) && c.flags == 0);
    reopen_queue(&q, &rep);
    CHECK(cq_get(&q, a1, &c) && c.flags == CQ_FLAG_LAST); /* written to the card */

    /* A cut right after the record was written, before any audio file existed. */
    const uint32_t b1 = begin(&q, SESSION_B, 1, 0, 0, 7, 0);
    char path[512];
    path_of(b1, "opus", path, sizeof(path));
    remove(path);
    reopen_queue(&q, &rep);
    CHECK_EQ(rep.discarded, 1);
    CHECK(cq_get(&q, b0, &c) && c.flags == CQ_FLAG_LAST);
    CHECK(!exists(b1, "met"));
    /* The id isn't reused. */
    CHECK(q.next_id > b1);
    cq_deinit(&q);
}

static void test_abandon_and_set_last(void)
{
    clear_dir();
    chunk_queue_t q;
    CHECK_EQ(cq_init(&q, g_dir, NULL), CQ_OK);
    const uint32_t a0 = add_chunk(&q, SESSION_A, 0, 1500, false);
    /* A write error with audio on the card: kept, closed with LAST. */
    const uint32_t a1 = begin(&q, SESSION_A, 1, 0, 0, 7, 0);
    packets(&q, 60);
    uint32_t kept = 0;
    CHECK_EQ(cq_abandon(&q, true, &kept), CQ_OK);
    CHECK_EQ(kept, a1);
    cq_info_t c;
    CHECK(cq_get(&q, a1, &c) && c.flags == CQ_FLAG_LAST && c.bytes == file_size(a1, "opus"));
    CHECK(!cq_current(&q, NULL));
    CHECK_EQ(cq_abandon(&q, true, &kept), CQ_ERR_STATE);

    /* Without audio: deleted, LAST moves back. */
    const uint32_t b0 = add_chunk(&q, SESSION_B, 0, 1500, false);
    begin(&q, SESSION_B, 1, 0, 0, 7, 0);
    CHECK_EQ(cq_abandon(&q, true, &kept), CQ_OK);
    CHECK_EQ(kept, 0);
    CHECK(cq_get(&q, b0, &c) && c.flags == CQ_FLAG_LAST);

    /* The session goes on (last = false): kept without LAST, or deleted without moving
     * LAST back. */
    const uint32_t c0 = add_chunk(&q, SESSION_C, 0, 1500, false);
    const uint32_t c1 = begin(&q, SESSION_C, 1, 0, 0, 7, 0);
    packets(&q, 60);
    CHECK_EQ(cq_abandon(&q, false, &kept), CQ_OK);
    CHECK_EQ(kept, c1);
    CHECK(cq_get(&q, c1, &c) && c.flags == 0 && c.state == CQ_STATE_CLOSED);
    begin(&q, SESSION_C, 2, 0, 0, 7, 0);
    CHECK_EQ(cq_abandon(&q, false, &kept), CQ_OK);
    CHECK_EQ(kept, 0);
    CHECK(cq_get(&q, c1, &c) && c.flags == 0);
    CHECK(cq_get(&q, c0, &c) && c.flags == 0);

    /* In two steps (the card is power-cycled in between on the watch): the same result. */
    const uint32_t d0 = begin(&q, SESSION_D, 0, 0, 0, 7, 0);
    packets(&q, 60);
    CHECK_EQ(cq_abandon_recover(&q, true, &kept), CQ_ERR_STATE); /* not closed yet */
    CHECK_EQ(cq_abandon_close(&q), CQ_OK);
    CHECK(!cq_current(&q, NULL));
    CHECK_EQ(cq_abandon_close(&q), CQ_ERR_STATE);
    CHECK_EQ(cq_abandon_recover(&q, true, &kept), CQ_OK);
    CHECK_EQ(kept, d0);
    CHECK(cq_get(&q, d0, &c) && c.flags == CQ_FLAG_LAST && c.bytes == file_size(d0, "opus"));
    CHECK_EQ(cq_abandon_recover(&q, true, &kept), CQ_ERR_STATE); /* once */

    /* Closed but never recovered: a new chunk drops it from memory; the card keeps it and
     * the next boot recovers it. */
    const uint32_t d1 = begin(&q, SESSION_D, 1, 0, 0, 7, 0);
    packets(&q, 60);
    CHECK_EQ(cq_abandon_close(&q), CQ_OK);
    const uint32_t d2 = begin(&q, SESSION_D, 2, 0, 0, 7, 0);
    CHECK_EQ(cq_abandon_recover(&q, false, &kept), CQ_ERR_STATE);
    CHECK(!cq_get(&q, d1, NULL));
    packets(&q, 60);
    CHECK_EQ(ogg_opus_finish(&g_w, 0), OGG_OPUS_OK);
    CHECK_EQ(cq_end(&q, 1200, true), CQ_OK);
    cq_boot_report_t rep;
    reopen_queue(&q, &rep);
    CHECK(cq_get(&q, d1, &c) && c.state == CQ_STATE_CLOSED);
    CHECK(cq_get(&q, d2, &c));

    CHECK_EQ(cq_set_last(&q, a0), CQ_OK);
    CHECK(cq_get(&q, a0, &c) && c.flags == CQ_FLAG_LAST);
    CHECK_EQ(cq_set_last(&q, 999), CQ_ERR_NOT_FOUND);
    reopen_queue(&q, &rep);
    CHECK(cq_get(&q, a0, &c) && c.flags == CQ_FLAG_LAST);

    /* One chunk open at a time; ending or appending without one is refused. */
    begin(&q, SESSION_A, 5, 0, 0, 7, 0);
    cq_new_t n = {0};
    uint32_t id;
    CHECK_EQ(cq_begin(&q, &n, &id), CQ_ERR_STATE);
    packets(&q, 5);
    CHECK_EQ(ogg_opus_finish(&g_w, 0), OGG_OPUS_OK);
    CHECK_EQ(cq_end(&q, 100, true), CQ_OK);
    CHECK_EQ(cq_end(&q, 100, true), CQ_ERR_STATE);
    CHECK_EQ(cq_append(&q, "x", 1), CQ_ERR_STATE);
    cq_deinit(&q);
}

static void test_fix_time(void)
{
    clear_dir();
    chunk_queue_t q;
    CHECK_EQ(cq_init(&q, g_dir, NULL), CQ_OK);
    /* Session recorded without a clock in boot 7: start_ms is session-relative. */
    const uint32_t a = begin(&q, SESSION_A, 0, 0, CQ_FLAG_TIME_UNSET, 7, 1000);
    packets(&q, 10);
    CHECK_EQ(ogg_opus_finish(&g_w, 0), OGG_OPUS_OK);
    CHECK_EQ(cq_end(&q, 30000, false), CQ_OK);
    const uint32_t b = begin(&q, SESSION_A, 1, 30000, CQ_FLAG_TIME_UNSET, 7, 31000);
    packets(&q, 10);
    CHECK_EQ(ogg_opus_finish(&g_w, 0), OGG_OPUS_OK);
    CHECK_EQ(cq_end(&q, 30000, true), CQ_OK);
    /* One from an earlier boot: its monotonic clock means nothing now. */
    const uint32_t old = begin(&q, SESSION_B, 0, 0, CQ_FLAG_TIME_UNSET, 6, 500);
    packets(&q, 10);
    CHECK_EQ(ogg_opus_finish(&g_w, 0), OGG_OPUS_OK);
    CHECK_EQ(cq_end(&q, 200, true), CQ_OK);
    /* And one still being recorded. */
    const uint32_t open = begin(&q, SESSION_A, 2, 60000, CQ_FLAG_TIME_UNSET, 7, 61000);
    packets(&q, 60); /* one audio page on the card */

    const int64_t now_epoch = 1792411200000LL, now_mono = 70000;
    CHECK_EQ(cq_fix_time(&q, 7, now_epoch, now_mono), 2);
    cq_info_t c;
    CHECK(cq_get(&q, a, &c));
    CHECK_EQ(c.start_ms, (uint64_t)(now_epoch - 69000));
    CHECK_EQ(c.flags, 0);
    CHECK(cq_get(&q, b, &c));
    CHECK_EQ(c.start_ms, (uint64_t)(now_epoch - 39000));
    CHECK_EQ(c.flags, CQ_FLAG_LAST);
    CHECK(cq_get(&q, old, &c));
    CHECK_EQ(c.flags, CQ_FLAG_LAST | CQ_FLAG_TIME_UNSET);
    CHECK_EQ(c.start_ms, 0);
    CHECK(cq_current(&q, &c));
    CHECK_EQ(c.start_ms, (uint64_t)(now_epoch - 9000));
    CHECK_EQ(c.flags, 0);
    /* Nothing left to convert. */
    CHECK_EQ(cq_fix_time(&q, 7, now_epoch, now_mono), 0);

    /* Converted records are on the card, the open one too (recovered after a cut). */
    cq_boot_report_t rep;
    reopen_queue(&q, &rep);
    CHECK(cq_get(&q, b, &c) && c.start_ms == (uint64_t)(now_epoch - 39000) && c.flags == CQ_FLAG_LAST);
    CHECK(cq_get(&q, open, &c) && c.start_ms == (uint64_t)(now_epoch - 9000));
    CHECK_EQ(c.flags, CQ_FLAG_LAST);
    cq_deinit(&q);
}

/* The NOTE flag (voice notes) is kept by cq_begin, gets LAST added by cq_end and by recovery,
 * survives cq_fix_time and a reload from the card; ordinary chunks never get it. */
static void test_note_flag(void)
{
    clear_dir();
    chunk_queue_t q;
    CHECK_EQ(cq_init(&q, g_dir, NULL), CQ_OK);
    /* An ordinary conversation chunk. */
    const uint32_t conv = begin(&q, SESSION_B, 0, 1792411200000ull, 0, 7, 0);
    packets(&q, 10);
    CHECK_EQ(ogg_opus_finish(&g_w, 0), OGG_OPUS_OK);
    CHECK_EQ(cq_end(&q, 200, true), CQ_OK);
    /* A note recorded without a clock; a LAST passed to cq_begin is not taken over. */
    const uint32_t n0 = begin(&q, SESSION_A, 0, 0, CQ_FLAG_NOTE | CQ_FLAG_TIME_UNSET | CQ_FLAG_LAST,
                              7, 1000);
    cq_info_t c;
    CHECK(cq_current(&q, &c));
    CHECK_EQ(c.flags, CQ_FLAG_NOTE | CQ_FLAG_TIME_UNSET);
    packets(&q, 10);
    CHECK_EQ(ogg_opus_finish(&g_w, 0), OGG_OPUS_OK);
    CHECK_EQ(cq_end(&q, 30000, false), CQ_OK);
    const uint32_t n1 = begin(&q, SESSION_A, 1, 30000, CQ_FLAG_NOTE | CQ_FLAG_TIME_UNSET, 7, 31000);
    packets(&q, 10);
    CHECK_EQ(ogg_opus_finish(&g_w, 0), OGG_OPUS_OK);
    CHECK_EQ(cq_end(&q, 12340, true), CQ_OK);
    CHECK(cq_get(&q, conv, &c));
    CHECK_EQ(c.flags, CQ_FLAG_LAST);
    CHECK(cq_get(&q, n0, &c));
    CHECK_EQ(c.flags, CQ_FLAG_NOTE | CQ_FLAG_TIME_UNSET);
    CHECK(cq_get(&q, n1, &c));
    CHECK_EQ(c.flags, CQ_FLAG_NOTE | CQ_FLAG_TIME_UNSET | CQ_FLAG_LAST);

    /* Reload from the card (cq_deinit + cq_init). */
    cq_boot_report_t rep;
    reopen_queue(&q, &rep);
    CHECK_EQ(rep.loaded, 3);
    CHECK(cq_get(&q, conv, &c));
    CHECK_EQ(c.flags, CQ_FLAG_LAST);
    CHECK(cq_get(&q, n0, &c));
    CHECK_EQ(c.flags, CQ_FLAG_NOTE | CQ_FLAG_TIME_UNSET);
    CHECK(cq_get(&q, n1, &c));
    CHECK_EQ(c.flags, CQ_FLAG_NOTE | CQ_FLAG_TIME_UNSET | CQ_FLAG_LAST);

    /* The clock is set: TIME_UNSET goes, NOTE stays, on the card too. */
    CHECK_EQ(cq_fix_time(&q, 7, 1792411260000LL, 70000), 2);
    CHECK(cq_get(&q, n0, &c));
    CHECK_EQ(c.flags, CQ_FLAG_NOTE);
    CHECK(cq_get(&q, n1, &c));
    CHECK_EQ(c.flags, CQ_FLAG_NOTE | CQ_FLAG_LAST);

    /* A note chunk cut by a power loss is recovered with LAST and keeps NOTE. */
    const uint8_t session_c[8] = {9, 9, 9, 9, 9, 9, 9, 9};
    const uint32_t cut = begin(&q, session_c, 0, 1792411300000ull, CQ_FLAG_NOTE, 7, 80000);
    packets(&q, 60); /* one audio page on the card */
    reopen_queue(&q, &rep);
    CHECK_EQ(rep.recovered, 1);
    CHECK(cq_get(&q, n0, &c));
    CHECK_EQ(c.flags, CQ_FLAG_NOTE);
    CHECK(cq_get(&q, n1, &c));
    CHECK_EQ(c.flags, CQ_FLAG_NOTE | CQ_FLAG_LAST);
    CHECK(cq_get(&q, cut, &c));
    CHECK_EQ(c.flags, CQ_FLAG_NOTE | CQ_FLAG_LAST);
    CHECK(cq_get(&q, conv, &c));
    CHECK_EQ(c.flags, CQ_FLAG_LAST);
    cq_deinit(&q);
}

static void test_torn_records(void)
{
    clear_dir();
    chunk_queue_t q;
    cq_boot_report_t rep;
    CHECK_EQ(cq_init(&q, g_dir, &rep), CQ_OK);
    const uint32_t a = add_chunk(&q, SESSION_A, 0, 100, false);
    const uint32_t b = add_chunk(&q, SESSION_A, 1, 100, true);
    const uint32_t c = add_chunk(&q, SESSION_B, 0, 100, true);
    const uint32_t d = add_chunk(&q, SESSION_B, 1, 100, true);
    cq_deinit(&q);

    /* a: torn record, audio intact -> kept on the card but not listed. */
    poke(a, "met", 30, 0x40);
    /* b: record without audio (an ack cut between the two deletes) -> cleaned. */
    char path[512];
    path_of(b, "opus", path, sizeof(path));
    remove(path);
    /* c: torn record and an audio file with only its header pages -> cleaned. */
    poke(c, "met", 5, 0x01);
    path_of(c, "opus", path, sizeof(path));
    truncate(path, 60);
    /* d: audio without any record, e.g. a stray file -> kept, not listed. */
    path_of(d, "met", path, sizeof(path));
    remove(path);
    /* Files that aren't ours are ignored. */
    snprintf(path, sizeof(path), "%s/notes.txt", g_dir);
    FILE *f = fopen(path, "w");
    fputs("hello", f);
    fclose(f);

    CHECK_EQ(cq_init(&q, g_dir, &rep), CQ_OK);
    CHECK_EQ(q.count, 0);
    CHECK_EQ(rep.corrupt, 2);
    CHECK_EQ(rep.cleaned, 2);
    CHECK(exists(a, "opus") && exists(a, "met"));
    CHECK(!exists(b, "met"));
    CHECK(!exists(c, "opus") && !exists(c, "met"));
    CHECK(exists(d, "opus"));
    CHECK(q.next_id > d);
    cq_deinit(&q);
}

/* Turn a chunk's file into an (empty) directory: fopen() still works, but every read or
 * write fails with EISDIR, as a card that stopped answering fails with EIO. remove() takes
 * an empty directory too, so a file the queue wrongly deletes is gone afterwards. */
static void make_unreadable(uint32_t id, const char *ext)
{
    char path[512];
    path_of(id, ext, path, sizeof(path));
    remove(path);
    CHECK_EQ(mkdir(path, 0777), 0);
}

/* QC R3: a read error at boot (scan or record) must never delete a chunk: it is left on the
 * card for the next boot, counted as corrupt. */
static void test_boot_read_errors_keep_files(void)
{
    clear_dir();
    chunk_queue_t q;
    cq_boot_report_t rep;
    CHECK_EQ(cq_init(&q, g_dir, &rep), CQ_OK);
    const uint32_t a = add_chunk(&q, SESSION_A, 0, 100, false);
    const uint32_t b = add_chunk(&q, SESSION_A, 1, 100, true);
    const uint32_t c = add_chunk(&q, SESSION_B, 0, 100, true);
    cq_deinit(&q);

    /* a: torn record, and its audio can't be read right now. */
    poke(a, "met", 30, 0x40);
    make_unreadable(a, "opus");
    /* b: audio without a record, and the audio can't be read. */
    char path[512];
    path_of(b, "met", path, sizeof(path));
    remove(path);
    make_unreadable(b, "opus");
    /* c: the record itself can't be read (its audio is fine). */
    make_unreadable(c, "met");

    CHECK_EQ(cq_init(&q, g_dir, &rep), CQ_OK);
    CHECK_EQ(q.count, 0);
    CHECK_EQ(rep.cleaned, 0);
    CHECK_EQ(rep.corrupt, 3);
    CHECK(exists(a, "met") && exists(a, "opus"));
    CHECK(exists(b, "opus"));
    CHECK(exists(c, "met") && exists(c, "opus"));
    cq_deinit(&q);
}

/* QC R2: a chunk left open on the card (its close and recovery failed) while the session
 * went on is recovered at boot without LAST, and gives no LAST to an earlier chunk either:
 * the session has later chunks. */
static void test_recover_open_mid_session(void)
{
    clear_dir();
    chunk_queue_t q;
    cq_boot_report_t rep;
    CHECK_EQ(cq_init(&q, g_dir, &rep), CQ_OK);
    /* Session A: chunk 0 is left open with audio, chunk 1 follows and ends the session. */
    const uint32_t a0 = begin(&q, SESSION_A, 0, 1792411200000ull, 0, 7, 0);
    packets(&q, 120);
    CHECK_EQ(cq_abandon_close(&q), CQ_OK); /* the card stopped answering: no recovery */
    const uint32_t a1 = add_chunk(&q, SESSION_A, 1, 100, true);
    /* Session B: chunk 4 closed, chunk 5 left open without audio, chunk 6 closed. */
    const uint32_t b4 = add_chunk(&q, SESSION_B, 4, 100, false);
    const uint32_t b5 = begin(&q, SESSION_B, 5, 0, 0, 7, 0);
    CHECK_EQ(cq_abandon_close(&q), CQ_OK);
    const uint32_t b6 = add_chunk(&q, SESSION_B, 6, 100, false);
    /* Session D: an open chunk without audio after a closed one: the closed one gets LAST. */
    const uint32_t d0 = add_chunk(&q, SESSION_D, 0, 100, false);
    /* Session C: chunk 0 closed, chunk 1 open (a power cut): its LAST stays. */
    const uint32_t c0 = add_chunk(&q, SESSION_C, 0, 100, false);
    const uint32_t c1 = begin(&q, SESSION_C, 1, 0, 0, 7, 0);
    packets(&q, 120);
    cq_info_t c;
    reopen_queue(&q, &rep); /* c1 was still open: closes it by "power cut" */
    const uint32_t d1 = begin(&q, SESSION_D, 1, 0, 0, 7, 0);
    CHECK_EQ(cq_abandon_close(&q), CQ_OK);
    reopen_queue(&q, &rep);

    CHECK(cq_get(&q, a0, &c));
    CHECK_EQ(c.flags, 0); /* recovered, but not the session's end */
    CHECK(cq_get(&q, a1, &c) && c.flags == CQ_FLAG_LAST);
    CHECK(!exists(b5, "met"));
    CHECK(cq_get(&q, b4, &c) && c.flags == 0);
    CHECK(cq_get(&q, b6, &c) && c.flags == 0);
    CHECK(cq_get(&q, c1, &c) && c.flags == CQ_FLAG_LAST);
    CHECK(cq_get(&q, c0, &c) && c.flags == 0);
    CHECK(!exists(d1, "met"));
    CHECK(cq_get(&q, d0, &c) && c.flags == CQ_FLAG_LAST);
    cq_deinit(&q);
}

/* QC R11: a TIME fix-up whose record write fails leaves the chunk as it is on the card
 * (TIME_UNSET), so the next TIME tries again. */
static void test_fix_time_write_error(void)
{
    clear_dir();
    chunk_queue_t q;
    CHECK_EQ(cq_init(&q, g_dir, NULL), CQ_OK);
    const uint32_t a = begin(&q, SESSION_A, 0, 0, CQ_FLAG_TIME_UNSET, 7, 1000);
    packets(&q, 10);
    CHECK_EQ(ogg_opus_finish(&g_w, 0), OGG_OPUS_OK);
    CHECK_EQ(cq_end(&q, 200, true), CQ_OK);
    const uint32_t b = begin(&q, SESSION_A, 1, 0, CQ_FLAG_TIME_UNSET, 7, 2000);
    packets(&q, 60);

    uint8_t saved[CQ_RECORD_SIZE];
    char path[512];
    path_of(a, "met", path, sizeof(path));
    FILE *f = fopen(path, "rb");
    CHECK(f && fread(saved, 1, sizeof(saved), f) == sizeof(saved));
    fclose(f);
    make_unreadable(a, "met");
    make_unreadable(b, "met");

    const int64_t now_epoch = 1792411200000LL, now_mono = 70000;
    CHECK(cq_fix_time(&q, 7, now_epoch, now_mono) < 0);
    cq_info_t c;
    CHECK(cq_get(&q, a, &c));
    CHECK_EQ(c.flags, CQ_FLAG_LAST | CQ_FLAG_TIME_UNSET);
    CHECK_EQ(c.start_ms, 0);
    CHECK(cq_current(&q, &c));
    CHECK_EQ(c.flags, CQ_FLAG_TIME_UNSET);

    /* The card answers again: the next TIME converts both. */
    remove(path);
    f = fopen(path, "wb");
    fwrite(saved, 1, sizeof(saved), f);
    fclose(f);
    path_of(b, "met", path, sizeof(path));
    remove(path);
    CHECK_EQ(cq_fix_time(&q, 7, now_epoch, now_mono), 1); /* the open chunk isn't counted */
    CHECK(cq_get(&q, a, &c));
    CHECK_EQ(c.flags, CQ_FLAG_LAST);
    CHECK_EQ(c.start_ms, (uint64_t)(now_epoch - 69000));
    CHECK(cq_current(&q, &c));
    CHECK_EQ(c.flags, 0);
    cq_boot_report_t rep;
    reopen_queue(&q, &rep);
    CHECK(cq_get(&q, a, &c) && c.flags == CQ_FLAG_LAST);
    CHECK(cq_get(&q, b, &c) && c.start_ms == (uint64_t)(now_epoch - 68000));
    cq_deinit(&q);
}

/* QC R12: chunk id 0 is never valid (LIST starts after 0), and the ids don't wrap around
 * to 0 and then onto chunks still queued: a full id space refuses new chunks. */
static void test_chunk_id_zero_and_wrap(void)
{
    cq_info_t in = {.chunk_id = 0, .sequence = 1, .state = CQ_STATE_CLOSED}, out;
    uint8_t rec[CQ_RECORD_SIZE];
    cq_record_encode(&in, rec);
    CHECK(!cq_record_decode(rec, &out));

    clear_dir();
    chunk_queue_t q;
    cq_boot_report_t rep;
    CHECK_EQ(cq_init(&q, g_dir, &rep), CQ_OK);
    const uint32_t a = add_chunk(&q, SESSION_A, 0, 100, true);
    cq_deinit(&q);
    /* A stray file with the highest id (copied onto the card, or a corrupt directory). */
    char path[512];
    path_of(UINT32_MAX, "met", path, sizeof(path));
    FILE *f = fopen(path, "wb");
    fputs("not a record", f);
    fclose(f);
    CHECK_EQ(cq_init(&q, g_dir, &rep), CQ_OK);
    CHECK_EQ(rep.cleaned, 1);
    cq_new_t n = {.sequence = 0};
    memcpy(n.session_id, SESSION_B, 8);
    uint32_t id = 12345;
    CHECK(cq_begin(&q, &n, &id) != CQ_OK);
    CHECK_EQ(id, 12345);
    CHECK(!cq_current(&q, NULL));
    cq_info_t c;
    CHECK(cq_get(&q, a, &c) && c.flags == CQ_FLAG_LAST && c.sequence == 0); /* untouched */
    CHECK(!exists(0, "met"));
    cq_deinit(&q);

    /* The last id itself is handed out once. */
    clear_dir();
    CHECK_EQ(cq_init(&q, g_dir, &rep), CQ_OK);
    q.next_id = UINT32_MAX;
    CHECK_EQ(cq_begin(&q, &n, &id), CQ_OK);
    CHECK_EQ(id, UINT32_MAX);
    CHECK_EQ(cq_abandon(&q, true, &id), CQ_OK);
    CHECK(cq_begin(&q, &n, &id) != CQ_OK);
    cq_deinit(&q);
}

/* `queue drop <id>`: strtoul(base 0) read the zero-padded hex ids `queue` printed as octal,
 * so "00000010" (chunk 16) dropped chunk 8. Decimal or 0x-hex only, never a leading 0. */
static void test_parse_id(void)
{
    uint32_t id = 0;
    CHECK(cq_parse_id("16", &id));
    CHECK_EQ(id, 16);
    CHECK(cq_parse_id("0x10", &id));
    CHECK_EQ(id, 16);
    CHECK(cq_parse_id("0X1a", &id));
    CHECK_EQ(id, 26);
    CHECK(cq_parse_id("4294967295", &id));
    CHECK_EQ(id, UINT32_MAX);
    id = 7;
    CHECK(!cq_parse_id("00000010", &id)); /* not octal 8, not decimal 10: refused */
    CHECK(!cq_parse_id("00000017", &id));
    CHECK(!cq_parse_id("0", &id));
    CHECK(!cq_parse_id("0x", &id));
    CHECK(!cq_parse_id("0x0", &id));
    CHECK(!cq_parse_id("", &id));
    CHECK(!cq_parse_id("1a", &id));
    CHECK(!cq_parse_id("-1", &id));
    CHECK(!cq_parse_id("4294967296", &id));
    CHECK(!cq_parse_id("0x100000000", &id));
    CHECK_EQ(id, 7); /* untouched on failure */
}

int main(void)
{
    const char *tmp = getenv("TEST_TMPDIR");
    snprintf(g_dir, sizeof(g_dir), "%s/q", tmp ? tmp : "/tmp/test_chunk_queue");
    char parent[256];
    snprintf(parent, sizeof(parent), "%s", tmp ? tmp : "/tmp/test_chunk_queue");
    mkdir(parent, 0777);

    test_record();
    test_parse_id();
    test_basic();
    test_recover_with_audio();
    test_recover_without_audio();
    test_abandon_and_set_last();
    test_fix_time();
    test_note_flag();
    test_torn_records();
    test_boot_read_errors_keep_files();
    test_recover_open_mid_session();
    test_fix_time_write_error();
    test_chunk_id_zero_and_wrap();
    return checks_done("test_chunk_queue");
}
