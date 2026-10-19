#include "ogg_opus.h"

#include <string.h>

#define HDR_LEN 27
#define BODY_OFF (HDR_LEN + 255)

#define FLAG_CONTINUED 0x01
#define FLAG_BOS 0x02
#define FLAG_EOS 0x04

/* Ogg's CRC: polynomial 0x04C11DB7, MSB first (RFC 3533 §6). */
static const uint32_t s_ogg_table[256] = {
    0x00000000u, 0x04c11db7u, 0x09823b6eu, 0x0d4326d9u, 0x130476dcu, 0x17c56b6bu,
    0x1a864db2u, 0x1e475005u, 0x2608edb8u, 0x22c9f00fu, 0x2f8ad6d6u, 0x2b4bcb61u,
    0x350c9b64u, 0x31cd86d3u, 0x3c8ea00au, 0x384fbdbdu, 0x4c11db70u, 0x48d0c6c7u,
    0x4593e01eu, 0x4152fda9u, 0x5f15adacu, 0x5bd4b01bu, 0x569796c2u, 0x52568b75u,
    0x6a1936c8u, 0x6ed82b7fu, 0x639b0da6u, 0x675a1011u, 0x791d4014u, 0x7ddc5da3u,
    0x709f7b7au, 0x745e66cdu, 0x9823b6e0u, 0x9ce2ab57u, 0x91a18d8eu, 0x95609039u,
    0x8b27c03cu, 0x8fe6dd8bu, 0x82a5fb52u, 0x8664e6e5u, 0xbe2b5b58u, 0xbaea46efu,
    0xb7a96036u, 0xb3687d81u, 0xad2f2d84u, 0xa9ee3033u, 0xa4ad16eau, 0xa06c0b5du,
    0xd4326d90u, 0xd0f37027u, 0xddb056feu, 0xd9714b49u, 0xc7361b4cu, 0xc3f706fbu,
    0xceb42022u, 0xca753d95u, 0xf23a8028u, 0xf6fb9d9fu, 0xfbb8bb46u, 0xff79a6f1u,
    0xe13ef6f4u, 0xe5ffeb43u, 0xe8bccd9au, 0xec7dd02du, 0x34867077u, 0x30476dc0u,
    0x3d044b19u, 0x39c556aeu, 0x278206abu, 0x23431b1cu, 0x2e003dc5u, 0x2ac12072u,
    0x128e9dcfu, 0x164f8078u, 0x1b0ca6a1u, 0x1fcdbb16u, 0x018aeb13u, 0x054bf6a4u,
    0x0808d07du, 0x0cc9cdcau, 0x7897ab07u, 0x7c56b6b0u, 0x71159069u, 0x75d48ddeu,
    0x6b93dddbu, 0x6f52c06cu, 0x6211e6b5u, 0x66d0fb02u, 0x5e9f46bfu, 0x5a5e5b08u,
    0x571d7dd1u, 0x53dc6066u, 0x4d9b3063u, 0x495a2dd4u, 0x44190b0du, 0x40d816bau,
    0xaca5c697u, 0xa864db20u, 0xa527fdf9u, 0xa1e6e04eu, 0xbfa1b04bu, 0xbb60adfcu,
    0xb6238b25u, 0xb2e29692u, 0x8aad2b2fu, 0x8e6c3698u, 0x832f1041u, 0x87ee0df6u,
    0x99a95df3u, 0x9d684044u, 0x902b669du, 0x94ea7b2au, 0xe0b41de7u, 0xe4750050u,
    0xe9362689u, 0xedf73b3eu, 0xf3b06b3bu, 0xf771768cu, 0xfa325055u, 0xfef34de2u,
    0xc6bcf05fu, 0xc27dede8u, 0xcf3ecb31u, 0xcbffd686u, 0xd5b88683u, 0xd1799b34u,
    0xdc3abdedu, 0xd8fba05au, 0x690ce0eeu, 0x6dcdfd59u, 0x608edb80u, 0x644fc637u,
    0x7a089632u, 0x7ec98b85u, 0x738aad5cu, 0x774bb0ebu, 0x4f040d56u, 0x4bc510e1u,
    0x46863638u, 0x42472b8fu, 0x5c007b8au, 0x58c1663du, 0x558240e4u, 0x51435d53u,
    0x251d3b9eu, 0x21dc2629u, 0x2c9f00f0u, 0x285e1d47u, 0x36194d42u, 0x32d850f5u,
    0x3f9b762cu, 0x3b5a6b9bu, 0x0315d626u, 0x07d4cb91u, 0x0a97ed48u, 0x0e56f0ffu,
    0x1011a0fau, 0x14d0bd4du, 0x19939b94u, 0x1d528623u, 0xf12f560eu, 0xf5ee4bb9u,
    0xf8ad6d60u, 0xfc6c70d7u, 0xe22b20d2u, 0xe6ea3d65u, 0xeba91bbcu, 0xef68060bu,
    0xd727bbb6u, 0xd3e6a601u, 0xdea580d8u, 0xda649d6fu, 0xc423cd6au, 0xc0e2d0ddu,
    0xcda1f604u, 0xc960ebb3u, 0xbd3e8d7eu, 0xb9ff90c9u, 0xb4bcb610u, 0xb07daba7u,
    0xae3afba2u, 0xaafbe615u, 0xa7b8c0ccu, 0xa379dd7bu, 0x9b3660c6u, 0x9ff77d71u,
    0x92b45ba8u, 0x9675461fu, 0x8832161au, 0x8cf30badu, 0x81b02d74u, 0x857130c3u,
    0x5d8a9099u, 0x594b8d2eu, 0x5408abf7u, 0x50c9b640u, 0x4e8ee645u, 0x4a4ffbf2u,
    0x470cdd2bu, 0x43cdc09cu, 0x7b827d21u, 0x7f436096u, 0x7200464fu, 0x76c15bf8u,
    0x68860bfdu, 0x6c47164au, 0x61043093u, 0x65c52d24u, 0x119b4be9u, 0x155a565eu,
    0x18197087u, 0x1cd86d30u, 0x029f3d35u, 0x065e2082u, 0x0b1d065bu, 0x0fdc1becu,
    0x3793a651u, 0x3352bbe6u, 0x3e119d3fu, 0x3ad08088u, 0x2497d08du, 0x2056cd3au,
    0x2d15ebe3u, 0x29d4f654u, 0xc5a92679u, 0xc1683bceu, 0xcc2b1d17u, 0xc8ea00a0u,
    0xd6ad50a5u, 0xd26c4d12u, 0xdf2f6bcbu, 0xdbee767cu, 0xe3a1cbc1u, 0xe760d676u,
    0xea23f0afu, 0xeee2ed18u, 0xf0a5bd1du, 0xf464a0aau, 0xf9278673u, 0xfde69bc4u,
    0x89b8fd09u, 0x8d79e0beu, 0x803ac667u, 0x84fbdbd0u, 0x9abc8bd5u, 0x9e7d9662u,
    0x933eb0bbu, 0x97ffad0cu, 0xafb010b1u, 0xab710d06u, 0xa6322bdfu, 0xa2f33668u,
    0xbcb4666du, 0xb8757bdau, 0xb5365d03u, 0xb1f740b4u,
};

