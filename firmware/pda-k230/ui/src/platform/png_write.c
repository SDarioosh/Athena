/* A minimal PNG writer (8-bit RGB): zlib's deflate when built with it (PDA_HAVE_ZLIB),
 * else stored blocks, so the screenshot tool needs nothing extra. CRC-32 from the recorder's
 * crc32.c (the same polynomial as PNG's). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "crc32.h"
#include "platform.h"

#ifdef PDA_HAVE_ZLIB
#include <zlib.h>
#endif

static void be32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

static bool chunk(FILE *f, const char *type, const uint8_t *data, size_t len)
{
    uint8_t hdr[8];
    be32(hdr, (uint32_t)len);
    memcpy(hdr + 4, type, 4);
    uint32_t crc = crc32_update(0, type, 4);
    crc = crc32_update(crc, data, len);
    uint8_t tail[4];
    be32(tail, crc);
    return fwrite(hdr, 1, 8, f) == 8 && (len == 0 || fwrite(data, 1, len, f) == len) &&
           fwrite(tail, 1, 4, f) == 4;
}

#ifndef PDA_HAVE_ZLIB
/* zlib stream of stored blocks */
static uint8_t *deflate_stored(const uint8_t *raw, size_t n, size_t *out_len)
{
    const size_t blocks = n / 65535 + 1;
    uint8_t *z = malloc(2 + n + blocks * 5 + 4);
    if (!z) {
        return NULL;
    }
    size_t w = 0;
    z[w++] = 0x78;
    z[w++] = 0x01;
    size_t off = 0;
    do {
        const size_t len = n - off > 65535 ? 65535 : n - off;
        z[w++] = off + len == n ? 1 : 0;
        z[w++] = (uint8_t)(len & 0xff);
        z[w++] = (uint8_t)(len >> 8);
        z[w++] = (uint8_t)(~len & 0xff);
        z[w++] = (uint8_t)((~len >> 8) & 0xff);
        memcpy(z + w, raw + off, len);
        w += len;
        off += len;
    } while (off < n);
    uint32_t a = 1, b = 0;
    for (size_t i = 0; i < n; i++) {
        a = (a + raw[i]) % 65521;
        b = (b + a) % 65521;
    }
    be32(z + w, b << 16 | a);
    *out_len = w + 4;
    return z;
}
#endif

int png_write_xrgb(const char *path, const uint32_t *px, int32_t w, int32_t h, int32_t stride)
{
    const size_t row = (size_t)w * 3 + 1;
    const size_t n = row * (size_t)h;
    uint8_t *raw = malloc(n);
    if (!raw) {
        return -1;
    }
    for (int32_t y = 0; y < h; y++) {
        uint8_t *r = raw + (size_t)y * row;
        r[0] = 0; /* filter: none */
        for (int32_t x = 0; x < w; x++) {
            const uint32_t c = px[(size_t)y * (size_t)stride + (size_t)x];
            r[1 + x * 3] = (uint8_t)(c >> 16);
            r[2 + x * 3] = (uint8_t)(c >> 8);
            r[3 + x * 3] = (uint8_t)c;
        }
    }
    size_t zlen = 0;
    uint8_t *z;
#ifdef PDA_HAVE_ZLIB
    uLongf cap = compressBound((uLong)n);
    z = malloc(cap);
    if (z && compress2(z, &cap, raw, (uLong)n, 6) != Z_OK) {
        free(z);
        z = NULL;
    }
    zlen = cap;
#else
    z = deflate_stored(raw, n, &zlen);
#endif
    free(raw);
    if (!z) {
        return -1;
    }
    FILE *f = fopen(path, "wb");
    if (!f) {
        free(z);
        return -1;
    }
    static const uint8_t sig[8] = {0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'};
    uint8_t ihdr[13];
    be32(ihdr, (uint32_t)w);
    be32(ihdr + 4, (uint32_t)h);
    ihdr[8] = 8; /* bit depth */
    ihdr[9] = 2; /* RGB */
    ihdr[10] = ihdr[11] = ihdr[12] = 0;
    bool ok = fwrite(sig, 1, 8, f) == 8 && chunk(f, "IHDR", ihdr, 13) && chunk(f, "IDAT", z, zlen) &&
              chunk(f, "IEND", NULL, 0);
    ok &= fclose(f) == 0;
    free(z);
    return ok ? 0 : -1;
}
