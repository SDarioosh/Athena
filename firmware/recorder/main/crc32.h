/* IEEE 802.3 CRC-32, the same value as zlib's crc32() and Python's zlib.crc32(). Pure C;
 * host-tested. The chunk queue keeps one per file and the BLE protocol checks it end to end
 * (docs/twatch-ble-protocol.md §4.3). */
#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Start with crc = 0 and feed the data in any number of pieces:
 *   crc = crc32_update(0, a, na); crc = crc32_update(crc, b, nb);
 * gives the CRC of a followed by b. */
uint32_t crc32_update(uint32_t crc, const void *data, size_t len);

#ifdef __cplusplus
}
#endif
