/* WAV (RIFF/PCM) header writer. Pure C, no ESP-IDF dependencies; covered by host tests. */
#pragma once

#include <stddef.h>
#include <stdint.h>

#define WAV_HEADER_SIZE 44

/* Write a 44-byte canonical PCM WAV header into `out`.
 * data_bytes is the size of the sample data that follows the header. */
void wav_header_write(uint8_t out[WAV_HEADER_SIZE], uint32_t sample_rate, uint16_t channels,
                      uint16_t bits_per_sample, uint32_t data_bytes);

/* Size of the sample data for a duration in seconds, in bytes. */
uint32_t wav_data_bytes(uint32_t sample_rate, uint16_t channels, uint16_t bits_per_sample,
                        uint32_t seconds);
