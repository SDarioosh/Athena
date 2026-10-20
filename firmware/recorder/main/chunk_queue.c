#include "chunk_queue.h"

#include <dirent.h>
#include <errno.h>
#include <inttypes.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "crc32.h"
#include "ogg_opus.h"

#define RECORD_MAGIC "BQMR"
#define NEXT_FILE "next"
#define FIRST_ID 1 /* LIST starts at after_id = 0, so 0 is never a chunk id */

/* ---- little-endian helpers ---- */

static void put32(uint8_t *p, uint32_t v)
{
    for (int i = 0; i < 4; i++) {
        p[i] = (uint8_t)(v >> (8 * i));
    }
}

static void put64(uint8_t *p, uint64_t v)
{
    put32(p, (uint32_t)v);
    put32(p + 4, (uint32_t)(v >> 32));
}

static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static uint64_t get64(const uint8_t *p)
{
    return (uint64_t)get32(p) | (uint64_t)get32(p + 4) << 32;
}

/* ---- the .met record ----
 *  0 magic "BQMR"   4 version   5 state   6 flags   7 reserved
 *  8 chunk_id  12 session_id[8]  20 sequence  24 start_ms (u64)  32 duration_ms
 * 36 bytes  40 crc32 of the file  44 boot_id  48 mono_ms (u64)  56 reserved
 * 60 CRC-32 of bytes 0..59 */

void cq_record_encode(const cq_info_t *info, uint8_t out[CQ_RECORD_SIZE])
{
    memset(out, 0, CQ_RECORD_SIZE);
    memcpy(out, RECORD_MAGIC, 4);
    out[4] = CQ_RECORD_VERSION;
    out[5] = info->state;
    out[6] = info->flags;
    put32(out + 8, info->chunk_id);
    memcpy(out + 12, info->session_id, 8);
    put32(out + 20, info->sequence);
    put64(out + 24, info->start_ms);
    put32(out + 32, info->duration_ms);
    put32(out + 36, info->bytes);
    put32(out + 40, info->crc32);
    put32(out + 44, info->boot_id);
    put64(out + 48, info->mono_ms);
    put32(out + 60, crc32_update(0, out, 60));
}

bool cq_record_decode(const uint8_t in[CQ_RECORD_SIZE], cq_info_t *info)
{
    if (memcmp(in, RECORD_MAGIC, 4) != 0 || in[4] != CQ_RECORD_VERSION ||
        get32(in + 60) != crc32_update(0, in, 60) ||
        (in[5] != CQ_STATE_OPEN && in[5] != CQ_STATE_CLOSED)) {
        return false;
    }
    memset(info, 0, sizeof(*info));
    info->state = in[5];
    info->flags = in[6];
    info->chunk_id = get32(in + 8);
    memcpy(info->session_id, in + 12, 8);
    info->sequence = get32(in + 20);
    info->start_ms = get64(in + 24);
    info->duration_ms = get32(in + 32);
    info->bytes = get32(in + 36);
    info->crc32 = get32(in + 40);
    info->boot_id = get32(in + 44);
    info->mono_ms = get64(in + 48);
    return true;
}

void cq_session_hex(const uint8_t id[8], char out[17])
{
    static const char hex[] = "0123456789abcdef";
    for (int i = 0; i < 8; i++) {
        out[2 * i] = hex[id[i] >> 4];
        out[2 * i + 1] = hex[id[i] & 0x0F];
    }
    out[16] = '\0';
}

