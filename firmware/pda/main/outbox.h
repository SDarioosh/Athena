/* The persistent action queue (docs/t5-pda-plan.md §2, §4.2). Pure C over stdio and
 * dirent, so it runs on LittleFS on the T5 and in a temp directory on the host
 * (test/test_outbox.c).
 *
 * One file per action, `<dir>/<id as 8 hex>.act`: a 24-byte header (magic, id, created_ms,
 * length, CRC-32 of the JSON) and the JSON. Files are written as `.tmp` and renamed, so a
 * power cut leaves either the whole action or none. The next id lives in `<dir>/next`
 * (same write-and-rename) and is never reused, so a late OB_ACK for an old id can't remove
 * a newer action and the phone's cal_add dedupe on action_id stays sound. A fresh outbox
 * starts its ids at a caller-given random base, so a wiped T5 doesn't repeat old ids. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "pda_proto.h"

#ifdef __cplusplus
extern "C" {
#endif

#define OUTBOX_MAX 64
#define OUTBOX_DIR_MAX 48

typedef struct {
    uint32_t id;
    uint64_t created_ms;
    uint16_t len;
    bool listed; /* sent to a relay in an OB_LIST since boot (RAM only) */
} outbox_meta_t;

typedef struct {
    char dir[OUTBOX_DIR_MAX];
    uint32_t next_id;
    outbox_meta_t e[OUTBOX_MAX]; /* ascending id */
    int n;
    int dropped_corrupt; /* files dropped at init because they didn't check out */
} outbox_t;

/* Create the directory if needed, drop leftover .tmp files and corrupt records, rebuild the
 * index. id_seed: the first id of a brand-new outbox (no `next` file); kept below 2^31. */
int outbox_init(outbox_t *ob, const char *dir, uint32_t id_seed);

/* Queue an action (json <= 400 bytes). 0, or -1 full / -2 too long / -3 I/O. */
int outbox_add(outbox_t *ob, const char *json, size_t len, uint64_t created_ms, uint32_t *id_out);

/* Read one action's JSON. 0, -1 unknown id, -3 I/O or corrupt. */
int outbox_read(const outbox_t *ob, uint32_t id, pda_ob_entry_t *out);

/* Entries with id > after_id, ascending, at most max (the pda_store_t contract); marks
 * them listed. Entries whose file went bad are skipped. */
size_t outbox_list(outbox_t *ob, uint32_t after_id, pda_ob_entry_t *out, size_t max,
                   uint32_t *remaining);

/* Delete an action. *was_queued = false if it wasn't there (still 0). -3 on I/O error. */
int outbox_remove(outbox_t *ob, uint32_t id, bool *was_queued);

/* Delete every action (Status screen "wipe"); the id counter keeps counting. */
int outbox_clear(outbox_t *ob);

const outbox_meta_t *outbox_find(const outbox_t *ob, uint32_t id);

#ifdef __cplusplus
}
#endif
