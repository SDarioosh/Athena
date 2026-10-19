#include "session_rec.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>

#include "chunk_queue.h"
#include "chunk_store.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "isotime.h"
#include "mic.h"
#include "ogg_opus.h"
#include "opus_enc.h"

static const char *TAG = "session";

/* libopus keeps its scratch buffers on the stack (alloca): ~25 KB peak measured on the host.
 * The task logs its stack high-water mark at the end of each session. */
#define TASK_STACK_BYTES (32 * 1024)
#define TASK_PRIORITY 8
#define FRAME_MS 20
#define MIC_TIMEOUT_MS 500
#define MIC_MAX_EMPTY_READS 20 /* ~10 s without audio: the mic is gone */

typedef struct {
    opus_enc_t *enc;
    ogg_opus_writer_t *ogg; /* ~8.5 KB page buffer */
    uint16_t pre_skip;
    uint8_t session_id[8];
    char session_hex[17];
    session_rec_kind_t kind;
    uint64_t max_packets;  /* 0 = no limit (conversation) */
    bool time_unset;
    int64_t start_ms;      /* session start, epoch ms (valid unless time_unset) */
    int64_t mono0_ms;      /* esp_timer ms at the session start */
    uint64_t packets;      /* real packets of this session so far: the time base */
    uint32_t sequence;     /* of the open (or next) chunk */
    bool chunk_open;
    uint32_t chunk_id;
    uint32_t prev_chunk_id; /* last closed chunk of this session, 0 if none */
    uint32_t chunk_packets;
    esp_err_t write_err;   /* set by the Ogg write callback */
    uint64_t enc_total_us;
    int16_t frame[OPUS_ENC_FRAME_SAMPLES];
    uint8_t packet[OPUS_ENC_MAX_PACKET];
} session_t;

static session_rec_state_t s_state;
static portMUX_TYPE s_state_lock = portMUX_INITIALIZER_UNLOCKED;
static volatile bool s_stop;
static session_rec_kind_t s_kind; /* of the session being started */
static session_rec_done_cb_t s_done;
static session_rec_tick_cb_t s_tick;
static void *s_ctx;

static const int16_t s_silence[OPUS_ENC_FRAME_SAMPLES];

#define STATE_UPDATE(...)                    \
    do {                                     \
        taskENTER_CRITICAL(&s_state_lock);   \
        __VA_ARGS__;                         \
        taskEXIT_CRITICAL(&s_state_lock);    \
    } while (0)

/* Every page the writer produces goes to the card at once and is fsynced: a power cut
 * loses at most the page being filled (~1 s, protocol §2.1). The cost is a few SD sector
 * writes per second; milestone 2 measures it. */
static int ogg_write(void *ctx, const void *data, size_t len)
{
    session_t *s = ctx;
    esp_err_t err = chunk_store_append(data, len);
    if (err == ESP_OK) {
        err = chunk_store_sync();
    }
    if (err != ESP_OK) {
        s->write_err = err;
        return -1;
    }
    return 0;
}

static int64_t mono_ms(void)
{
    return esp_timer_get_time() / 1000;
}

static bool clock_epoch_ms(int64_t *out)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    *out = (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
    return isotime_is_plausible(tv.tv_sec);
}

static esp_err_t open_chunk(session_t *s)
{
    /* A session that started without a clock switches to epoch time as soon as the clock is
     * set (relay TIME); the chunks already queued are converted by chunk_store_fix_time(). */
    int64_t now_epoch;
    if (s->time_unset && clock_epoch_ms(&now_epoch)) {
        s->start_ms = now_epoch - (mono_ms() - s->mono0_ms);
        s->time_unset = false;
        STATE_UPDATE(s_state.time_unset = false);
        ESP_LOGI(TAG, "clock set: session start is now %" PRId64 " ms epoch", s->start_ms);
    }
    /* Chunks are contiguous: each starts where the previous one's audio ended. */
    const uint64_t offset_ms = s->packets * FRAME_MS;
    chunk_store_new_t c = {
        .sequence = s->sequence,
        .start_ms = s->time_unset ? offset_ms : (uint64_t)s->start_ms + offset_ms,
        .flags = (uint8_t)((s->time_unset ? CHUNK_FLAG_TIME_UNSET : 0) |
                           (s->kind == SESSION_KIND_NOTE ? CHUNK_FLAG_NOTE : 0)),
        .mono_ms = (uint64_t)s->mono0_ms + offset_ms,
    };
    memcpy(c.session_id, s->session_id, sizeof(c.session_id));
    ESP_RETURN_ON_ERROR(chunk_store_begin(&c, &s->chunk_id), TAG, "begin chunk");

    char seq[32], start[40];
    snprintf(seq, sizeof(seq), "BARVIS_SEQUENCE=%" PRIu32, s->sequence);
    snprintf(start, sizeof(start), "BARVIS_START_MS=%" PRIu64, c.start_ms);
    char session[40];
    snprintf(session, sizeof(session), "BARVIS_SESSION=%s", s->session_hex);
    const ogg_opus_head_t head = {
        .serial = esp_random(),
        .pre_skip = s->pre_skip,
        .input_rate = OPUS_ENC_SAMPLE_RATE,
        .comments = {session, seq, start},
    };
    s->chunk_open = true;
    s->chunk_packets = 0;
    s->write_err = ESP_OK;
    if (ogg_opus_begin(s->ogg, &head, ogg_write, s) != OGG_OPUS_OK) {
        return s->write_err != ESP_OK ? s->write_err : ESP_FAIL;
    }
    STATE_UPDATE(s_state.sequence = s->sequence);
    return ESP_OK;
}