bool cq_parse_id(const char *s, uint32_t *id)
{
    if (!s || !*s) {
        return false;
    }
    int base = 10;
    if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
        base = 16;
        s += 2;
    } else if (s[0] == '0') {
        return false; /* octal for strtoul, hex in a file name: ambiguous */
    }
    uint64_t v = 0;
    if (!*s) {
        return false;
    }
    for (; *s; s++) {
        int d;
        if (*s >= '0' && *s <= '9') {
            d = *s - '0';
        } else if (base == 16 && *s >= 'a' && *s <= 'f') {
            d = *s - 'a' + 10;
        } else if (base == 16 && *s >= 'A' && *s <= 'F') {
            d = *s - 'A' + 10;
        } else {
            return false;
        }
        v = v * (uint64_t)base + (uint64_t)d;
        if (v > UINT32_MAX) {
            return false;
        }
    }
    if (v == 0) {
        return false;
    }
    *id = (uint32_t)v;
    return true;
}

/* ---- files ---- */

static void path_of(const chunk_queue_t *q, uint32_t id, const char *ext, char *out, size_t len)
{
    snprintf(out, len, "%s/%08" PRIx32 ".%s", q->dir, id, ext);
}

/* Write a small file in place. An existing file is overwritten with "r+b" rather than
 * recreated: its size and directory entry stay as they are, so a power cut can at worst
 * tear the data sector, which the record's CRC catches. */
static int write_small(const char *path, const void *data, size_t len)
{
    FILE *f = fopen(path, "r+b");
    if (!f) {
        f = fopen(path, "wb");
    }
    if (!f) {
        return CQ_ERR_IO;
    }
    int err = fwrite(data, 1, len, f) == len ? CQ_OK : CQ_ERR_IO;
    if (fflush(f) != 0 || fsync(fileno(f)) != 0) {
        err = CQ_ERR_IO;
    }
    if (fclose(f) != 0) {
        err = CQ_ERR_IO;
    }
    return err;
}

static int write_record(const chunk_queue_t *q, const cq_info_t *info)
{
    char path[128];
    uint8_t rec[CQ_RECORD_SIZE];
    path_of(q, info->chunk_id, "met", path, sizeof(path));
    cq_record_encode(info, rec);
    return write_small(path, rec, sizeof(rec));
}

static bool read_record(const chunk_queue_t *q, uint32_t id, cq_info_t *info)
{
    char path[128];
    uint8_t rec[CQ_RECORD_SIZE];
    path_of(q, id, "met", path, sizeof(path));
    FILE *f = fopen(path, "rb");
    if (!f) {
        return false;
    }
    const bool ok = fread(rec, 1, sizeof(rec), f) == sizeof(rec) && cq_record_decode(rec, info) &&
                    info->chunk_id == id;
    fclose(f);
    return ok;
}

static int write_next(const chunk_queue_t *q, uint32_t next)
{
    char path[128];
    uint8_t b[8];
    snprintf(path, sizeof(path), "%s/" NEXT_FILE, q->dir);
    put32(b, next);
    put32(b + 4, crc32_update(0, b, 4));
    return write_small(path, b, sizeof(b));
}

static uint32_t read_next(const chunk_queue_t *q)
{
    char path[128];
    uint8_t b[8];
    snprintf(path, sizeof(path), "%s/" NEXT_FILE, q->dir);
    FILE *f = fopen(path, "rb");
    if (!f) {
        return FIRST_ID;
    }
    const bool ok = fread(b, 1, sizeof(b), f) == sizeof(b) && get32(b + 4) == crc32_update(0, b, 4);
    fclose(f);
    return ok && get32(b) >= FIRST_ID ? get32(b) : FIRST_ID;
}

static bool remove_file(const char *path)
{
    return remove(path) == 0 || errno == ENOENT;
}

static void remove_chunk_files(const chunk_queue_t *q, uint32_t id)
{
    char path[128];
    path_of(q, id, "opus", path, sizeof(path));
    remove_file(path);
    path_of(q, id, "met", path, sizeof(path));
    remove_file(path);
}

typedef struct {
    FILE *f;
} file_reader_t;

static int file_read_at(void *ctx, uint64_t offset, void *buf, size_t len)
{
    FILE *f = ((file_reader_t *)ctx)->f;
    if (fseek(f, (long)offset, SEEK_SET) != 0) {
        return -1;
    }
    const size_t got = fread(buf, 1, len, f);
    return ferror(f) ? -1 : (int)got;
}

