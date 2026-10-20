/* The chunk queue as the rest of the firmware sees it: closed Ogg/Opus chunks on the SD card
 * waiting for the phone (docs/twatch-ble-protocol.md §2.4). Thread-safe: the recorder task
 * adds chunks, the BLE task lists, reads and acks them.
 *
 * The in-memory index lives in RAM, so listing and stats never touch the card. Reads and
 * acks need the card: wrap a BLE connection's reads in chunk_store_media_open/close (the SD
 * mount is reference-counted, see board_storage_mount). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define CHUNK_FLAG_LAST 0x01       /* last chunk of its session */
#define CHUNK_FLAG_TIME_UNSET 0x02 /* start_ms is ms since the session start, not epoch ms */
#define CHUNK_FLAG_NOTE 0x04       /* the chunk belongs to a voice note (its own short session) */

/* One closed, listable chunk. The first fields are exactly the ENTRY fields of the
 * protocol (§4.3); boot_id and mono_ms are for TIME fix-ups only. */
typedef struct {
    uint32_t chunk_id;
    uint8_t session_id[8];
    uint32_t sequence;
    uint64_t start_ms;
    uint32_t duration_ms;
    uint32_t bytes;
    uint32_t crc32;
    uint8_t flags;
    uint32_t boot_id;  /* boot the chunk was recorded in */
    uint64_t mono_ms;  /* esp_timer ms at the chunk start, in that boot */
} chunk_info_t;

typedef struct {
    uint32_t chunks;
    uint32_t bytes;
    uint32_t audio_ms;
    bool has_last;          /* a finished session is waiting (advertising rule §7.1) */
    uint32_t oldest_id;     /* 0 if empty */
} chunk_store_stats_t;

/* Called (from the task that changed it) after a chunk was added, acked or changed. Keep
 * it short: post an event. */
typedef void (*chunk_store_listener_t)(void *ctx);

/* Mount the card, rebuild the index from /sdcard/q (recovering an interrupted chunk),
 * unmount. Without a card the store starts empty and chunk_store_ready() is false; call
 * again later to retry. boot_id is this boot's random id (status characteristic). */
esp_err_t chunk_store_init(uint32_t boot_id);
bool chunk_store_ready(void);
uint32_t chunk_store_boot_id(void);
void chunk_store_set_listener(chunk_store_listener_t cb, void *ctx);

void chunk_store_stats(chunk_store_stats_t *out);
/* Up to `max` chunks with chunk_id > after_id, ascending. *remaining = listable chunks
 * after the last one returned. Returns the number written to out. */
size_t chunk_store_list(uint32_t after_id, chunk_info_t *out, size_t max, uint32_t *remaining);
bool chunk_store_get(uint32_t chunk_id, chunk_info_t *out);

/* Reference-counted card access for reads/acks (mounts and powers the card). */
esp_err_t chunk_store_media_open(void);
void chunk_store_media_close(void);
/* Read up to `len` bytes at `offset`. *got = 0 at the end of the file. ESP_ERR_NOT_FOUND
 * if the chunk isn't queued. Needs chunk_store_media_open(). */
esp_err_t chunk_store_read(uint32_t chunk_id, uint32_t offset, void *buf, size_t len,
                           size_t *got);
/* Delete a chunk after the phone's ACK. *was_queued = false if it was already gone
 * (still ESP_OK). Needs chunk_store_media_open(). */
esp_err_t chunk_store_ack(uint32_t chunk_id, bool *was_queued);
/* Operator delete (console `queue drop <id>`): same as ack. */
esp_err_t chunk_store_drop(uint32_t chunk_id);

/* The clock was just set to now_epoch_ms (protocol TIME). Converts TIME_UNSET chunks of
 * this boot to epoch time and rewrites their records. Returns how many were converted. */
int chunk_store_fix_time(int64_t now_epoch_ms);

/* Console `queue`: print the index. */
void chunk_store_print(void);

/* ---- recording side (session_rec.c) ----
 * One chunk is open at a time and is not listed until chunk_store_end(). Call these between
 * chunk_store_media_open() and chunk_store_media_close(), from one task. */
typedef struct {
    uint8_t session_id[8];
    uint32_t sequence;
    uint64_t start_ms; /* epoch ms, or session-relative ms with CHUNK_FLAG_TIME_UNSET */
    uint8_t flags;     /* CHUNK_FLAG_TIME_UNSET and/or CHUNK_FLAG_NOTE, or 0 */
    uint64_t mono_ms;  /* esp_timer ms at the chunk start (for the TIME fix-up) */
} chunk_store_new_t;

/* Start a chunk (its record says "open" until it is ended). */
esp_err_t chunk_store_begin(const chunk_store_new_t *c, uint32_t *chunk_id);
esp_err_t chunk_store_append(const void *data, size_t len);
/* Flush what was appended to the card (fsync), so a power cut keeps it. */
esp_err_t chunk_store_sync(void);
/* Test hook (console `sd failwrite <n>`): the next n appends fail as a card error would. */
void chunk_store_fail_writes(int n);
/* Close the open chunk; it becomes listable. last = the session ends with it. */
esp_err_t chunk_store_end(uint32_t duration_ms, bool last);
/* The open chunk can't be finished (write error): keep what the card holds up to the last
 * complete Ogg page, as a closed chunk with LAST; without audio, delete it and give LAST to
 * the session's previous chunk. *kept_id = 0 if it was deleted. */
esp_err_t chunk_store_abandon(bool last, uint32_t *kept_id);
/* Give a closed chunk LAST (the session ended before its next chunk could start). */
esp_err_t chunk_store_set_last(uint32_t chunk_id);
/* Free space on the card. Needs chunk_store_media_open(). */
esp_err_t chunk_store_free_bytes(uint64_t *free_bytes);

#ifdef __cplusplus
}
#endif
