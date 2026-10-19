/* Ogg/Opus writer and scanner. The page layout is checked by an independent parser below,
 * the first pages against bytes computed outside this code (Python, from RFC 3533/7845).
 * If ffmpeg is on PATH, a remux of real libopus packets is also decoded by ffmpeg. */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "check.h"
#include "ogg_opus.h"

/* ---- a memory sink for the writer ---- */

typedef struct {
    uint8_t *data;
    size_t len, cap;
    int calls;
    int fail_at; /* make this call fail (1-based), 0 = never */
} sink_t;

static int sink_write(void *ctx, const void *data, size_t len)
{
    sink_t *s = ctx;
    if (++s->calls == s->fail_at) {
        return -1;
    }
    if (s->len + len > s->cap) {
        s->cap = (s->len + len) * 2;
        s->data = realloc(s->data, s->cap);
    }
    memcpy(s->data + s->len, data, len);
    s->len += len;
    return 0;
}

typedef struct {
    const uint8_t *data;
    size_t len;
} mem_t;

static int mem_read(void *ctx, uint64_t off, void *buf, size_t len)
{
    const mem_t *m = ctx;
    if (off >= m->len) {
        return 0;
    }
    if (len > m->len - off) {
        len = (size_t)(m->len - off);
    }
    /* Short reads on purpose, to exercise the scanner's read loop. */
    if (len > 100) {
        len = 100;
    }
    memcpy(buf, m->data + off, len);
    return (int)len;
}

/* ---- an independent page parser ---- */

typedef struct {
    size_t off, len;
    uint8_t flags;
    int64_t granule;
    uint32_t serial, seq;
    int nsegs;
    const uint8_t *segs;
    const uint8_t *body;
    size_t body_len;
    bool crc_ok;
} page_t;

static uint32_t rd32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

/* Plain bitwise CRC, so the test doesn't depend on the table in ogg_opus.c. */
static uint32_t slow_crc(const uint8_t *p, size_t n, size_t crc_at)
{
    uint32_t crc = 0;
    for (size_t i = 0; i < n; i++) {
        const uint8_t b = (i >= crc_at && i < crc_at + 4) ? 0 : p[i];
        crc ^= (uint32_t)b << 24;
        for (int k = 0; k < 8; k++) {
            crc = (crc & 0x80000000u) ? (crc << 1) ^ 0x04C11DB7u : crc << 1;
        }
    }
    return crc;
}

static int parse_pages(const uint8_t *d, size_t len, page_t *pages, int max)
{
    int n = 0;
    size_t off = 0;
    while (off + 27 <= len && n < max) {
        page_t *p = &pages[n];
        if (memcmp(d + off, "OggS", 4) != 0) {
            break;
        }
        p->off = off;
        p->flags = d[off + 5];
        p->granule = (int64_t)((uint64_t)rd32(d + off + 6) | (uint64_t)rd32(d + off + 10) << 32);
        p->serial = rd32(d + off + 14);
        p->seq = rd32(d + off + 18);
        p->nsegs = d[off + 26];
        p->segs = d + off + 27;
        p->body_len = 0;
        for (int i = 0; i < p->nsegs; i++) {
            p->body_len += p->segs[i];
        }
        p->body = p->segs + p->nsegs;
        p->len = 27 + (size_t)p->nsegs + p->body_len;
        if (off + p->len > len) {
            break;
        }
        p->crc_ok = slow_crc(d + off, p->len, 22) == rd32(d + off + 22);
        off += p->len;
        n++;
    }
    return n;
}

/* Packets of a page that start and end on it (our writer never spans pages). */
static int page_packets(const page_t *p, const uint8_t **pkt, size_t *len, int max)
{
    int n = 0;
    size_t cur = 0, start = 0;
    for (int i = 0; i < p->nsegs && n < max; i++) {
        cur += p->segs[i];
        if (p->segs[i] < 255) {
            pkt[n] = p->body + start;
            len[n] = cur - start;
            start = cur;
            n++;
        }
    }
    return n;
}

