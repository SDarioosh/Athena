#include "check.h"
#include "wav.h"

#include <string.h>

static uint32_t rd32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static uint16_t rd16(const uint8_t *p)
{
    return (uint16_t)(p[0] | (p[1] << 8));
}

int main(void)
{
    uint8_t h[WAV_HEADER_SIZE];
    const uint32_t data = wav_data_bytes(16000, 1, 16, 60);
    CHECK_EQ(data, 16000u * 2u * 60u);

    wav_header_write(h, 16000, 1, 16, data);
    CHECK(memcmp(h, "RIFF", 4) == 0);
    CHECK_EQ(rd32(h + 4), 36 + data);
    CHECK(memcmp(h + 8, "WAVE", 4) == 0);
    CHECK(memcmp(h + 12, "fmt ", 4) == 0);
    CHECK_EQ(rd32(h + 16), 16);
    CHECK_EQ(rd16(h + 20), 1);     /* PCM */
    CHECK_EQ(rd16(h + 22), 1);     /* mono */
    CHECK_EQ(rd32(h + 24), 16000); /* sample rate */
    CHECK_EQ(rd32(h + 28), 32000); /* byte rate */
    CHECK_EQ(rd16(h + 32), 2);     /* block align */
    CHECK_EQ(rd16(h + 34), 16);    /* bits */
    CHECK(memcmp(h + 36, "data", 4) == 0);
    CHECK_EQ(rd32(h + 40), data);

    /* A placeholder header (size 0) is what gets written before recording. */
    wav_header_write(h, 16000, 1, 16, 0);
    CHECK_EQ(rd32(h + 4), 36);
    CHECK_EQ(rd32(h + 40), 0);

    /* Stereo 48 kHz 24-bit, for the arithmetic. */
    wav_header_write(h, 48000, 2, 24, 6);
    CHECK_EQ(rd32(h + 28), 48000 * 6);
    CHECK_EQ(rd16(h + 32), 6);

    return checks_done("test_wav");
}
