#include "glance_frame.h"

#include <string.h>

void glance_rx_reset(glance_rx_t *rx)
{
    rx->have = 0;
}

int glance_rx_feed(glance_rx_t *rx, const uint8_t *data, size_t n, glance_frame_cb cb, void *ctx)
{
    while (n > 0) {
        if (rx->have < GLANCE_HDR_LEN) {
            rx->buf[rx->have++] = *data++;
            n--;
            if (rx->have == GLANCE_HDR_LEN) {
                const size_t len = (size_t)rx->buf[1] | (size_t)rx->buf[2] << 8;
                if (len > GLANCE_PAYLOAD_MAX) {
                    rx->have = 0;
                    return -1;
                }
                if (len == 0) {
                    rx->have = 0;
                    cb(ctx, rx->buf[0], rx->buf + GLANCE_HDR_LEN, 0);
                }
            }
            continue;
        }
        const size_t len = (size_t)rx->buf[1] | (size_t)rx->buf[2] << 8;
        const size_t want = GLANCE_HDR_LEN + len - rx->have;
        const size_t take = n < want ? n : want;
        memcpy(rx->buf + rx->have, data, take);
        rx->have += take;
        data += take;
        n -= take;
        if (rx->have == GLANCE_HDR_LEN + len) {
            rx->have = 0;
            cb(ctx, rx->buf[0], rx->buf + GLANCE_HDR_LEN, len);
        }
    }
    return 0;
}

size_t glance_frame_encode(uint8_t type, const void *payload, size_t len, uint8_t *out)
{
    if (len > GLANCE_PAYLOAD_MAX) {
        return 0;
    }
    out[0] = type;
    out[1] = (uint8_t)(len & 0xff);
    out[2] = (uint8_t)(len >> 8);
    if (len) {
        memcpy(out + GLANCE_HDR_LEN, payload, len);
    }
    return GLANCE_HDR_LEN + len;
}