static const char *const COMMENTS[] = {"BARVIS_SESSION=3f9a0c11d2e4b507", "BARVIS_SEQUENCE=2",
                                       "BARVIS_START_MS=1792411260000"};

static ogg_opus_head_t test_head(void)
{
    ogg_opus_head_t h = {.serial = 0x1234abcd, .pre_skip = 312, .input_rate = 16000};
    for (int i = 0; i < 3; i++) {
        h.comments[i] = COMMENTS[i];
    }
    return h;
}

static size_t unhex(const char *hex, uint8_t *out)
{
    size_t n = 0;
    for (; hex[0] && hex[1]; hex += 2) {
        unsigned v;
        sscanf(hex, "%2x", &v);
        out[n++] = (uint8_t)v;
    }
    return n;
}

static void test_crc(void)
{
    CHECK_EQ(ogg_crc_update(0, "123456789", 9), 0x89A1897Fu);
}

static void test_header_pages(void)
{
    static ogg_opus_writer_t w;
    sink_t s = {0};
    ogg_opus_head_t h = test_head();
    CHECK_EQ(ogg_opus_begin(&w, &h, sink_write, &s), OGG_OPUS_OK);
    CHECK_EQ(s.calls, 2); /* one write per page */

    /* Computed with Python from RFC 7845 §5 and RFC 3533 §6. */
    uint8_t want[512];
    size_t n = unhex("4f67675300020000000000000000cdab341200000000967d4e4c01134f707573486561640101"
                     "3801803e0000000000",
                     want);
    n += unhex("4f67675300000000000000000000cdab341201000000f6178a54017e4f70757354616773150000"
               "006261727669732d736f756e64207265636f72646572030000001f0000004241525649535f534553"
               "53494f4e3d33663961306331316432653462353037110000004241525649535f53455155454e4345"
               "3d321d0000004241525649535f53544152545f4d533d31373932343131323630303030",
               want + n);
    CHECK_EQ(s.len, n);
    CHECK(s.len == n && memcmp(s.data, want, n) == 0);
    free(s.data);
}

/* Packets of assorted sizes, including the lacing edge cases 254/255/256/510. */
static size_t packet_len(int i)
{
    static const size_t special[] = {1, 254, 255, 256, 510, 511, 1275};
    return i < 7 ? special[i] : (size_t)(20 + (i * 37) % 90);
}

static void fill_packet(uint8_t *p, size_t len, int i)
{
    for (size_t k = 0; k < len; k++) {
        p[k] = (uint8_t)(i * 7 + k);
    }
}