uint32_t ogg_crc_update(uint32_t crc, const void *data, size_t len)
{
    const uint8_t *p = data;
    while (len--) {
        crc = (crc << 8) ^ s_ogg_table[((crc >> 24) ^ *p++) & 0xFF];
    }
    return crc;
}

static void put_le16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}

static void put_le32(uint8_t *p, uint32_t v)
{
    for (int i = 0; i < 4; i++) {
        p[i] = (uint8_t)(v >> (8 * i));
    }
}

static void put_le64(uint8_t *p, uint64_t v)
{
    for (int i = 0; i < 8; i++) {
        p[i] = (uint8_t)(v >> (8 * i));
    }
}

static uint32_t get_le32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static uint64_t get_le64(const uint8_t *p)
{
    return (uint64_t)get_le32(p) | (uint64_t)get_le32(p + 4) << 32;
}

/* Lacing values for one packet of `len` bytes into the buffered page (RFC 3533 §5): a run
 * of 255s and a final value < 255, so a packet of exactly 255 bytes ends with a 0. */
static void lace(ogg_opus_writer_t *w, size_t len)
{
    while (len >= 255) {
        w->segs[w->nsegs++] = 255;
        len -= 255;
    }
    w->segs[w->nsegs++] = (uint8_t)len;
}