/* The chunk can't be finished normally: keep what is on the card (cut back to the last
 * complete page, with LAST), or give LAST to the previous chunk if nothing is left. */
static void abandon_chunk(session_t *s)
{
    if (!s->chunk_open) {
        return;
    }
    s->chunk_open = false;
    uint32_t kept = 0;
    const esp_err_t err = chunk_store_abandon(&kept);
    ESP_LOGW(TAG, "chunk %08" PRIx32 " abandoned: %s (%s)", s->chunk_id,
             kept ? "kept up to its last page" : "nothing to keep", esp_err_to_name(err));
    if (kept) {
        STATE_UPDATE(s_state.chunks_written++);
    }
}

/* Close the open chunk. `next` is the frame after the chunk's last one: the next chunk's
 * first frame, or silence at the end of the session. It is encoded once more with this
 * chunk's encoder state and added as a final packet, so the encoder's lookahead (the last
 * ~6.5 ms of the chunk) still decodes; the end trim cuts the rest. The encoder then starts
 * fresh, so every chunk is a standalone stream that decodes to exactly its 20 ms x packets,
 * and back to back the chunks reproduce the session without a gap or overlap. */
static esp_err_t close_chunk(session_t *s, const int16_t *next, bool last)
{
    size_t len = 0;
    esp_err_t err = opus_enc_encode(s->enc, next, s->packet, &len);
    if (err == ESP_OK &&
        (ogg_opus_add_packet(s->ogg, s->packet, len, OPUS_ENC_FRAME_SAMPLES_48K) != OGG_OPUS_OK ||
         ogg_opus_finish(s->ogg, OPUS_ENC_FRAME_SAMPLES_48K - s->pre_skip) != OGG_OPUS_OK)) {
        err = s->write_err != ESP_OK ? s->write_err : ESP_FAIL;
    }
    if (err != ESP_OK) {
        abandon_chunk(s);
        return err;
    }
    s->chunk_open = false;
    const uint32_t duration_ms = s->chunk_packets * FRAME_MS;
    err = chunk_store_end(duration_ms, last);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "close chunk %08" PRIx32 ": %s", s->chunk_id, esp_err_to_name(err));
        return err;
    }
    opus_enc_reset(s->enc);
    s->prev_chunk_id = s->chunk_id;
    s->sequence++;

    session_rec_state_t st;
    STATE_UPDATE(s_state.chunks_written++; st = s_state);
    ESP_LOGI(TAG,
             "chunk %08" PRIx32 " seq %" PRIu32 "%s: %" PRIu32 " ms, %" PRIu64
             " bytes; encode avg %" PRIu32 " us, max %" PRIu32 " us per 20 ms frame",
             s->chunk_id, st.sequence, last ? " LAST" : "", duration_ms, s->ogg->bytes,
             st.enc_avg_us, st.enc_max_us);
    return ESP_OK;
}

static bool card_full(void)
{
    uint64_t free_bytes = 0;
    const esp_err_t err = chunk_store_free_bytes(&free_bytes);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "free space unknown: %s", esp_err_to_name(err));
        return false; /* a write error will stop us if the card is really gone */
    }
    if (free_bytes < SESSION_REC_MIN_FREE_BYTES) {
        ESP_LOGW(TAG, "card full: %" PRIu64 " KB free", free_bytes / 1024);
        return true;
    }
    return false;
}

