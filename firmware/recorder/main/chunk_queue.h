/* The chunk queue on the SD card (docs/twatch-ble-protocol.md §2.4), as pure C over a
 * directory: stdio, dirent and POSIX file calls only, so it runs on the host tests as it
 * does on the watch. chunk_store.c wraps it with a mutex and the SD mount. Not thread-safe.
 *
 * Files in the queue directory:
 *   <id>.opus  the Ogg/Opus chunk (id = chunk_id as 8 lowercase hex digits)
 *   <id>.met   a 64-byte record: session, sequence, times, size, CRC-32, flags, state, with
 *              its own CRC. Rewritten in place (FAT can't rename onto an existing file);
 *              a torn write fails the CRC.
 *   next       the next chunk_id, so ids are never reused on a card.
 *
 * A chunk is "open" while it is being written and "closed" (listable) after. Writing
 * order keeps every power cut recoverable: the .met (open) is written before the .opus is
 * created, an ack deletes the .opus before the .met. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CQ_FLAG_LAST 0x01       /* same bits as CHUNK_FLAG_* in chunk_store.h */
#define CQ_FLAG_TIME_UNSET 0x02
#define CQ_FLAG_NOTE 0x04       /* the chunk belongs to a voice note */

#define CQ_RECORD_SIZE 64
#define CQ_RECORD_VERSION 1

enum {
    CQ_OK = 0,
    CQ_ERR_IO = -1,       /* file system call failed */
    CQ_ERR_NOT_FOUND = -2,
    CQ_ERR_STATE = -3,    /* e.g. a chunk is already open, or none is */
    CQ_ERR_NO_MEM = -4,
    CQ_ERR_ARG = -5,
};

typedef enum {
    CQ_STATE_OPEN = 1,
    CQ_STATE_CLOSED = 2,
} cq_state_t;

/* One chunk's record. The ENTRY fields of protocol §4.3, plus where it was recorded. */
typedef struct {
    uint32_t chunk_id;
    uint8_t session_id[8];
    uint32_t sequence;
    uint64_t start_ms;    /* epoch ms, or ms since the session start with TIME_UNSET */
    uint32_t duration_ms;
    uint32_t bytes;
    uint32_t crc32;       /* IEEE CRC-32 of the whole file */
    uint8_t flags;        /* CQ_FLAG_* */
    uint8_t state;        /* cq_state_t */
    uint32_t boot_id;     /* boot the chunk was recorded in */
    uint64_t mono_ms;     /* monotonic ms at the chunk start, in that boot */
} cq_info_t;

/* What a new chunk starts with (cq_begin). */
typedef struct {
    uint8_t session_id[8];
    uint32_t sequence;
    uint64_t start_ms;
    uint8_t flags;        /* CQ_FLAG_TIME_UNSET and/or CQ_FLAG_NOTE, or 0 */
    uint32_t boot_id;
    uint64_t mono_ms;
} cq_new_t;

typedef struct {
    uint32_t chunks;
    uint64_t bytes;
    uint64_t audio_ms;
    bool has_last;
    uint32_t oldest_id; /* 0 if empty */
} cq_stats_t;

/* What cq_init found and did, for the boot log. */
typedef struct {
    uint32_t loaded;      /* closed chunks indexed */
    uint32_t recovered;   /* open chunks cut back to their last page and closed */
    uint32_t discarded;   /* open chunks without audio, deleted */
    uint32_t corrupt;     /* chunks with audio but an unreadable record: left on the card, not listed */
    uint32_t cleaned;     /* leftover files without audio, deleted */
} cq_boot_report_t;

typedef struct {
    char dir[96];
    uint32_t next_id;
    cq_info_t *items;     /* closed chunks, ascending chunk_id */
    size_t count;
    size_t cap;
    /* The chunk being written. */
    bool open;
    bool abandoned;       /* cq_abandon_close() done, cq_abandon_recover() not yet */
    cq_info_t cur;
    FILE *cur_file;
    /* A cached read handle: the BLE side reads a chunk in many small pieces. */
    FILE *rd_file;
    uint32_t rd_id;
} chunk_queue_t;

/* Encode/decode the 64-byte .met record. decode returns false on a bad magic, version, state
 * or CRC, or chunk_id 0. Exposed for the tests. */
void cq_record_encode(const cq_info_t *info, uint8_t out[CQ_RECORD_SIZE]);
bool cq_record_decode(const uint8_t in[CQ_RECORD_SIZE], cq_info_t *info);