static int write_page(ogg_opus_writer_t *w, uint8_t flags, uint64_t granule)
{
    uint8_t *const page = w->buf + BODY_OFF - w->nsegs - HDR_LEN;
    memcpy(page, "OggS", 4);
    page[4] = 0; /* stream structure version */
    page[5] = flags;
    put_le64(page + 6, granule);
    put_le32(page + 14, w->serial);
    put_le32(page + 18, w->page_seq);
    put_le32(page + 22, 0);
    page[26] = (uint8_t)w->nsegs;
    memcpy(page + HDR_LEN, w->segs, (size_t)w->nsegs);
    const size_t len = HDR_LEN + (size_t)w->nsegs + w->body_len;
    put_le32(page + 22, ogg_crc_update(0, page, len));
    if (w->write(w->ctx, page, len) != 0) {
        w->finished = true; /* a page may be half on the card: stop here */
        return OGG_OPUS_ERR_WRITE;
    }
    w->page_seq++;
    w->bytes += len;
    w->nsegs = 0;
    w->body_len = 0;
    w->page_packets = 0;
    w->page_start = w->granule;
    return OGG_OPUS_OK;
}

/* Header packets go on pages of their own (RFC 7845 §3). */
static int write_header_packet(ogg_opus_writer_t *w, uint8_t flags, size_t len)
{
    w->body_len = len;
    lace(w, len);
    return write_page(w, flags, 0);
}

int ogg_opus_begin(ogg_opus_writer_t *w, const ogg_opus_head_t *head, ogg_opus_write_fn write,
                   void *ctx)
{
    if (!w || !head || !write) {
        return OGG_OPUS_ERR_ARG;
    }
    memset(w, 0, offsetof(ogg_opus_writer_t, buf));
    w->write = write;
    w->ctx = ctx;
    w->serial = head->serial;
    w->started = true;

    /* OpusHead (RFC 7845 §5.1): version 1, mono, mapping family 0, no output gain. */
    uint8_t *b = w->buf + BODY_OFF;
    memcpy(b, "OpusHead", 8);
    b[8] = 1;
    b[9] = 1;
    put_le16(b + 10, head->pre_skip);
    put_le32(b + 12, head->input_rate);
    put_le16(b + 16, 0);
    b[18] = 0;
    int err = write_header_packet(w, FLAG_BOS, 19);
    if (err) {
        return err;
    }

    /* OpusTags (RFC 7845 §5.2). */
    const char *vendor = head->vendor ? head->vendor : OGG_OPUS_VENDOR;
    size_t n = 0;
    const size_t vlen = strlen(vendor);
    if (8 + 4 + vlen + 4 > OGG_OPUS_MAX_BODY) {
        return OGG_OPUS_ERR_ARG;
    }
    memcpy(b, "OpusTags", 8);
    put_le32(b + 8, (uint32_t)vlen);
    memcpy(b + 12, vendor, vlen);
    n = 12 + vlen + 4;
    uint32_t count = 0;
    for (int i = 0; i < OGG_OPUS_MAX_COMMENTS; i++) {
        if (!head->comments[i]) {
            continue;
        }
        const size_t clen = strlen(head->comments[i]);
        if (n + 4 + clen > OGG_OPUS_MAX_BODY) {
            return OGG_OPUS_ERR_ARG;
        }
        put_le32(b + n, (uint32_t)clen);
        memcpy(b + n + 4, head->comments[i], clen);
        n += 4 + clen;
        count++;
    }
    put_le32(b + 12 + vlen, count);
    return write_header_packet(w, 0, n);
}

int ogg_opus_add_packet(ogg_opus_writer_t *w, const uint8_t *packet, size_t len,
                        uint32_t samples48)
{
    if (!w || !w->started || w->finished) {
        return OGG_OPUS_ERR_STATE;
    }
    if (!packet || len == 0 || len > OGG_OPUS_MAX_PACKET) {
        return OGG_OPUS_ERR_ARG;
    }
    const int need = (int)(len / 255) + 1;
    if (w->page_packets >= OGG_OPUS_PAGE_PACKETS || w->nsegs + need > 255 ||
        w->body_len + len > OGG_OPUS_MAX_BODY) {
        const int err = write_page(w, 0, w->granule);
        if (err) {
            return err;
        }
    }
    memcpy(w->buf + BODY_OFF + w->body_len, packet, len);
    w->body_len += len;
    lace(w, len);
    w->page_packets++;
    w->packets++;
    w->granule += samples48;
    return OGG_OPUS_OK;
}