/* Read one 20 ms frame. ESP_ERR_INVALID_STATE when a stop was requested. */
static esp_err_t read_frame(session_t *s)
{
    size_t have = 0;
    int empty = 0;
    while (have < OPUS_ENC_FRAME_SAMPLES) {
        if (s_stop) {
            return ESP_ERR_INVALID_STATE;
        }
        size_t got = 0;
        const esp_err_t r = mic_read(s->frame + have, OPUS_ENC_FRAME_SAMPLES - have, &got,
                                     MIC_TIMEOUT_MS);
        if (got == 0) {
            if (++empty > MIC_MAX_EMPTY_READS) {
                ESP_LOGE(TAG, "mic read failing: %s", esp_err_to_name(r));
                return r == ESP_OK ? ESP_ERR_TIMEOUT : r;
            }
            continue;
        }
        empty = 0;
        have += got;
    }
    return ESP_OK;
}

static esp_err_t encode_frame(session_t *s)
{
    size_t len = 0;
    const int64_t t0 = esp_timer_get_time();
    ESP_RETURN_ON_ERROR(opus_enc_encode(s->enc, s->frame, s->packet, &len), TAG, "encode");
    const uint32_t us = (uint32_t)(esp_timer_get_time() - t0);
    s->enc_total_us += us;
    STATE_UPDATE({
        s_state.enc_frames++;
        s_state.enc_avg_us = (uint32_t)(s->enc_total_us / s_state.enc_frames);
        if (us > s_state.enc_max_us) {
            s_state.enc_max_us = us;
        }
    });
    if (ogg_opus_add_packet(s->ogg, s->packet, len, OPUS_ENC_FRAME_SAMPLES_48K) != OGG_OPUS_OK) {
        return s->write_err != ESP_OK ? s->write_err : ESP_FAIL;
    }
    s->chunk_packets++;
    s->packets++;
    return ESP_OK;
}

static esp_err_t run(session_t *s, session_rec_end_t *why)
{
    *why = SESSION_END_REQUEST;
    ESP_RETURN_ON_ERROR(mic_start(), TAG, "mic");
    s->mono0_ms = mono_ms();
    int64_t epoch;
    s->time_unset = !clock_epoch_ms(&epoch);
    s->start_ms = s->time_unset ? 0 : epoch;
    STATE_UPDATE(s_state.time_unset = s->time_unset);
    ESP_LOGI(TAG, "%s %s started%s", s->kind == SESSION_KIND_NOTE ? "note" : "session",
             s->session_hex, s->time_unset ? " without a valid clock (TIME_UNSET)" : "");

    esp_err_t err = ESP_OK;
    if (card_full()) {
        *why = SESSION_END_CARD_FULL;
    }
    while (*why == SESSION_END_REQUEST) {
        if (s->max_packets && s->packets >= s->max_packets) {
            *why = SESSION_END_MAX; /* the open chunk is closed with LAST below */
            break;
        }
        err = read_frame(s);
        if (err == ESP_ERR_INVALID_STATE) {
            err = ESP_OK; /* stop requested */
            break;
        }
        if (err != ESP_OK) {
            break;
        }
        /* Lazy rotation: a full chunk is closed only now that its successor has audio, so
         * the chunk open at the end of a session always has audio and gets LAST. */
        if (s->chunk_open && s->chunk_packets >= SESSION_REC_CHUNK_PACKETS) {
            const bool full = card_full();
            err = close_chunk(s, s->frame, full);
            if (err != ESP_OK) {
                break;
            }
            if (full) {
                *why = SESSION_END_CARD_FULL;
                break;
            }
        }
        if (!s->chunk_open) {
            err = open_chunk(s);
            if (err != ESP_OK && s->chunk_open) {
                abandon_chunk(s); /* the queue moves LAST back if nothing was kept */
            } else if (err != ESP_OK && s->prev_chunk_id) {
                chunk_store_set_last(s->prev_chunk_id); /* broke off between two chunks */
            }
            if (err != ESP_OK) {
                break;
            }
        }
        err = encode_frame(s);
        if (err != ESP_OK) {
            abandon_chunk(s);
            break;
        }
        if (s->packets % (1000 / FRAME_MS) == 0) {
            const uint32_t elapsed = (uint32_t)(s->packets * FRAME_MS / 1000);
            STATE_UPDATE(s_state.elapsed_s = elapsed);
            if (s_tick) {
                s_tick(elapsed, s_ctx);
            }
        }
    }
    mic_stop();

    if (s->chunk_open) {
        /* Normal end (or a mic failure): the open chunk becomes the session's last. */
        const esp_err_t cerr = close_chunk(s, s_silence, true);
        if (err == ESP_OK) {
            err = cerr;
        }
    }
    if (err != ESP_OK) {
        *why = SESSION_END_ERROR;
    }
    return err;
}