static int scan_file(const char *path, ogg_opus_scan_t *scan, long *size)
{
    file_reader_t r = {.f = fopen(path, "rb")};
    if (!r.f) {
        return CQ_ERR_NOT_FOUND;
    }
    int err = ogg_opus_scan(file_read_at, &r, scan) == OGG_OPUS_OK ? CQ_OK : CQ_ERR_IO;
    if (fseek(r.f, 0, SEEK_END) != 0 || (*size = ftell(r.f)) < 0) {
        err = CQ_ERR_IO;
    }
    fclose(r.f);
    return err;
}

static int file_crc(const char *path, uint32_t *crc, uint32_t *bytes)
{
    FILE *f = fopen(path, "rb");
    if (!f) {
        return CQ_ERR_IO;
    }
    uint8_t buf[512];
    size_t n;
    *crc = 0;
    *bytes = 0;
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0) {
        *crc = crc32_update(*crc, buf, n);
        *bytes += (uint32_t)n;
    }
    const int err = ferror(f) ? CQ_ERR_IO : CQ_OK;
    fclose(f);
    return err;
}

/* ---- the index (closed chunks, ascending id) ---- */

static size_t lower_bound(const chunk_queue_t *q, uint32_t id)
{
    size_t lo = 0, hi = q->count;
    while (lo < hi) {
        const size_t mid = lo + (hi - lo) / 2;
        if (q->items[mid].chunk_id < id) {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    return lo;
}

static cq_info_t *find(const chunk_queue_t *q, uint32_t id)
{
    const size_t i = lower_bound(q, id);
    return i < q->count && q->items[i].chunk_id == id ? &q->items[i] : NULL;
}

static int insert(chunk_queue_t *q, const cq_info_t *info)
{
    if (q->count == q->cap) {
        const size_t cap = q->cap ? q->cap * 2 : 64;
        cq_info_t *items = realloc(q->items, cap * sizeof(*items));
        if (!items) {
            return CQ_ERR_NO_MEM;
        }
        q->items = items;
        q->cap = cap;
    }
    const size_t i = lower_bound(q, info->chunk_id);
    memmove(&q->items[i + 1], &q->items[i], (q->count - i) * sizeof(*info));
    q->items[i] = *info;
    q->count++;
    return CQ_OK;
}

/* The latest queued chunk of a session before `sequence`. */
static cq_info_t *previous_in_session(const chunk_queue_t *q, const uint8_t session[8],
                                      uint32_t sequence)
{
    cq_info_t *best = NULL;
    for (size_t i = 0; i < q->count; i++) {
        cq_info_t *c = &q->items[i];
        if (memcmp(c->session_id, session, 8) == 0 && c->sequence < sequence &&
            (!best || c->sequence > best->sequence)) {
            best = c;
        }
    }
    return best;
}

/* Recover a chunk left open (power cut or write error): cut the file back to its last
 * complete page. With audio left it becomes a closed chunk with LAST; without, both files
 * go and the session's previous queued chunk gets LAST, so the session still ends. (If that
 * chunk was already acked, the server ends the session on its idle timeout.) */
static int recover_open(chunk_queue_t *q, cq_info_t info, uint32_t *kept_id,
                        cq_boot_report_t *rep)
{
    char path[128];
    path_of(q, info.chunk_id, "opus", path, sizeof(path));
    ogg_opus_scan_t scan;
    long size = 0;
    int err = scan_file(path, &scan, &size);
    if (err == CQ_ERR_IO) {
        return err;
    }
    *kept_id = 0;
    if (err == CQ_OK && scan.has_audio) {
        if ((uint64_t)size > scan.valid_bytes && truncate(path, (off_t)scan.valid_bytes) != 0) {
            return CQ_ERR_IO;
        }
        err = file_crc(path, &info.crc32, &info.bytes);
        if (err) {
            return err;
        }
        info.duration_ms = scan.granule > scan.pre_skip
                               ? (uint32_t)((scan.granule - scan.pre_skip) / 48)
                               : 0;
        info.flags |= CQ_FLAG_LAST;
        info.state = CQ_STATE_CLOSED;
        err = write_record(q, &info);
        if (err) {
            return err;
        }
        *kept_id = info.chunk_id;
        rep->recovered++;
        return insert(q, &info);
    }
    remove_chunk_files(q, info.chunk_id);
    rep->discarded++;
    cq_info_t *prev = previous_in_session(q, info.session_id, info.sequence);
    if (prev && !(prev->flags & CQ_FLAG_LAST)) {
        prev->flags |= CQ_FLAG_LAST;
        return write_record(q, prev);
    }
    return CQ_OK;
}

/* ---- boot ---- */

typedef struct {
    uint32_t *v;
    size_t n, cap;
} idlist_t;

static bool idlist_add(idlist_t *l, uint32_t id)
{
    if (l->n == l->cap) {
        const size_t cap = l->cap ? l->cap * 2 : 64;
        uint32_t *v = realloc(l->v, cap * sizeof(*v));
        if (!v) {
            return false;
        }
        l->v = v;
        l->cap = cap;
    }
    l->v[l->n++] = id;
    return true;
}

static int cmp_u32(const void *a, const void *b)
{
    const uint32_t x = *(const uint32_t *)a, y = *(const uint32_t *)b;
    return x < y ? -1 : x > y;
}

static bool idlist_has(const idlist_t *l, uint32_t id)
{
    return bsearch(&id, l->v, l->n, sizeof(id), cmp_u32) != NULL;
}

/* "0000002a.opus" -> 0x2a and "opus". */
static bool parse_name(const char *name, uint32_t *id, const char **ext)
{
    for (int i = 0; i < 8; i++) {
        const char c = name[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F'))) {
            return false;
        }
    }
    if (name[8] != '.') {
        return false;
    }
    *id = (uint32_t)strtoul(name, NULL, 16);
    *ext = name + 9;
    return true;
}

static bool ext_is(const char *ext, const char *want)
{
    /* FAT may report short names in upper case. */
    for (; *want; ext++, want++) {
        const char c = (*ext >= 'A' && *ext <= 'Z') ? (char)(*ext - 'A' + 'a') : *ext;
        if (c != *want) {
            return false;
        }
    }
    return *ext == '\0';
}

/* A chunk whose record is missing or unreadable: keep the audio file if it holds audio (the
 * owner can still pull it over USB), otherwise clean up. */
static void handle_orphan(chunk_queue_t *q, uint32_t id, bool has_opus, cq_boot_report_t *rep)
{
    char path[128];
    path_of(q, id, "opus", path, sizeof(path));
    ogg_opus_scan_t scan;
    long size;
    if (has_opus && scan_file(path, &scan, &size) == CQ_OK && scan.has_audio) {
        rep->corrupt++;
        return;
    }
    remove_chunk_files(q, id);
    rep->cleaned++;
}

int cq_init(chunk_queue_t *q, const char *dir, cq_boot_report_t *report)
{
    cq_boot_report_t dummy;
    cq_boot_report_t *rep = report ? report : &dummy;
    memset(rep, 0, sizeof(*rep));
    memset(q, 0, sizeof(*q));
    if (strlen(dir) >= sizeof(q->dir)) {
        return CQ_ERR_ARG;
    }
    strcpy(q->dir, dir);
    if (mkdir(dir, 0777) != 0 && errno != EEXIST) {
        return CQ_ERR_IO;
    }
    q->next_id = read_next(q);

    /* Collect names first: deleting while a FAT directory is being read is not safe. */
    idlist_t mets = {0}, opus = {0};
    DIR *d = opendir(dir);
    if (!d) {
        return CQ_ERR_IO;
    }
    int err = CQ_OK;
    uint32_t max_id = 0;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        uint32_t id;
        const char *ext;
        if (!parse_name(e->d_name, &id, &ext)) {
            continue;
        }
        const bool is_met = ext_is(ext, "met"), is_opus = ext_is(ext, "opus");
        if ((is_met && !idlist_add(&mets, id)) || (is_opus && !idlist_add(&opus, id))) {
            err = CQ_ERR_NO_MEM;
        }
        if ((is_met || is_opus) && id > max_id) {
            max_id = id;
        }
    }
    closedir(d);
    qsort(mets.v, mets.n, sizeof(uint32_t), cmp_u32);
    qsort(opus.v, opus.n, sizeof(uint32_t), cmp_u32);

    /* Closed chunks first, so that recovering an open one can find its session's previous
     * chunk. */
    idlist_t open_ids = {0};
    for (size_t i = 0; err == CQ_OK && i < mets.n; i++) {
        const uint32_t id = mets.v[i];
        cq_info_t info;
        const bool has_opus = idlist_has(&opus, id);
        if (!read_record(q, id, &info)) {
            handle_orphan(q, id, has_opus, rep);
        } else if (info.state == CQ_STATE_OPEN) {
            if (!idlist_add(&open_ids, id)) {
                err = CQ_ERR_NO_MEM;
            }
        } else if (!has_opus) {
            /* An ack cut short between deleting the audio and the record. */
            remove_chunk_files(q, id);
            rep->cleaned++;
        } else {
            err = insert(q, &info);
            rep->loaded++;
        }
    }
    for (size_t i = 0; err == CQ_OK && i < opus.n; i++) {
        if (!idlist_has(&mets, opus.v[i])) {
            handle_orphan(q, opus.v[i], true, rep);
        }
    }
    for (size_t i = 0; err == CQ_OK && i < open_ids.n; i++) {
        cq_info_t info;
        uint32_t kept;
        if (read_record(q, open_ids.v[i], &info)) {
            err = recover_open(q, info, &kept, rep);
        }
    }
    free(mets.v);
    free(opus.v);
    free(open_ids.v);

    if (err == CQ_OK && max_id >= q->next_id) {
        q->next_id = max_id + 1;
        err = write_next(q, q->next_id);
    }
    if (err != CQ_OK) {
        cq_deinit(q);
    }
    return err;
}

void cq_deinit(chunk_queue_t *q)
{
    cq_read_close(q);
    if (q->cur_file) {
        fclose(q->cur_file);
        q->cur_file = NULL;
    }
    q->open = false;
    free(q->items);
    q->items = NULL;
    q->count = q->cap = 0;
}

/* ---- writing ---- */

int cq_begin(chunk_queue_t *q, const cq_new_t *c, uint32_t *chunk_id)
{
    if (q->open) {
        return CQ_ERR_STATE;
    }
    const uint32_t id = q->next_id;
    /* Persist the counter first: an id handed out is never handed out again. */
    int err = write_next(q, id + 1);
    if (err) {
        return err;
    }
    q->next_id = id + 1;

    cq_info_t *cur = &q->cur;
    memset(cur, 0, sizeof(*cur));
    cur->chunk_id = id;
    memcpy(cur->session_id, c->session_id, 8);
    cur->sequence = c->sequence;
    cur->start_ms = c->start_ms;
    cur->flags = c->flags & (CQ_FLAG_TIME_UNSET | CQ_FLAG_NOTE);
    cur->state = CQ_STATE_OPEN;
    cur->boot_id = c->boot_id;
    cur->mono_ms = c->mono_ms;
    err = write_record(q, cur);
    if (err) {
        remove_chunk_files(q, id);
        return err;
    }
    char path[128];
    path_of(q, id, "opus", path, sizeof(path));
    q->cur_file = fopen(path, "wb");
    if (!q->cur_file) {
        remove_chunk_files(q, id);
        return CQ_ERR_IO;
    }
    q->open = true;
    *chunk_id = id;
    return CQ_OK;
}

int cq_append(chunk_queue_t *q, const void *data, size_t len)
{
    if (!q->open) {
        return CQ_ERR_STATE;
    }
    if (fwrite(data, 1, len, q->cur_file) != len) {
        return CQ_ERR_IO;
    }
    q->cur.crc32 = crc32_update(q->cur.crc32, data, len);
    q->cur.bytes += (uint32_t)len;
    return CQ_OK;
}

int cq_sync(chunk_queue_t *q)
{
    if (!q->open) {
        return CQ_ERR_STATE;
    }
    return fflush(q->cur_file) == 0 && fsync(fileno(q->cur_file)) == 0 ? CQ_OK : CQ_ERR_IO;
}

int cq_end(chunk_queue_t *q, uint32_t duration_ms, bool last)
{
    if (!q->open) {
        return CQ_ERR_STATE;
    }
    int err = cq_sync(q);
    if (fclose(q->cur_file) != 0) {
        err = CQ_ERR_IO;
    }
    q->cur_file = NULL;
    if (err) {
        /* Leave the open-state record: recover it like a power cut. */
        uint32_t kept;
        cq_boot_report_t rep = {0};
        q->open = false;
        recover_open(q, q->cur, &kept, &rep);
        return err;
    }
    q->open = false;
    q->cur.duration_ms = duration_ms;
    if (last) {
        q->cur.flags |= CQ_FLAG_LAST;
    }
    q->cur.state = CQ_STATE_CLOSED;
    err = write_record(q, &q->cur);
    if (err) {
        return err; /* still "open" on the card: the next boot recovers it */
    }
    return insert(q, &q->cur);
}

int cq_abandon(chunk_queue_t *q, uint32_t *closed_id)
{
    *closed_id = 0;
    if (!q->open) {
        return CQ_ERR_STATE;
    }
    fclose(q->cur_file);
    q->cur_file = NULL;
    q->open = false;
    cq_boot_report_t rep = {0};
    return recover_open(q, q->cur, closed_id, &rep);
}

int cq_set_last(chunk_queue_t *q, uint32_t chunk_id)
{
    cq_info_t *c = find(q, chunk_id);
    if (!c) {
        return CQ_ERR_NOT_FOUND;
    }
    if (c->flags & CQ_FLAG_LAST) {
        return CQ_OK;
    }
    c->flags |= CQ_FLAG_LAST;
    return write_record(q, c);
}

bool cq_current(const chunk_queue_t *q, cq_info_t *out)
{
    if (q->open && out) {
        *out = q->cur;
    }
    return q->open;
}

/* ---- reading and acking ---- */

size_t cq_list(const chunk_queue_t *q, uint32_t after_id, cq_info_t *out, size_t max,
               uint32_t *remaining)
{
    const size_t start = after_id == UINT32_MAX ? q->count : lower_bound(q, after_id + 1);
    size_t n = q->count - start;
    if (n > max) {
        n = max;
    }
    if (n) {
        memcpy(out, &q->items[start], n * sizeof(*out));
    }
    if (remaining) {
        *remaining = (uint32_t)(q->count - start - n);
    }
    return n;
}

bool cq_get(const chunk_queue_t *q, uint32_t chunk_id, cq_info_t *out)
{
    const cq_info_t *c = find(q, chunk_id);
    if (c && out) {
        *out = *c;
    }
    return c != NULL;
}

void cq_read_close(chunk_queue_t *q)
{
    if (q->rd_file) {
        fclose(q->rd_file);
        q->rd_file = NULL;
    }
    q->rd_id = 0;
}

int cq_read(chunk_queue_t *q, uint32_t chunk_id, uint32_t offset, void *buf, size_t len,
            size_t *got)
{
    *got = 0;
    const cq_info_t *c = find(q, chunk_id);
    if (!c) {
        return CQ_ERR_NOT_FOUND;
    }
    if (offset >= c->bytes) {
        return CQ_OK;
    }
    if (!q->rd_file || q->rd_id != chunk_id) {
        cq_read_close(q);
        char path[128];
        path_of(q, chunk_id, "opus", path, sizeof(path));
        q->rd_file = fopen(path, "rb");
        if (!q->rd_file) {
            return CQ_ERR_IO;
        }
        q->rd_id = chunk_id;
    }
    if (len > c->bytes - offset) {
        len = c->bytes - offset;
    }
    if (fseek(q->rd_file, (long)offset, SEEK_SET) != 0) {
        cq_read_close(q);
        return CQ_ERR_IO;
    }
    *got = fread(buf, 1, len, q->rd_file);
    if (*got < len) {
        cq_read_close(q);
        return CQ_ERR_IO;
    }
    return CQ_OK;
}

int cq_delete(chunk_queue_t *q, uint32_t chunk_id, bool *was_queued)
{
    *was_queued = false;
    cq_info_t *c = find(q, chunk_id);
    if (!c) {
        return CQ_OK;
    }
    if (q->rd_id == chunk_id) {
        cq_read_close(q);
    }
    char path[128];
    /* Audio first: a cut between the two leaves a record without audio, which the next
     * boot cleans up, never audio without a record. */
    path_of(q, chunk_id, "opus", path, sizeof(path));
    if (!remove_file(path)) {
        return CQ_ERR_IO;
    }
    path_of(q, chunk_id, "met", path, sizeof(path));
    remove_file(path);
    const size_t i = (size_t)(c - q->items);
    memmove(&q->items[i], &q->items[i + 1], (q->count - i - 1) * sizeof(*c));
    q->count--;
    *was_queued = true;
    return CQ_OK;
}

static bool fix_one(cq_info_t *c, uint32_t boot_id, int64_t now_epoch_ms, int64_t now_mono_ms)
{
    if (!(c->flags & CQ_FLAG_TIME_UNSET) || c->boot_id != boot_id) {
        return false;
    }
    const int64_t start = now_epoch_ms - (now_mono_ms - (int64_t)c->mono_ms);
    if (start <= 0) {
        return false;
    }
    c->start_ms = (uint64_t)start;
    c->flags &= (uint8_t)~CQ_FLAG_TIME_UNSET;
    return true;
}

int cq_fix_time(chunk_queue_t *q, uint32_t boot_id, int64_t now_epoch_ms, int64_t now_mono_ms)
{
    int fixed = 0, err = CQ_OK;
    for (size_t i = 0; i < q->count; i++) {
        if (fix_one(&q->items[i], boot_id, now_epoch_ms, now_mono_ms)) {
            fixed++;
            if (write_record(q, &q->items[i]) != CQ_OK) {
                err = CQ_ERR_IO;
            }
        }
    }
    /* The open chunk: its record on the card too, so a power cut keeps the epoch time. */
    if (q->open && fix_one(&q->cur, boot_id, now_epoch_ms, now_mono_ms)) {
        if (write_record(q, &q->cur) != CQ_OK) {
            err = CQ_ERR_IO;
        }
    }
    return err ? err : fixed;
}

void cq_stats(const chunk_queue_t *q, cq_stats_t *out)
{
    memset(out, 0, sizeof(*out));
    out->chunks = (uint32_t)q->count;
    out->oldest_id = q->count ? q->items[0].chunk_id : 0;
    for (size_t i = 0; i < q->count; i++) {
        out->bytes += q->items[i].bytes;
        out->audio_ms += q->items[i].duration_ms;
        out->has_last |= (q->items[i].flags & CQ_FLAG_LAST) != 0;
    }
}
