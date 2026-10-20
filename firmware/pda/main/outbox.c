#include "outbox.h"

#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "crc32.h"

#define MAGIC 0x31424F50u /* "POB1" little-endian */
#define HDR_LEN 24
#define PATH_MAX_LEN (OUTBOX_DIR_MAX + 24)

static void put_u32(uint8_t *p, uint32_t v)
{
    for (int i = 0; i < 4; i++) {
        p[i] = (uint8_t)(v >> (8 * i));
    }
}

static uint32_t get_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static void path_for(const outbox_t *ob, uint32_t id, const char *ext, char *out)
{
    snprintf(out, PATH_MAX_LEN, "%s/%08x.%s", ob->dir, (unsigned)id, ext);
}

/* Write a whole file under a temporary name, then rename it into place. */
static int write_atomic(const char *path, const uint8_t *a, size_t alen, const uint8_t *b,
                        size_t blen)
{
    char tmp[PATH_MAX_LEN + 8];
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    FILE *f = fopen(tmp, "wb");
    if (!f) {
        return -3;
    }
    bool ok = fwrite(a, 1, alen, f) == alen && (blen == 0 || fwrite(b, 1, blen, f) == blen);
    ok &= fflush(f) == 0;
    ok &= fsync(fileno(f)) == 0;
    ok &= fclose(f) == 0;
    if (!ok || rename(tmp, path) != 0) {
        remove(tmp);
        return -3;
    }
    return 0;
}

static int save_next(const outbox_t *ob, uint32_t next)
{
    char path[PATH_MAX_LEN];
    snprintf(path, sizeof(path), "%s/next", ob->dir);
    uint8_t b[8];
    put_u32(b, next);
    put_u32(b + 4, ~next); /* check word */
    return write_atomic(path, b, sizeof(b), NULL, 0);
}

static bool load_next(const outbox_t *ob, uint32_t *next)
{
    char path[PATH_MAX_LEN];
    snprintf(path, sizeof(path), "%s/next", ob->dir);
    FILE *f = fopen(path, "rb");
    if (!f) {
        return false;
    }
    uint8_t b[8];
    const bool ok = fread(b, 1, 8, f) == 8 && get_u32(b) == ~get_u32(b + 4);
    fclose(f);
    if (ok) {
        *next = get_u32(b);
    }
    return ok;
}

/* Read and check one record. 0, -1 missing, -3 corrupt. */
static int read_record(const char *path, uint32_t want_id, pda_ob_entry_t *out)
{
    FILE *f = fopen(path, "rb");
    if (!f) {
        return -1;
    }
    uint8_t h[HDR_LEN];
    int rc = -3;
    if (fread(h, 1, HDR_LEN, f) == HDR_LEN && get_u32(h) == MAGIC && get_u32(h + 4) == want_id) {
        const uint32_t len = get_u32(h + 16);
        if (len > 0 && len <= PDA_PROTO_OB_JSON_MAX &&
            fread(out->json, 1, len, f) == len && fgetc(f) == EOF &&
            crc32_update(0, out->json, len) == get_u32(h + 20)) {
            out->action_id = want_id;
            out->created_ms = (uint64_t)get_u32(h + 8) | ((uint64_t)get_u32(h + 12) << 32);
            out->len = (uint16_t)len;
            rc = 0;
        }
    }
    fclose(f);
    return rc;
}

static int cmp_meta(const void *a, const void *b)
{
    const uint32_t x = ((const outbox_meta_t *)a)->id, y = ((const outbox_meta_t *)b)->id;
    return x < y ? -1 : x > y;
}

static bool parse_name(const char *name, uint32_t *id, const char **ext)
{
    if (strlen(name) < 10 || name[8] != '.') {
        return false;
    }
    char *end;
    const unsigned long v = strtoul(name, &end, 16);
    if (end != name + 8) {
        return false;
    }
    *id = (uint32_t)v;
    *ext = name + 9;
    return true;
}

