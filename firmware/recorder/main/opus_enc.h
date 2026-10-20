/* Opus encoder for the session recorder: 16 kHz mono, 20 ms frames, VBR, VoIP, DTX off
 * (docs/twatch-ble-protocol.md §2.1). Wraps libopus from the 78/esp-opus component; the
 * encoder state (~25 KB) lives in internal RAM, not PSRAM, because the encoder touches all
 * of it every frame.
 *
 * Cost (host instruction counts of the same fixed-point libopus, 16 kbps speech-like
 * input): ~0.60 M instructions per frame at complexity 0, ~0.67 M at 1, ~0.87 M at 2,
 * ~0.96 M at 3. At 80 MHz a 20 ms frame has 1.6 M cycles, so expect roughly 40-60 % of one
 * core at complexity 1 on the watch; session_rec logs the measured time per frame. */
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define OPUS_ENC_SAMPLE_RATE 16000
#define OPUS_ENC_FRAME_SAMPLES 320     /* 20 ms at 16 kHz */
#define OPUS_ENC_FRAME_SAMPLES_48K 960 /* the same 20 ms in Ogg granule units */
#define OPUS_ENC_MAX_PACKET 1275
/* Milestone 3 picks bitrate and complexity by transcription accuracy (plan §7.4). Complexity
 * 1 over 0: better pitch analysis and noise shaping for ~10 % more CPU; 2 and up add the
 * delayed-decision quantiser for another ~30 %.
 * Bitrate 24 kbps, not 16. Two server evals agree: in the recognition eval (AMI far-field
 * speech at the watch's ~-24 dBFS, complexity 1) 16 kbps lost ~7 WER points to uncompressed
 * audio (small.en) and ~9 (large-v3-turbo), 24 kbps came within ~1.3; in the room eval
 * (docs/omi-audio-quality.md §7) 16 kbps cost 4-5 points for people 2-3 m away and 24 kbps
 * matched uncompressed audio. Bitrate costs little encoder CPU (unlike complexity; check
 * session_rec's time per frame on the watch) but 50 % more SD space and BLE time (a 30 s
 * chunk ~90 KB instead of ~60 KB). */
#define OPUS_ENC_DEFAULT_BITRATE 24000
#define OPUS_ENC_DEFAULT_COMPLEXITY 1

typedef struct opus_enc opus_enc_t;

typedef struct {
    int bitrate_bps; /* VBR target */
    int complexity;  /* 0..10 */
} opus_enc_config_t;

#define OPUS_ENC_CONFIG_DEFAULT() \
    { .bitrate_bps = OPUS_ENC_DEFAULT_BITRATE, .complexity = OPUS_ENC_DEFAULT_COMPLEXITY }

esp_err_t opus_enc_create(const opus_enc_config_t *cfg, opus_enc_t **out);
void opus_enc_destroy(opus_enc_t *enc);

/* The encoder's delay in 48 kHz samples: OpusHead pre_skip (312 for libopus at 16 kHz). */
uint16_t opus_enc_pre_skip(const opus_enc_t *enc);

/* Encode one 20 ms frame (OPUS_ENC_FRAME_SAMPLES samples). out needs OPUS_ENC_MAX_PACKET
 * bytes. */
esp_err_t opus_enc_encode(opus_enc_t *enc, const int16_t *pcm, uint8_t *out, size_t *out_len);

/* Back to the initial state, so the next packet starts a fresh, standalone stream. */
esp_err_t opus_enc_reset(opus_enc_t *enc);

#ifdef __cplusplus
}
#endif