int ogg_opus_flush(ogg_opus_writer_t *w)
{
    if (!w || !w->started || w->finished) {
        return OGG_OPUS_ERR_STATE;
    }
    return w->page_packets ? write_page(w, 0, w->granule) : OGG_OPUS_OK;
}

int ogg_opus_finish(ogg_opus_writer_t *w, uint32_t end_trim48)
{
    if (!w || !w->started || w->finished || w->page_packets == 0) {
        return OGG_OPUS_ERR_STATE;
    }
    if (end_trim48 > w->granule - w->page_start) {
        return OGG_OPUS_ERR_ARG;
    }
    const int err = write_page(w, FLAG_EOS, w->granule - end_trim48);
    w->finished = true;
    return err;
}

/* ---- scanner ---- */

/* Reads exactly len bytes; returns 1 if all came, 0 if the file ended, < 0 on error. */
static int read_full(ogg_opus_read_fn read, void *ctx, uint64_t off, void *buf, size_t len)
{
    uint8_t *p = buf;
    while (len) {
        const int got = read(ctx, off, p, len);
        if (got < 0) {
            return OGG_OPUS_ERR_READ;
        }
        if (got == 0) {
            return 0;
        }
        p += got;
        off += (uint64_t)got;
        len -= (size_t)got;
    }
    return 1;
}

int ogg_opus_scan(ogg_opus_read_fn read, void *ctx, ogg_opus_scan_t *out)
{
    memset(out, 0, sizeof(*out));
    uint64_t off = 0;
    uint32_t serial = 0;
    uint8_t hdr[HDR_LEN + 255];
    uint8_t blk[256];
    for (;;) {
        int r = read_full(read, ctx, off, hdr, HDR_LEN);
        if (r <= 0) {
            return r < 0 ? r : OGG_OPUS_OK;
        }
        const uint32_t page_seq = get_le32(hdr + 18);
        if (memcmp(hdr, "OggS", 4) != 0 || hdr[4] != 0 ||
            (out->pages > 0 && get_le32(hdr + 14) != serial) || page_seq != out->pages) {
            return OGG_OPUS_OK;
        }
        const int nsegs = hdr[26];
        r = read_full(read, ctx, off + HDR_LEN, hdr + HDR_LEN, (size_t)nsegs);
        if (r <= 0) {
            return r < 0 ? r : OGG_OPUS_OK;
        }
        size_t body = 0;
        uint32_t completed = 0;
        for (int i = 0; i < nsegs; i++) {
            body += hdr[HDR_LEN + i];
            completed += hdr[HDR_LEN + i] < 255;
        }
        const uint32_t want_crc = get_le32(hdr + 22);
        put_le32(hdr + 22, 0);
        uint32_t crc = ogg_crc_update(0, hdr, HDR_LEN + (size_t)nsegs);
        uint8_t head[19];
        const uint64_t body_off = off + HDR_LEN + (uint64_t)nsegs;
        for (size_t done = 0; done < body;) {
            const size_t n = body - done < sizeof(blk) ? body - done : sizeof(blk);
            r = read_full(read, ctx, body_off + done, blk, n);
            if (r <= 0) {
                return r < 0 ? r : OGG_OPUS_OK;
            }
            if (done == 0 && out->pages == 0) {
                memcpy(head, blk, n < sizeof(head) ? n : sizeof(head));
            }
            crc = ogg_crc_update(crc, blk, n);
            done += n;
        }
        if (crc != want_crc) {
            return OGG_OPUS_OK;
        }
        const uint8_t flags = hdr[5];
        if (out->pages == 0) {
            /* The stream must start with a BOS page holding exactly the OpusHead packet. */
            if (!(flags & FLAG_BOS) || nsegs != 1 || body < 19 || memcmp(head, "OpusHead", 8) != 0) {
                return OGG_OPUS_OK;
            }
            serial = get_le32(hdr + 14);
            out->has_head = true;
            out->pre_skip = (uint16_t)(head[10] | head[11] << 8);
        } else if (out->pages >= 2 && completed > 0) {
            /* Pages 0 and 1 are OpusHead and OpusTags (our writer never spans them). */
            out->has_audio = true;
            out->audio_packets += completed;
            out->granule = get_le64(hdr + 6);
        }
        out->pages++;
        out->eos = (flags & FLAG_EOS) != 0;
        off = body_off + body;
        out->valid_bytes = off;
        if (out->eos) {
            return OGG_OPUS_OK;
        }
    }
}