int outbox_init(outbox_t *ob, const char *dir, uint32_t id_seed)
{
    memset(ob, 0, sizeof(*ob));
    if (strlen(dir) >= sizeof(ob->dir)) {
        return -2;
    }
    strcpy(ob->dir, dir);
    if (mkdir(dir, 0755) != 0 && errno != EEXIST) {
        return -3;
    }
    DIR *d = opendir(dir);
    if (!d) {
        return -3;
    }
    uint32_t max_id = 0;
    struct dirent *de;
    char path[OUTBOX_DIR_MAX + sizeof(de->d_name) + 2];
    pda_ob_entry_t *tmp = malloc(sizeof(*tmp));
    if (!tmp) {
        closedir(d);
        return -3;
    }
    while ((de = readdir(d)) != NULL) {
        const char *name = de->d_name;
        const size_t nl = strlen(name);
        if (nl > 4 && strcmp(name + nl - 4, ".tmp") == 0) {
            snprintf(path, sizeof(path), "%s/%s", dir, name);
            remove(path); /* an interrupted write */
            continue;
        }
        uint32_t id;
        const char *ext;
        if (!parse_name(name, &id, &ext) || strcmp(ext, "act") != 0) {
            continue;
        }
        snprintf(path, sizeof(path), "%s/%s", dir, name);
        if (read_record(path, id, tmp) != 0 || ob->n >= OUTBOX_MAX) {
            remove(path);
            ob->dropped_corrupt++;
            continue;
        }
        ob->e[ob->n++] = (outbox_meta_t){.id = id, .created_ms = tmp->created_ms, .len = tmp->len};
        if (id > max_id) {
            max_id = id;
        }
    }
    closedir(d);
    free(tmp);
    qsort(ob->e, (size_t)ob->n, sizeof(ob->e[0]), cmp_meta);

    uint32_t next = 0;
    if (!load_next(ob, &next)) {
        next = (id_seed & 0x7FFFFFFFu) | 1u;
        if (save_next(ob, next) != 0) {
            return -3;
        }
    }
    if (next <= max_id) {
        next = max_id + 1; /* never hand out an id that is on disk */
    }
    ob->next_id = next;
    return 0;
}

const outbox_meta_t *outbox_find(const outbox_t *ob, uint32_t id)
{
    for (int i = 0; i < ob->n; i++) {
        if (ob->e[i].id == id) {
            return &ob->e[i];
        }
    }
    return NULL;
}

int outbox_add(outbox_t *ob, const char *json, size_t len, uint64_t created_ms, uint32_t *id_out)
{
    if (ob->n >= OUTBOX_MAX) {
        return -1;
    }
    if (len == 0 || len > PDA_PROTO_OB_JSON_MAX) {
        return -2;
    }
    const uint32_t id = ob->next_id;
    /* Burn the id first: after a power cut it is never handed out again. */
    if (save_next(ob, id + 1) != 0) {
        return -3;
    }
    ob->next_id = id + 1;
    uint8_t h[HDR_LEN];
    put_u32(h, MAGIC);
    put_u32(h + 4, id);
    put_u32(h + 8, (uint32_t)created_ms);
    put_u32(h + 12, (uint32_t)(created_ms >> 32));
    put_u32(h + 16, (uint32_t)len);
    put_u32(h + 20, crc32_update(0, json, len));
    char path[PATH_MAX_LEN];
    path_for(ob, id, "act", path);
    if (write_atomic(path, h, sizeof(h), (const uint8_t *)json, len) != 0) {
        return -3;
    }
    ob->e[ob->n++] = (outbox_meta_t){.id = id, .created_ms = created_ms, .len = (uint16_t)len};
    if (id_out) {
        *id_out = id;
    }
    return 0;
}

int outbox_read(const outbox_t *ob, uint32_t id, pda_ob_entry_t *out)
{
    if (!outbox_find(ob, id)) {
        return -1;
    }
    char path[PATH_MAX_LEN];
    path_for(ob, id, "act", path);
    const int rc = read_record(path, id, out);
    return rc == 0 ? 0 : -3;
}

size_t outbox_list(outbox_t *ob, uint32_t after_id, pda_ob_entry_t *out, size_t max,
                   uint32_t *remaining)
{
    size_t n = 0;
    int i = 0;
    while (i < ob->n && ob->e[i].id <= after_id) {
        i++;
    }
    for (; i < ob->n && n < max; i++) {
        if (outbox_read(ob, ob->e[i].id, &out[n]) == 0) {
            ob->e[i].listed = true;
            n++;
        }
    }
    *remaining = (uint32_t)(ob->n - i);
    return n;
}

int outbox_remove(outbox_t *ob, uint32_t id, bool *was_queued)
{
    *was_queued = false;
    for (int i = 0; i < ob->n; i++) {
        if (ob->e[i].id != id) {
            continue;
        }
        char path[PATH_MAX_LEN];
        path_for(ob, id, "act", path);
        if (remove(path) != 0 && errno != ENOENT) {
            return -3;
        }
        memmove(&ob->e[i], &ob->e[i + 1], sizeof(ob->e[0]) * (size_t)(ob->n - i - 1));
        ob->n--;
        *was_queued = true;
        return 0;
    }
    return 0;
}

int outbox_clear(outbox_t *ob)
{
    while (ob->n > 0) {
        bool was;
        if (outbox_remove(ob, ob->e[0].id, &was) != 0) {
            return -3;
        }
    }
    return 0;
}
