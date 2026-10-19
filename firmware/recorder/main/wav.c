#include "wav.h"

#include <string.h>

static void put_u16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)(v >> 8);
}

static void put_u32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
    p[2] = (uint8_t)((v >> 16) & 0xFF);
    p[3] = (uint8_t)((v >> 24) & 0xFF);
}

void wav_header_write(uint8_t out[WAV_HEADER_SIZE], uint32_t sample_rate, uint16_t channels,
                      uint16_t bits_per_sample, uint32_t data_bytes)
{
    const uint16_t block_align = (uint16_t)(channels * (bits_per_sample / 8));
    const uint32_t byte_rate = sample_rate * block_align;

    memcpy(out + 0, "RIFF", 4);
    put_u32(out + 4, 36 + data_bytes);
    memcpy(out + 8, "WAVE", 4);
    memcpy(out + 12, "fmt ", 4);
    put_u32(out + 16, 16);          /* PCM fmt chunk size */
    put_u16(out + 20, 1);           /* PCM */
    put_u16(out + 22, channels);
    put_u32(out + 24, sample_rate);
    put_u32(out + 28, byte_rate);
    put_u16(out + 32, block_align);
    put_u16(out + 34, bits_per_sample);
    memcpy(out + 36, "data", 4);
    put_u32(out + 40, data_bytes);
}

uint32_t wav_data_bytes(uint32_t sample_rate, uint16_t channels, uint16_t bits_per_sample,
                        uint32_t seconds)
{
    return sample_rate * channels * (bits_per_sample / 8) * seconds;
}
