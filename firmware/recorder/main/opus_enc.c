#include "opus_enc.h"

#include <stdlib.h>

#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "opus.h"

static const char *TAG = "opus_enc";

struct opus_enc {
    OpusEncoder *st; /* opus_encoder_get_size() bytes, internal RAM */
    uint16_t pre_skip;
};

static esp_err_t ctl_err(int r)
{
    return r == OPUS_OK ? ESP_OK : ESP_FAIL;
}

esp_err_t opus_enc_create(const opus_enc_config_t *cfg, opus_enc_t **out)
{
    ESP_RETURN_ON_FALSE(cfg && out, ESP_ERR_INVALID_ARG, TAG, "args");
    *out = NULL;
    opus_enc_t *enc = calloc(1, sizeof(*enc));
    ESP_RETURN_ON_FALSE(enc, ESP_ERR_NO_MEM, TAG, "no memory");
    const int size = opus_encoder_get_size(1);
    enc->st = heap_caps_malloc((size_t)size, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!enc->st) {
        free(enc);
        ESP_LOGE(TAG, "no internal RAM for the encoder (%d bytes)", size);
        return ESP_ERR_NO_MEM;
    }
    esp_err_t err = ctl_err(opus_encoder_init(enc->st, OPUS_ENC_SAMPLE_RATE, 1,
                                              OPUS_APPLICATION_VOIP));
    /* VBR without the constraint, voice hint, no DTX and no FEC: chunks go to a file, not
     * over a lossy link, and v1 keeps silence as audio (protocol §2.1). */
    if (err == ESP_OK) {
        err = ctl_err(opus_encoder_ctl(enc->st, OPUS_SET_BITRATE(cfg->bitrate_bps)));
    }
    if (err == ESP_OK) {
        err = ctl_err(opus_encoder_ctl(enc->st, OPUS_SET_VBR(1)));
    }
    if (err == ESP_OK) {
        err = ctl_err(opus_encoder_ctl(enc->st, OPUS_SET_VBR_CONSTRAINT(0)));
    }
    if (err == ESP_OK) {
        err = ctl_err(opus_encoder_ctl(enc->st, OPUS_SET_COMPLEXITY(cfg->complexity)));
    }
    if (err == ESP_OK) {
        err = ctl_err(opus_encoder_ctl(enc->st, OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE)));
    }
    if (err == ESP_OK) {
        err = ctl_err(opus_encoder_ctl(enc->st, OPUS_SET_DTX(0)));
    }
    if (err == ESP_OK) {
        err = ctl_err(opus_encoder_ctl(enc->st, OPUS_SET_INBAND_FEC(0)));
    }
    if (err == ESP_OK) {
        err = ctl_err(opus_encoder_ctl(enc->st, OPUS_SET_LSB_DEPTH(16)));
    }
    opus_int32 lookahead = 0;
    if (err == ESP_OK) {
        err = ctl_err(opus_encoder_ctl(enc->st, OPUS_GET_LOOKAHEAD(&lookahead)));
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "encoder setup failed");
        opus_enc_destroy(enc);
        return err;
    }
    /* Lookahead is in input samples (16 kHz); pre_skip counts 48 kHz samples. */
    enc->pre_skip = (uint16_t)(lookahead * (48000 / OPUS_ENC_SAMPLE_RATE));
    ESP_LOGI(TAG, "%s: %d bps VBR, complexity %d, %d bytes state, pre-skip %u",
             opus_get_version_string(), cfg->bitrate_bps, cfg->complexity, size, enc->pre_skip);
    *out = enc;
    return ESP_OK;
}

void opus_enc_destroy(opus_enc_t *enc)
{
    if (enc) {
        heap_caps_free(enc->st);
        free(enc);
    }
}

uint16_t opus_enc_pre_skip(const opus_enc_t *enc)
{
    return enc->pre_skip;
}

esp_err_t opus_enc_encode(opus_enc_t *enc, const int16_t *pcm, uint8_t *out, size_t *out_len)
{
    const opus_int32 n = opus_encode(enc->st, pcm, OPUS_ENC_FRAME_SAMPLES, out, OPUS_ENC_MAX_PACKET);
    if (n < 0) {
        *out_len = 0;
        ESP_LOGE(TAG, "encode: %s", opus_strerror(n));
        return ESP_FAIL;
    }
    *out_len = (size_t)n;
    return ESP_OK;
}

esp_err_t opus_enc_reset(opus_enc_t *enc)
{
    return ctl_err(opus_encoder_ctl(enc->st, OPUS_RESET_STATE));
}
