/* Host tests for the mic-array mixer (main/mic_mix.c). */
#include <math.h>
#include <stdint.h>

#include "check.h"
#include "mic_mix.h"

#define FRAMES 64

static int32_t g_a[FRAMES * 2], g_b[FRAMES * 2];

/* A 24-bit sample as an INMP441 puts it in a 32-bit I2S slot. */
static int32_t slot(int32_t s24)
{
    return (int32_t)((uint32_t)s24 << 8);
}

static void fill(int32_t *port, int ch_slot, int32_t s24)
{
    for (int f = 0; f < FRAMES; f++) {
        port[f * 2 + ch_slot] = slot(s24);
    }
}

static void test_single_mic_level(void)
{
    /* One mic at 2^20 (24-bit) -> 2^20 >> 7 = 8192 in 16 bits. */
    fill(g_a, 0, 1 << 20);
    fill(g_a, 1, 0);
    const int32_t *ports[] = {g_a};
    int16_t out[FRAMES];
    mic_mix_frames(ports, 1, FRAMES, 0x1, out);
    CHECK_EQ(out[0], 8192);
    CHECK_EQ(out[FRAMES - 1], 8192);
}

static void test_average_keeps_level(void)
{
    /* Four mics with the same signal give the same level as one. */
    fill(g_a, 0, 1 << 20);
    fill(g_a, 1, 1 << 20);
    fill(g_b, 0, 1 << 20);
    fill(g_b, 1, 1 << 20);
    const int32_t *ports[] = {g_a, g_b};
    int16_t out[FRAMES];
    mic_mix_frames(ports, 2, FRAMES, 0xF, out);
    CHECK_EQ(out[3], 8192);
    /* Masked-out channels don't count: B-R silent, mask without it. */
    fill(g_b, 1, 0);
    mic_mix_frames(ports, 2, FRAMES, 0x7, out);
    CHECK_EQ(out[3], 8192);
    /* With it, the average drops by a quarter. */
    mic_mix_frames(ports, 2, FRAMES, 0xF, out);
    CHECK_EQ(out[3], 6144);
}

static void test_saturation_and_sign(void)
{
    fill(g_a, 0, 8388607);  /* 24-bit max: +6 dB past 16-bit full scale */
    fill(g_a, 1, -8388608); /* 24-bit min */
    const int32_t *ports[] = {g_a};
    int16_t out[FRAMES];
    mic_mix_frames(ports, 1, FRAMES, 0x1, out);
    CHECK_EQ(out[0], INT16_MAX);
    mic_mix_frames(ports, 1, FRAMES, 0x2, out);
    CHECK_EQ(out[0], INT16_MIN);
    fill(g_a, 0, -(1 << 20));
    mic_mix_frames(ports, 1, FRAMES, 0x1, out);
    CHECK_EQ(out[0], -8192);
}

static void test_empty_mask_is_silence(void)
{
    fill(g_a, 0, 12345);
    const int32_t *ports[] = {g_a};
    int16_t out[FRAMES] = {1, 2, 3};
    mic_mix_frames(ports, 1, FRAMES, 0x0, out);
    CHECK_EQ(out[0], 0);
    /* Channels of a port that isn't there are ignored. */
    mic_mix_frames(ports, 1, FRAMES, 0xC, out);
    CHECK_EQ(out[2], 0);
}

static void test_alive_detection(void)
{
    /* A-L: noise; A-R: nothing drives it (0); B-L: stuck at -1; B-R: one LSB of jitter. */
    for (int f = 0; f < FRAMES; f++) {
        g_a[f * 2] = slot((f * 7919) % 801 - 400);
        g_a[f * 2 + 1] = 0;
        g_b[f * 2] = slot(-1);
        g_b[f * 2 + 1] = slot(f & 1 ? 0 : -1);
    }
    const int32_t *ports[] = {g_a, g_b};
    mic_ch_stats_t st[MIC_MIX_MAX_CH];
    mic_mix_stats_reset(st);
    mic_mix_stats_add(st, ports, 2, FRAMES);
    CHECK(mic_mix_alive(&st[0]));
    CHECK(!mic_mix_alive(&st[1]));
    CHECK(!mic_mix_alive(&st[2]));
    CHECK(!mic_mix_alive(&st[3]));
    CHECK_EQ(mic_mix_alive_mask(st, 2), 0x1);
    CHECK_EQ(mic_mix_alive_mask(st, 1), 0x1);
    CHECK(mic_mix_rms_dbfs(&st[1]) <= -144.0);
    /* Full-scale square wave: RMS = 0 dBFS. */
    for (int f = 0; f < FRAMES; f++) {
        g_a[f * 2] = slot(f & 1 ? 8388607 : -8388607);
    }
    mic_mix_stats_reset(st);
    mic_mix_stats_add(st, ports, 1, FRAMES);
    CHECK(fabs(mic_mix_rms_dbfs(&st[0])) < 0.01);
    CHECK_EQ(st[0].count, FRAMES);
    CHECK_EQ(mic_mix_count(0xF), 4);
    CHECK_EQ(mic_mix_count(0x5), 2);
}

int main(void)
{
    test_single_mic_level();
    test_average_keeps_level();
    test_saturation_and_sign();
    test_empty_mask_is_silence();
    test_alive_detection();
    return checks_done("test_mic_mix");
}
