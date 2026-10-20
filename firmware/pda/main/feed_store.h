/* Blob storage in flash (docs/t5-pda-plan.md §2): the feed with an atomic swap, and up to
 * 20 cached detail blobs evicted oldest first. Pure C over stdio/dirent; host-tested
 * (test/test_feed_store.c). On the T5 the directory is on the LittleFS "storage"
 * partition, never the SD card.
 *
 * Files: <dir>/feed.json (the current feed), <dir>/feed.tmp (only during a swap),
 * <dir>/d/<seq 8 hex>-<crc32(ref) 8 hex>.json (details; seq orders them by age). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define FEED_STORE_DETAILS_MAX 20

/* Create the directories, drop a leftover feed.tmp. 0 or -3. */
int feed_store_init(const char *dir);

/* Write data as feed.tmp, then rename it over feed.json. The old feed stays until the
 * rename, so a power cut leaves one complete feed. 0 or -3. */
int feed_store_put_feed(const char *dir, const void *data, size_t len);
/* malloc'd copy of feed.json (NUL-terminated, *len without it). -1 none, -3 I/O. */
int feed_store_load_feed(const char *dir, char **data, size_t *len);

/* Cache a detail under its ref, replacing an older copy and evicting the oldest beyond 20. */
int feed_store_put_detail(const char *dir, const char *ref, const void *data, size_t len);
int feed_store_load_detail(const char *dir, const char *ref, char **data, size_t *len);
int feed_store_count_details(const char *dir);

/* Delete the feed and every detail (Status screen "wipe cache"). */
int feed_store_wipe(const char *dir);

#ifdef __cplusplus
}
#endif