static void test_audio_pages(void)
{
    static ogg_opus_writer_t w;
    sink_t s = {0};
    ogg_opus_head_t h = test_head();
    CHECK_EQ(ogg_opus_begin(&w, &h, sink_write, &s), OGG_OPUS_OK);
    const int total = 123;
    uint8_t pkt[OGG_OPUS_MAX_PACKET];
    for (int i = 0; i < total; i++) {
        fill_packet(pkt, packet_len(i), i);
        CHECK_EQ(ogg_opus_add_packet(&w, pkt, packet_len(i), 960), OGG_OPUS_OK);
    }
    /* 123 packets = 2 full pages written, 23 packets still buffered. */
    CHECK_EQ(s.calls, 4);
    CHECK_EQ(ogg_opus_finish(&w, 648), OGG_OPUS_OK);
    CHECK_EQ(s.calls, 5);
    CHECK_EQ(w.bytes, s.len);
    CHECK_EQ(ogg_opus_add_packet(&w, pkt, 10, 960), OGG_OPUS_ERR_STATE);

    page_t pages[16];
    const int np = parse_pages(s.data, s.len, pages, 16);
    CHECK_EQ(np, 5);
    int seen = 0;
    for (int i = 0; i < np; i++) {
        const page_t *p = &pages[i];
        CHECK(p->crc_ok);
        CHECK_EQ(p->seq, (uint32_t)i);
        CHECK_EQ(p->serial, 0x1234abcdu);
        CHECK_EQ(p->flags, i == 0 ? 0x02 : i == np - 1 ? 0x04 : 0);
        if (i < 2) {
            CHECK_EQ(p->granule, 0);
            continue;
        }
        const uint8_t *pk[64];
        size_t pl[64];
        const int k = page_packets(p, pk, pl, 64);
        CHECK(k <= OGG_OPUS_PAGE_PACKETS);
        CHECK_EQ(k, i < np - 1 ? 50 : 23);
        CHECK(p->segs[p->nsegs - 1] < 255); /* no packet continues on the next page */
        for (int j = 0; j < k; j++, seen++) {
            uint8_t expect[OGG_OPUS_MAX_PACKET];
            fill_packet(expect, packet_len(seen), seen);
            CHECK_EQ(pl[j], packet_len(seen));
            CHECK(pl[j] == packet_len(seen) && memcmp(pk[j], expect, pl[j]) == 0);
        }
        /* Granule = 48 kHz samples decoded through this page (pre-skip included); the last
         * page is end-trimmed. */
        CHECK_EQ(p->granule, i < np - 1 ? 960LL * seen : 960LL * seen - 648);
    }
    CHECK_EQ(seen, total);
    /* Lacing: 255 -> {255, 0}, 510 -> {255, 255, 0}. */
    CHECK_EQ(pages[2].segs[1], 254);
    CHECK_EQ(pages[2].segs[2], 255);
    CHECK_EQ(pages[2].segs[3], 0);
    CHECK_EQ(pages[2].segs[4], 255);
    CHECK_EQ(pages[2].segs[5], 1);
    CHECK_EQ(pages[2].segs[6], 255);
    CHECK_EQ(pages[2].segs[7], 255);
    CHECK_EQ(pages[2].segs[8], 0);

    /* The scanner agrees on the complete file. */
    ogg_opus_scan_t sc;
    mem_t m = {s.data, s.len};
    CHECK_EQ(ogg_opus_scan(mem_read, &m, &sc), OGG_OPUS_OK);
    CHECK_EQ(sc.valid_bytes, s.len);
    CHECK_EQ(sc.pages, 5);
    CHECK(sc.has_head && sc.has_audio && sc.eos);
    CHECK_EQ(sc.pre_skip, 312);
    CHECK_EQ(sc.granule, 960LL * total - 648);
    CHECK_EQ(sc.audio_packets, total);

    /* Cut anywhere: valid_bytes is the end of the last whole page before the cut, and
     * there is audio once the first audio page is complete. */
    for (size_t cut = 0; cut <= s.len; cut++) {
        size_t expect = 0;
        int whole = 0;
        for (int i = 0; i < np; i++) {
            if (pages[i].off + pages[i].len <= cut) {
                expect = pages[i].off + pages[i].len;
                whole = i + 1;
            }
        }
        m.len = cut;
        CHECK_EQ(ogg_opus_scan(mem_read, &m, &sc), OGG_OPUS_OK);
        CHECK_EQ(sc.valid_bytes, expect);
        CHECK_EQ(sc.has_audio, whole >= 3);
        CHECK_EQ(sc.has_head, whole >= 1);
        if (whole == 3) {
            CHECK_EQ(sc.granule, 960 * 50);
            CHECK_EQ(sc.audio_packets, 50);
            CHECK(!sc.eos);
        }
    }

    /* A flipped bit in page 3 (the second audio page) stops the scan after page 2. */
    s.data[pages[3].off + 40] ^= 0x10;
    m.len = s.len;
    CHECK_EQ(ogg_opus_scan(mem_read, &m, &sc), OGG_OPUS_OK);
    CHECK_EQ(sc.valid_bytes, pages[3].off);
    CHECK_EQ(sc.audio_packets, 50);
    s.data[pages[3].off + 40] ^= 0x10;

    /* A page out of sequence (a stale page left after a cut) ends the stream too. */
    m.len = s.len;
    uint8_t *copy = malloc(s.len);
    memcpy(copy, s.data, s.len);
    memcpy(copy + pages[3].off, s.data + pages[2].off, pages[2].len < pages[3].len ? pages[2].len : pages[3].len);
    m.data = copy;
    CHECK_EQ(ogg_opus_scan(mem_read, &m, &sc), OGG_OPUS_OK);
    CHECK_EQ(sc.valid_bytes, pages[3].off);
    free(copy);

    /* Not an Opus stream at all. */
    const uint8_t junk[64] = "OggS but not really";
    m.data = junk;
    m.len = sizeof(junk);
    CHECK_EQ(ogg_opus_scan(mem_read, &m, &sc), OGG_OPUS_OK);
    CHECK_EQ(sc.valid_bytes, 0);
    CHECK(!sc.has_head);
    free(s.data);
}