static void session_task(void *arg)
{
    (void)arg;
    session_t *s = heap_caps_calloc(1, sizeof(session_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    ogg_opus_writer_t *ogg = heap_caps_malloc(sizeof(ogg_opus_writer_t),
                                              MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    esp_err_t err = s && ogg ? ESP_OK : ESP_ERR_NO_MEM;
    session_rec_end_t why = SESSION_END_ERROR;
    bool media = false;
    if (err == ESP_OK) {
        s->ogg = ogg;
        s->kind = s_kind;
        s->max_packets = s_kind == SESSION_KIND_NOTE
                             ? (uint64_t)SESSION_REC_NOTE_MAX_S * (1000 / FRAME_MS)
                             : 0;
        esp_fill_random(s->session_id, sizeof(s->session_id));
        cq_session_hex(s->session_id, s->session_hex);
        STATE_UPDATE(memcpy(s_state.session_hex, s->session_hex, sizeof(s_state.session_hex)));
        const opus_enc_config_t cfg = OPUS_ENC_CONFIG_DEFAULT();
        err = opus_enc_create(&cfg, &s->enc);
    }
    if (err == ESP_OK) {
        s->pre_skip = opus_enc_pre_skip(s->enc);
        if (s->pre_skip > OPUS_ENC_FRAME_SAMPLES_48K) {
            /* The end trim of close_chunk() covers at most one frame of lookahead. */
            ESP_LOGE(TAG, "encoder lookahead %u too long", s->pre_skip);
            err = ESP_ERR_NOT_SUPPORTED;
        }
    }
    if (err == ESP_OK && !chunk_store_ready()) {
        err = chunk_store_init(chunk_store_boot_id()); /* the card may have come in since boot */
    }
    if (err == ESP_OK) {
        err = chunk_store_media_open();
        media = err == ESP_OK;
    }
    if (err == ESP_OK) {
        err = run(s, &why);
    } else {
        ESP_LOGE(TAG, "cannot start: %s", esp_err_to_name(err));
    }
    if (media) {
        chunk_store_media_close();
    }
    const UBaseType_t stack_free = uxTaskGetStackHighWaterMark(NULL);
    session_rec_state_t st;
    STATE_UPDATE({
        s_state.stack_free_min = (uint32_t)stack_free;
        s_state.last_err = err;
        s_state.last_end = why;
        st = s_state;
    });
    static const char *const WHY[] = {"stopped", "card full", "error", "at its length limit"};
    ESP_LOGI(TAG,
             "%s %s %s after %" PRIu32 " s, %" PRIu32 " chunk(s) (%s); encode avg %" PRIu32
             " us, max %" PRIu32 " us per 20 ms frame; stack never used %" PRIu32 " bytes",
             st.kind == SESSION_KIND_NOTE ? "note" : "session", st.session_hex, WHY[why], st.elapsed_s, st.chunks_written, esp_err_to_name(err),
             st.enc_avg_us, st.enc_max_us, st.stack_free_min);
    if (s) {
        opus_enc_destroy(s->enc);
    }
    heap_caps_free(ogg);
    heap_caps_free(s);
    STATE_UPDATE(s_state.running = false);
    if (s_done) {
        s_done(err, why, s_ctx);
    }
    vTaskDelete(NULL);
}

esp_err_t session_rec_start(session_rec_kind_t kind, session_rec_done_cb_t done,
                            session_rec_tick_cb_t tick, void *ctx)
{
    bool busy = false;
    STATE_UPDATE({
        busy = s_state.running;
        if (!busy) {
            memset(&s_state, 0, sizeof(s_state));
            s_state.running = true;
            s_state.kind = kind;
        }
    });
    ESP_RETURN_ON_FALSE(!busy, ESP_ERR_INVALID_STATE, TAG, "a session is running");
    s_done = done;
    s_tick = tick;
    s_ctx = ctx;
    s_kind = kind;
    s_stop = false;
    if (xTaskCreate(session_task, "session", TASK_STACK_BYTES, NULL, TASK_PRIORITY, NULL) != pdPASS) {
        STATE_UPDATE(s_state.running = false);
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

bool session_rec_stop(void)
{
    if (!session_rec_running()) {
        return false;
    }
    s_stop = true;
    return true;
}

bool session_rec_running(void)
{
    bool running;
    STATE_UPDATE(running = s_state.running);
    return running;
}

void session_rec_get_state(session_rec_state_t *out)
{
    STATE_UPDATE(*out = s_state);
}
