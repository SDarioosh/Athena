/* Feed swap and the detail cache (§2). */
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "check.h"
#include "feed_store.h"

static char dir[256];

/* A fresh folder under /tmp, not under the build directory: the firmware's paths are short
 * ("/lfs/ob", OUTBOX_DIR_MAX 48; feed_store PATH_LEN 96), and a CI checkout path is longer
 * than that. */
static void remove_dir(void)
{
    char cmd[300];
    snprintf(cmd, sizeof(cmd), "rm -rf '%s'", dir);
    (void)system(cmd);
}

/* /tmp/pda_<name>_<pid>, empty (mkdtemp isn't declared on macOS under _POSIX_C_SOURCE). */
static int make_dir(const char *name)
{
    snprintf(dir, sizeof(dir), "/tmp/pda_%s_%d", name, (int)getpid());
    remove_dir();
    return mkdir(dir, 0755);
}

int main(void)
{
    if (make_dir("fs") != 0) {
        return 1;
    }
    CHECK_EQ(feed_store_init(dir), 0);
    char *data;
    size_t len;
    CHECK_EQ(feed_store_load_feed(dir, &data, &len), -1);

    CHECK_EQ(feed_store_put_feed(dir, "{\"v\":1}", 7), 0);
    CHECK_EQ(feed_store_load_feed(dir, &data, &len), 0);
    CHECK(len == 7 && strcmp(data, "{\"v\":1}") == 0);
    free(data);
    CHECK_EQ(feed_store_put_feed(dir, "second", 6), 0);
    CHECK_EQ(feed_store_load_feed(dir, &data, &len), 0);
    CHECK(len == 6 && memcmp(data, "second", 6) == 0);
    free(data);

    /* a feed.tmp left by a power cut is dropped; feed.json is untouched */
    char path[400];
    snprintf(path, sizeof(path), "%s/feed.tmp", dir);
    FILE *f = fopen(path, "wb");
    fputs("torn", f);
    fclose(f);
    CHECK_EQ(feed_store_init(dir), 0);
    struct stat st;
    CHECK(stat(path, &st) != 0);
    CHECK_EQ(feed_store_load_feed(dir, &data, &len), 0);
    CHECK(len == 6);
    free(data);

    /* details: replace by ref, evict the oldest beyond 20 */
    CHECK_EQ(feed_store_put_detail(dir, "conv:a", "A1", 2), 0);
    CHECK_EQ(feed_store_put_detail(dir, "conv:a", "A2", 2), 0);
    CHECK_EQ(feed_store_count_details(dir), 1);
    CHECK_EQ(feed_store_load_detail(dir, "conv:a", &data, &len), 0);
    CHECK(memcmp(data, "A2", 2) == 0);
    free(data);
    CHECK_EQ(feed_store_load_detail(dir, "conv:zz", &data, &len), -1);
    for (int i = 0; i < 25; i++) {
        char ref[32], body[32];
        snprintf(ref, sizeof(ref), "transcript:c%d", i);
        snprintf(body, sizeof(body), "body %d", i);
        CHECK_EQ(feed_store_put_detail(dir, ref, body, strlen(body)), 0);
    }
    CHECK_EQ(feed_store_count_details(dir), FEED_STORE_DETAILS_MAX);
    /* conv:a and c0..c4 were the oldest */
    CHECK_EQ(feed_store_load_detail(dir, "conv:a", &data, &len), -1);
    CHECK_EQ(feed_store_load_detail(dir, "transcript:c4", &data, &len), -1);
    CHECK_EQ(feed_store_load_detail(dir, "transcript:c5", &data, &len), 0);
    CHECK(memcmp(data, "body 5", 6) == 0);
    free(data);
    /* re-putting an old one makes it the newest */
    CHECK_EQ(feed_store_put_detail(dir, "transcript:c5", "again", 5), 0);
    CHECK_EQ(feed_store_put_detail(dir, "transcript:new", "n", 1), 0);
    CHECK_EQ(feed_store_load_detail(dir, "transcript:c5", &data, &len), 0);
    free(data);
    CHECK_EQ(feed_store_load_detail(dir, "transcript:c6", &data, &len), -1);

    CHECK_EQ(feed_store_wipe(dir), 0);
    CHECK_EQ(feed_store_count_details(dir), 0);
    CHECK_EQ(feed_store_load_feed(dir, &data, &len), -1);
    remove_dir();
    return checks_done("test_feed_store");
}