static void test_page_limits(void)
{
    static ogg_opus_writer_t w;
    sink_t s = {0};
    ogg_opus_head_t h = test_head();
    CHECK_EQ(ogg_opus_begin(&w, &h, sink_write, &s), OGG_OPUS_OK);
    uint8_t pkt[OGG_OPUS_MAX_PACKET] = {0};
    /* Big packets: 6 x 1275 bytes fill the 8 KB body (and 36 lacing values), the 7th goes
     * on a new page. */
    for (int i = 0; i < 13; i++) {
        CHECK_EQ(ogg_opus_add_packet(&w, pkt, sizeof(pkt), 960), OGG_OPUS_OK);
    }
    CHECK_EQ(ogg_opus_finish(&w, 0), OGG_OPUS_OK);
    page_t pages[8];
    const int np = parse_pages(s.data, s.len, pages, 8);
    CHECK_EQ(np, 5);
    const uint8_t *pk[64];
    size_t pl[64];
    CHECK_EQ(page_packets(&pages[2], pk, pl, 64), 6);
    CHECK_EQ(page_packets(&pages[3], pk, pl, 64), 6);
    CHECK_EQ(page_packets(&pages[4], pk, pl, 64), 1);
    CHECK_EQ(pages[4].granule, 13 * 960);
    free(s.data);

    /* Tiny packets: the 50-packet limit splits the pages. (With an 8 KB body and 50 packets
     * a page needs at most ~82 lacing values, so the 255 limit is only a safety net.) */
    memset(&s, 0, sizeof(s));
    CHECK_EQ(ogg_opus_begin(&w, &h, sink_write, &s), OGG_OPUS_OK);
    for (int i = 0; i < 101; i++) {
        CHECK_EQ(ogg_opus_add_packet(&w, pkt, 1, 960), OGG_OPUS_OK);
    }
    CHECK_EQ(ogg_opus_finish(&w, 0), OGG_OPUS_OK);
    CHECK_EQ(parse_pages(s.data, s.len, pages, 8), 5);
    CHECK_EQ(pages[2].nsegs, 50);
    CHECK_EQ(pages[4].nsegs, 1);
    free(s.data);
}

