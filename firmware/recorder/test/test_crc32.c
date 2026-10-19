#include <string.h>

#include "check.h"
#include "crc32.h"

int main(void)
{
    /* The standard check value, and zlib.crc32() of a pangram. */
    CHECK_EQ(crc32_update(0, "123456789", 9), 0xCBF43926u);
    const char *fox = "The quick brown fox jumps over the lazy dog";
    CHECK_EQ(crc32_update(0, fox, strlen(fox)), 0x414FA339u);
    CHECK_EQ(crc32_update(0, "", 0), 0);

    /* Fed in pieces, of any size, it gives the same value. */
    for (size_t split = 0; split <= strlen(fox); split++) {
        uint32_t crc = crc32_update(0, fox, split);
        crc = crc32_update(crc, fox + split, strlen(fox) - split);
        CHECK_EQ(crc, 0x414FA339u);
    }

    /* Every byte value goes through the table. */
    unsigned char all[256];
    for (int i = 0; i < 256; i++) {
        all[i] = (unsigned char)i;
    }
    CHECK_EQ(crc32_update(0, all, sizeof(all)), 0x29058C73u);
    return checks_done("test_crc32");
}