/* Create the directory if needed, read `next`, index the closed chunks, recover open ones
 * (cut back to the last complete Ogg page: closed with LAST if audio is left, otherwise
 * deleted and LAST moved to the session's previous queued chunk; no LAST either way when a
 * later chunk of the session is on the card), and remove leftovers. A file that can't be
 * read (EIO) is never deleted: it is left on the card and counted as corrupt.
 * report may be NULL. */
int cq_init(chunk_queue_t *q, const char *dir, cq_boot_report_t *report);
/* Close open files and free the index. */
void cq_deinit(chunk_queue_t *q);

/* ---- writing (one chunk at a time) ---- */
/* CQ_ERR_STATE if a chunk is open, or if every chunk id is used (ids never wrap to 0). */
int cq_begin(chunk_queue_t *q, const cq_new_t *c, uint32_t *chunk_id);
int cq_append(chunk_queue_t *q, const void *data, size_t len);
/* Push what was appended to the card (fflush + fsync), so a power cut keeps it. */
int cq_sync(chunk_queue_t *q);
/* Close the open chunk: final size, CRC, duration and flags (CQ_FLAG_LAST) go into its
 * record and it becomes listable. */
int cq_end(chunk_queue_t *q, uint32_t duration_ms, bool last);
/* The open chunk can't be finished normally (write error): recover it like a power cut,
 * cut back to its last complete page. *closed_id = the chunk_id if it was kept, 0 if it was
 * deleted. `last`: the session ends here, so the kept chunk gets LAST (or, if nothing was
 * kept, the session's previous chunk does). Without it the session goes on with a new chunk
 * and no flag changes. */
int cq_abandon(chunk_queue_t *q, bool last, uint32_t *closed_id);
/* cq_abandon() in two steps, so the caller can power the card off and on in between (the
 * file is closed after the first): a card that stopped answering can't be read to recover
 * the chunk until then. cq_abandon_recover() without cq_abandon_close() is CQ_ERR_STATE. If
 * the card still can't be read, recovery returns CQ_ERR_IO and leaves the chunk on the card
 * for the next boot's recovery (it is never deleted for a read error). */
int cq_abandon_close(chunk_queue_t *q);
int cq_abandon_recover(chunk_queue_t *q, bool last, uint32_t *closed_id);
/* Set LAST on a closed chunk (the next chunk of its session couldn't be started). */
int cq_set_last(chunk_queue_t *q, uint32_t chunk_id);
/* The open chunk, if any. */
bool cq_current(const chunk_queue_t *q, cq_info_t *out);

/* ---- reading and acking (closed chunks only) ---- */
size_t cq_list(const chunk_queue_t *q, uint32_t after_id, cq_info_t *out, size_t max,
               uint32_t *remaining);
bool cq_get(const chunk_queue_t *q, uint32_t chunk_id, cq_info_t *out);
/* *got = 0 at the end of the file. CQ_ERR_NOT_FOUND if the chunk isn't queued. */
int cq_read(chunk_queue_t *q, uint32_t chunk_id, uint32_t offset, void *buf, size_t len,
            size_t *got);
/* Close the cached read handle (before the card is unmounted). */
void cq_read_close(chunk_queue_t *q);
/* Delete both files. *was_queued = false if it wasn't queued (still CQ_OK). */
int cq_delete(chunk_queue_t *q, uint32_t chunk_id, bool *was_queued);

/* The clock was set: TIME_UNSET chunks recorded in boot `boot_id` (closed and open) get
 * start_ms = now_epoch_ms - (now_mono_ms - mono_ms), the flag cleared and their record
 * rewritten. Returns how many closed chunks were converted, or < 0 on an error; a chunk
 * whose record couldn't be written stays TIME_UNSET (in memory too) for the next call. */
int cq_fix_time(chunk_queue_t *q, uint32_t boot_id, int64_t now_epoch_ms, int64_t now_mono_ms);

void cq_stats(const chunk_queue_t *q, cq_stats_t *out);

/* A chunk id typed on the console (`queue drop <id>`): decimal, as `queue`, the relays and
 * the BLE log print it, or hex with a 0x prefix. A leading 0 without 0x is refused: strtoul
 * base 0 would read "00000010" (a zero-padded hex file name) as octal 8, another chunk.
 * Returns false for 0, junk, or more than 32 bits. */
bool cq_parse_id(const char *s, uint32_t *id);

/* "3f9a0c11d2e4b507" form of a session id; out needs 17 bytes. */
void cq_session_hex(const uint8_t id[8], char out[17]);

#ifdef __cplusplus
}
#endif