static void test_errors(void)
{
    static ogg_opus_writer_t w;
    sink_t s = {0};
    ogg_opus_head_t h = test_head();
    uint8_t pkt[OGG_OPUS_MAX_PACKET + 1] = {0};

    CHECK_EQ(ogg_opus_add_packet(&w, pkt, 10, 960) != OGG_OPUS_OK, 1);
    CHECK_EQ(ogg_opus_begin(&w, &h, sink_write, &s), OGG_OPUS_OK);
    CHECK_EQ(ogg_opus_finish(&w, 0), OGG_OPUS_ERR_STATE); /* no audio yet */
    CHECK_EQ(ogg_opus_add_packet(&w, pkt, 0, 960), OGG_OPUS_ERR_ARG);
    CHECK_EQ(ogg_opus_add_packet(&w, pkt, sizeof(pkt), 960), OGG_OPUS_ERR_ARG);
    CHECK_EQ(ogg_opus_add_packet(&w, pkt, 10, 960), OGG_OPUS_OK);
    CHECK_EQ(ogg_opus_finish(&w, 961), OGG_OPUS_ERR_ARG); /* more than the last page holds */
    CHECK_EQ(ogg_opus_flush(&w), OGG_OPUS_OK);
    CHECK_EQ(ogg_opus_finish(&w, 0), OGG_OPUS_ERR_STATE); /* flushed: nothing buffered */
    free(s.data);

    /* A failed write stops the writer. */
    memset(&s, 0, sizeof(s));
    s.fail_at = 3;
    CHECK_EQ(ogg_opus_begin(&w, &h, sink_write, &s), OGG_OPUS_OK);
    for (int i = 0; i < 50; i++) {
        CHECK_EQ(ogg_opus_add_packet(&w, pkt, 10, 960), OGG_OPUS_OK);
    }
    CHECK_EQ(ogg_opus_add_packet(&w, pkt, 10, 960), OGG_OPUS_ERR_WRITE);
    CHECK_EQ(ogg_opus_add_packet(&w, pkt, 10, 960), OGG_OPUS_ERR_STATE);
    free(s.data);

    s.fail_at = 1;
    s.calls = 0;
    CHECK_EQ(ogg_opus_begin(&w, &h, sink_write, &s), OGG_OPUS_ERR_WRITE);
}

/* ---- optional: real Opus packets through ffmpeg ---- */

#ifdef __unix__
static bool run(const char *cmd, char *out, size_t out_len)
{
    FILE *p = popen(cmd, "r");
    if (!p) {
        return false;
    }
    size_t n = fread(out, 1, out_len - 1, p);
    out[n] = '\0';
    return pclose(p) == 0;
}

static uint8_t *read_file(const char *path, size_t *len)
{
    FILE *f = fopen(path, "rb");
    if (!f) {
        return NULL;
    }
    fseek(f, 0, SEEK_END);
    *len = (size_t)ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *d = malloc(*len ? *len : 1);
    if (fread(d, 1, *len, f) != *len) {
        free(d);
        d = NULL;
    }
    fclose(f);
    return d;
}

static int file_write(void *ctx, const void *data, size_t len)
{
    return fwrite(data, 1, len, ctx) == len ? 0 : -1;
}

/* Samples (48 kHz) the decoder outputs after pre-skip and end trimming: the real length. */
static long decoded_samples(const char *path)
{
    char cmd[512];
    snprintf(cmd, sizeof(cmd), "ffmpeg -nostdin -v error -i '%s' -f s16le -ac 1 -ar 48000 -", path);
    FILE *p = popen(cmd, "r");
    if (!p) {
        return -1;
    }
    static char buf[65536];
    long bytes = 0;
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), p)) > 0) {
        bytes += (long)n;
    }
    return pclose(p) == 0 ? bytes / 2 : -1;
}

/* Decoding must succeed with nothing on stderr. */
static bool decodes_clean(const char *path)
{
    char cmd[512], out[1024];
    snprintf(cmd, sizeof(cmd), "ffmpeg -nostdin -v error -i '%s' -f null - 2>&1", path);
    return run(cmd, out, sizeof(out)) && out[0] == '\0';
}

