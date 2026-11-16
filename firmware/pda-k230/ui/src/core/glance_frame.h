/* glance.sock framing (docs/k230-pda-plan.md Contract 1): `type:u8` `len:u16 LE` `payload`,
 * len <= 512. Pure C; host-tested (test/test_glance.c). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define GLANCE_CTRL 0x01       /* syncd -> UI: one glance command (T5 plan §3.3) */
#define GLANCE_DATA 0x02       /* UI -> syncd: one response (a BLE notification) */
#define GLANCE_STATUS_REQ 0x03 /* syncd -> UI, empty */
#define GLANCE_STATUS 0x04     /* UI -> syncd: the 16-byte status (§3.2) */
#define GLANCE_INFO_REQ 0x05   /* syncd -> UI, empty */
#define GLANCE_INFO 0x06       /* UI -> syncd: proto=1;device=k230-pda;... */
#define GLANCE_LOCAL 0x10      /* both: one UTF-8 JSON object */

#define GLANCE_HDR_LEN 3
#define GLANCE_PAYLOAD_MAX 512
#define GLANCE_FRAME_MAX (GLANCE_HDR_LEN + GLANCE_PAYLOAD_MAX)
/* glance.sock stands in for GATT at this MTU: pda_proto's checks apply unchanged. */
#define GLANCE_MTU 517

typedef void (*glance_frame_cb)(void *ctx, uint8_t type, const uint8_t *payload, size_t len);

typedef struct {
    uint8_t buf[GLANCE_FRAME_MAX];
    size_t have;
} glance_rx_t;

void glance_rx_reset(glance_rx_t *rx);
/* Feed received bytes; calls cb for every complete frame. -1 if a header announces more
 * than 512 bytes (the stream can't be trusted any more: drop the client), else 0. */
int glance_rx_feed(glance_rx_t *rx, const uint8_t *data, size_t n, glance_frame_cb cb, void *ctx);

/* out holds GLANCE_HDR_LEN + len bytes; len <= 512. Returns the frame length, 0 if too long. */
size_t glance_frame_encode(uint8_t type, const void *payload, size_t len, uint8_t *out);

#ifdef __cplusplus
}
#endif
