/* Ogg/Opus muxer (RFC 7845 on RFC 3533) for the recorder's 30 s chunks, and the scanner the
 * chunk queue uses to recover a file cut short by a power loss. Pure C; host-tested.
 * Chunk format: docs/twatch-ble-protocol.md §2.1.
 *
 * The writer buffers one page and writes it, in a single call to the caller's write
 * function, once the page holds OGG_OPUS_PAGE_PACKETS packets and the next one arrives (or
 * at finish). So a page on the card is always complete, and a power cut loses at most the
 * page being filled (~1 s). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define OGG_OPUS_PAGE_PACKETS 50     /* packets per audio page: 1 s of 20 ms frames */
#define OGG_OPUS_MAX_PACKET 1275     /* largest Opus packet (RFC 6716 §3.4) */
#define OGG_OPUS_MAX_BODY 8192       /* packet bytes per page; a full page is ~2 KB at 16 kbps */
#define OGG_OPUS_MAX_COMMENTS 4
#define OGG_OPUS_VENDOR "barvis-sound recorder"

enum {
    OGG_OPUS_OK = 0,
    OGG_OPUS_ERR_WRITE = -1, /* the write function failed; the writer is unusable */
    OGG_OPUS_ERR_ARG = -2,
    OGG_OPUS_ERR_STATE = -3,
    OGG_OPUS_ERR_READ = -4,
};

/* Writes `len` bytes (always one whole page). Returns 0 on success. */
typedef int (*ogg_opus_write_fn)(void *ctx, const void *data, size_t len);

typedef struct {
    uint32_t serial;      /* Ogg stream serial number */
    uint16_t pre_skip;    /* encoder lookahead in 48 kHz samples */
    uint32_t input_rate;  /* informational, e.g. 16000 */
    const char *vendor;   /* NULL: OGG_OPUS_VENDOR */
    const char *comments[OGG_OPUS_MAX_COMMENTS]; /* "KEY=value", unused entries NULL */
} ogg_opus_head_t;

typedef struct {
    ogg_opus_write_fn write;
    void *ctx;
    uint32_t serial;
    uint32_t page_seq;    /* next page's sequence number */
    uint64_t granule;     /* 48 kHz samples of every packet added so far */
    uint64_t page_start;  /* granule before the buffered page's first packet */
    uint32_t packets;     /* audio packets added */
    uint64_t bytes;       /* bytes handed to the write function */
    bool started;
    bool finished;
    int page_packets;     /* packets in the buffered page */
    int nsegs;            /* lacing values in the buffered page */
    size_t body_len;
    uint8_t segs[255];
    /* Header and lacing are assembled right in front of the body, so a page goes out in
     * one write: [27-byte header][lacing][body]. */
    uint8_t buf[27 + 255 + OGG_OPUS_MAX_BODY];
} ogg_opus_writer_t;

/* Write the OpusHead (BOS) and OpusTags pages. The writer struct is ~8.5 KB: allocate it,
 * don't put it on a task stack. */
int ogg_opus_begin(ogg_opus_writer_t *w, const ogg_opus_head_t *head, ogg_opus_write_fn write,
                   void *ctx);
/* Add one Opus packet that decodes to `samples48` samples at 48 kHz (960 for 20 ms). */
int ogg_opus_add_packet(ogg_opus_writer_t *w, const uint8_t *packet, size_t len,
                        uint32_t samples48);
/* Write the buffered page now (not EOS). Not needed in normal use. */
int ogg_opus_flush(ogg_opus_writer_t *w);
/* Write the buffered page as the last one, with the EOS flag. `end_trim48` samples at the
 * end are cut on decode (RFC 7845 §4.5); it may not exceed the samples in that page. At
 * least one packet must be buffered. */
int ogg_opus_finish(ogg_opus_writer_t *w, uint32_t end_trim48);

/* ---- scanner (power-cut recovery) ---- */

/* Read up to `len` bytes at `offset`. Returns the bytes read (0 at the end), < 0 on error. */
typedef int (*ogg_opus_read_fn)(void *ctx, uint64_t offset, void *buf, size_t len);

typedef struct {
    uint64_t valid_bytes; /* end of the last complete, CRC-valid page of the stream */
    uint32_t pages;       /* valid pages, headers included */
    bool has_head;        /* page 0 is a valid OpusHead */
    bool has_audio;       /* a valid audio page completes at least one packet */
    bool eos;             /* the last valid page has the EOS flag */
    uint16_t pre_skip;    /* from OpusHead */
    uint64_t granule;     /* of the last valid audio page that completes a packet */
    uint32_t audio_packets;
} ogg_opus_scan_t;

/* Walk the pages from offset 0 and stop at the first one that is cut short, fails its CRC,
 * or doesn't continue the stream (serial, sequence). Returns OGG_OPUS_OK or
 * OGG_OPUS_ERR_READ; a damaged file is not an error, it just has a short valid_bytes. */
int ogg_opus_scan(ogg_opus_read_fn read, void *ctx, ogg_opus_scan_t *out);

/* Ogg's own CRC-32 (polynomial 0x04C11DB7, not reflected, init 0, no final xor). Exposed for
 * the tests. */
uint32_t ogg_crc_update(uint32_t crc, const void *data, size_t len);

#ifdef __cplusplus
}
#endif