static void test_ffmpeg(void)
{
    char out[256];
    const char *tmp = getenv("TEST_TMPDIR");
    if (!tmp || !run("ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libopus", out, sizeof(out))) {
        printf("test_ogg_opus: ffmpeg with libopus not found, skipping the decode check\n");
        return;
    }
    mkdir(tmp, 0777);
    char ref[256], ours[256], cut[256], cmd[768];
    snprintf(ref, sizeof(ref), "%s/ref.opus", tmp);
    snprintf(ours, sizeof(ours), "%s/ours.opus", tmp);
    snprintf(cut, sizeof(cut), "%s/cut.opus", tmp);
    /* 12.34 s of a tone sweep, encoded like the watch does: 16 kHz mono, 20 ms, 16 kbps
     * VBR, VoIP. */
    snprintf(cmd, sizeof(cmd),
             "ffmpeg -nostdin -v error -y -f lavfi -i \"aevalsrc=0.3*sin(2*PI*(200+40*t)*t):s=16000:d=12.34\" "
             "-ac 1 -c:a libopus -b:a 16k -vbr on -application voip -frame_duration 20 '%s' 2>&1",
             ref);
    if (!run(cmd, out, sizeof(out))) {
        printf("test_ogg_opus: ffmpeg encode failed (%s), skipping\n", out);
        return;
    }
    size_t len;
    uint8_t *d = read_file(ref, &len);
    CHECK(d != NULL);
    if (!d) {
        return;
    }
    static page_t pages[256];
    const int np = parse_pages(d, len, pages, 256);
    CHECK(np > 3);

    static ogg_opus_writer_t w;
    ogg_opus_head_t h = test_head();
    h.pre_skip = (uint16_t)(pages[0].body[10] | pages[0].body[11] << 8);
    FILE *f = fopen(ours, "wb");
    CHECK_EQ(ogg_opus_begin(&w, &h, file_write, f), OGG_OPUS_OK);
    /* ffmpeg doesn't span packets across pages for small packets; reassemble anyway. */
    uint8_t pkt[4096];
    size_t plen = 0;
    int packets = 0;
    for (int i = 2; i < np; i++) {
        for (int k = 0, at = 0; k < pages[i].nsegs; k++) {
            memcpy(pkt + plen, pages[i].body + at, pages[i].segs[k]);
            plen += pages[i].segs[k];
            at += pages[i].segs[k];
            if (pages[i].segs[k] < 255) {
                CHECK_EQ(ogg_opus_add_packet(&w, pkt, plen, 960), OGG_OPUS_OK);
                plen = 0;
                packets++;
            }
        }
    }
    /* Trim the end like the reference stream does. */
    const int64_t ref_end = pages[np - 1].granule;
    CHECK_EQ(ogg_opus_finish(&w, (uint32_t)(960LL * packets - ref_end)), OGG_OPUS_OK);
    fclose(f);
    CHECK(decodes_clean(ours));
    const long dur = decoded_samples(ours);
    CHECK_EQ(dur, decoded_samples(ref));
    CHECK_EQ(dur, 48000L * 1234 / 100);

    /* A power cut in the middle of page 5: the scanner cuts back to page 4, and that
     * decodes cleanly too (no EOS), to (pages 2..4 = 150 packets) * 960 - pre-skip samples. */
    free(d);
    d = read_file(ours, &len);
    const int onp = parse_pages(d, len, pages, 256);
    CHECK(onp > 6);
    mem_t m = {d, pages[5].off + pages[5].len / 2};
    ogg_opus_scan_t sc;
    CHECK_EQ(ogg_opus_scan(mem_read, &m, &sc), OGG_OPUS_OK);
    CHECK_EQ(sc.valid_bytes, pages[5].off);
    f = fopen(cut, "wb");
    fwrite(d, 1, (size_t)sc.valid_bytes, f);
    fclose(f);
    CHECK(decodes_clean(cut));
    const long cut_dur = decoded_samples(cut);
    CHECK_EQ(cut_dur, (long)(sc.granule - sc.pre_skip));
    printf("test_ogg_opus: ffmpeg decoded the remux (%ld samples) and the cut file (%ld)\n", dur,
           cut_dur);
    free(d);
}
#endif

int main(void)
{
    test_crc();
    test_header_pages();
    test_audio_pages();
    test_page_limits();
    test_errors();
#ifdef __unix__
    test_ffmpeg();
#endif
    return checks_done("test_ogg_opus");
}
