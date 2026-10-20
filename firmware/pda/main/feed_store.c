#include "feed_store.h"

#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "crc32.h"

#define PATH_LEN 96

static int write_file(const char *path, const void *data, size_t len)
{
    FILE *f = fopen(path, "wb");
    if (!f) {
        return -3;
    }
    bool ok = fwrite(data, 1, len, f) == len;
    ok &= fflush(f) == 0;
    ok &= fsync(fileno(f)) == 0;
    ok &= fclose(f) == 0;
    return ok ? 0 : -3;
}

static int read_file(const char *path, char **data, size_t *len)
{
    FILE *f = fopen(path, "rb");
    if (!f) {
        return -1;
    }
    struct stat st;
    if (fstat(fileno(f), &st) != 0 || st.st_size < 0) {
        fclose(f);
        return -3;
    }
    char *buf = malloc((size_t)st.st_size + 1);
    if (!buf) {
        fclose(f);
        return -3;
    }
    const size_t n = fread(buf, 1, (size_t)st.st_size, f);
    fclose(f);
    if (n != (size_t)st.st_size) {
        free(buf);
        return -3;
    }
    buf[n] = 0;
    *data = buf;
    *len = n;
    return 0;
}

int feed_store_init(const char *dir)
{
    char path[PATH_LEN];
    if (mkdir(dir, 0755) != 0 && errno != EEXIST) {
        return -3;
    }
    snprintf(path, sizeof(path), "%s/d", dir);
    if (mkdir(path, 0755) != 0 && errno != EEXIST) {
        return -3;
    }
    snprintf(path, sizeof(path), "%s/feed.tmp", dir);
    remove(path);
    return 0;
}

int feed_store_put_feed(const char *dir, const void *data, size_t len)
{
    char tmp[PATH_LEN], dst[PATH_LEN];
    snprintf(tmp, sizeof(tmp), "%s/feed.tmp", dir);
    snprintf(dst, sizeof(dst), "%s/feed.json", dir);
    if (write_file(tmp, data, len) != 0) {
        remove(tmp);
        return -3;
    }
    if (rename(tmp, dst) != 0) {
        remove(tmp);
        return -3;
    }
    return 0;
}

int feed_store_load_feed(const char *dir, char **data, size_t *len)
{
    char path[PATH_LEN];
    snprintf(path, sizeof(path), "%s/feed.json", dir);
    return read_file(path, data, len);
}

/* ---- details ---- */

typedef struct {
    uint32_t seq, key;
} dname_t;

static bool parse_dname(const char *name, dname_t *out)
{
    unsigned seq, key;
    char tail[8];
    if (strlen(name) != 22 || sscanf(name, "%8x-%8x%5s", &seq, &key, tail) != 3 ||
        strcmp(tail, ".json") != 0) {
        return false;
    }
    out->seq = seq;
    out->key = key;
    return true;
}

/* All detail files; returns the count (at most max). */
static int scan(const char *dir, dname_t *out, int max)
{
    char path[PATH_LEN];
    snprintf(path, sizeof(path), "%s/d", dir);
    DIR *d = opendir(path);
    if (!d) {
        return 0;
    }
    int n = 0;
    struct dirent *de;
    while ((de = readdir(d)) != NULL && n < max) {
        if (parse_dname(de->d_name, &out[n])) {
            n++;
        }
    }
    closedir(d);
    return n;
}

static void dpath(const char *dir, const dname_t *n, char *out, const char *ext)
{
    snprintf(out, PATH_LEN, "%s/d/%08x-%08x.%s", dir, (unsigned)n->seq, (unsigned)n->key, ext);
}

static uint32_t ref_key(const char *ref)
{
    return crc32_update(0, ref, strlen(ref));
}

int feed_store_count_details(const char *dir)
{
    dname_t names[FEED_STORE_DETAILS_MAX * 2];
    return scan(dir, names, FEED_STORE_DETAILS_MAX * 2);
}

int feed_store_put_detail(const char *dir, const char *ref, const void *data, size_t len)
{
    dname_t names[FEED_STORE_DETAILS_MAX * 2 + 1];
    int n = scan(dir, names, FEED_STORE_DETAILS_MAX * 2);
    const uint32_t key = ref_key(ref);
    uint32_t seq = 0;
    char path[PATH_LEN];
    /* Drop an older copy of the same ref first; find the newest seq. */
    for (int i = 0; i < n; i++) {
        if (names[i].seq >= seq) {
            seq = names[i].seq + 1;
        }
    }
    for (int i = 0; i < n;) {
        if (names[i].key == key) {
            dpath(dir, &names[i], path, "json");
            remove(path);
            names[i] = names[--n];
        } else {
            i++;
        }
    }
    const dname_t mine = {.seq = seq, .key = key};
    char tmp[PATH_LEN];
    dpath(dir, &mine, tmp, "tmp");
    dpath(dir, &mine, path, "json");
    if (write_file(tmp, data, len) != 0 || rename(tmp, path) != 0) {
        remove(tmp);
        return -3;
    }
    /* Evict the oldest beyond the limit. */
    while (n + 1 > FEED_STORE_DETAILS_MAX) {
        int oldest = 0;
        for (int i = 1; i < n; i++) {
            if (names[i].seq < names[oldest].seq) {
                oldest = i;
            }
        }
        dpath(dir, &names[oldest], path, "json");
        remove(path);
        names[oldest] = names[--n];
    }
    return 0;
}

int feed_store_load_detail(const char *dir, const char *ref, char **data, size_t *len)
{
    dname_t names[FEED_STORE_DETAILS_MAX * 2];
    const int n = scan(dir, names, FEED_STORE_DETAILS_MAX * 2);
    const uint32_t key = ref_key(ref);
    for (int i = 0; i < n; i++) {
        if (names[i].key == key) {
            char path[PATH_LEN];
            dpath(dir, &names[i], path, "json");
            return read_file(path, data, len);
        }
    }
    return -1;
}

int feed_store_wipe(const char *dir)
{
    dname_t names[FEED_STORE_DETAILS_MAX * 2];
    const int n = scan(dir, names, FEED_STORE_DETAILS_MAX * 2);
    char path[PATH_LEN];
    int rc = 0;
    for (int i = 0; i < n; i++) {
        dpath(dir, &names[i], path, "json");
        if (remove(path) != 0) {
            rc = -3;
        }
    }
    snprintf(path, sizeof(path), "%s/feed.json", dir);
    if (remove(path) != 0 && errno != ENOENT) {
        rc = -3;
    }
    return rc;
}
