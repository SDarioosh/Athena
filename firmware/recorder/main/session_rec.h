/* Session recorder: mic -> Opus -> 30 s Ogg/Opus chunks in the queue on the SD card
 * (docs/twatch-ble-protocol.md §2). Replaces the milestone-1 WAV test recorder for real
 * recordings. One session at a time; it runs in its own task and keeps the card mounted
 * until it ends.
 *
 * A session ends on session_rec_stop() (BOOT+PWR, mute, console), when the card has less
 * than SESSION_REC_MIN_FREE_BYTES free, or on a mic/encoder/card error. Its last chunk
 * always has the LAST flag, and a session that never produced audio leaves no chunk. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define SESSION_REC_CHUNK_PACKETS 1500 /* 30 s of 20 ms packets */
#define SESSION_REC_MIN_FREE_BYTES (8ull * 1024 * 1024)

typedef enum {
    SESSION_END_REQUEST,   /* session_rec_stop() */
    SESSION_END_CARD_FULL, /* less than 8 MB free: the app buzzes 3 times */
    SESSION_END_ERROR,     /* mic, encoder or card failure; `result` says which */
} session_rec_end_t;

typedef struct {
    bool running;
    char session_hex[17];     /* current or last session, "" before the first */
    bool time_unset;          /* the session's chunks carry TIME_UNSET */
    uint32_t sequence;        /* sequence of the chunk being written (or the last one) */
    uint32_t elapsed_s;       /* audio encoded in this session */
    uint32_t chunks_written;  /* closed chunks of this session */
    esp_err_t last_err;       /* ESP_OK, or why the last session failed */
    session_rec_end_t last_end;
    /* Encoder load: microseconds per 20 ms frame (20000 = one full core). */
    uint32_t enc_frames;
    uint32_t enc_avg_us;
    uint32_t enc_max_us;
    uint32_t stack_free_min;  /* bytes of task stack never used */
} session_rec_state_t;

/* Called from the recorder task when the session has ended and its last chunk is closed. */
typedef void (*session_rec_done_cb_t)(esp_err_t result, session_rec_end_t why, void *ctx);
/* Called once per second of audio. */
typedef void (*session_rec_tick_cb_t)(uint32_t elapsed_s, void *ctx);

/* Start a session. Call chunk_store_init() first (at boot); the session retries it once if
 * the card wasn't there then. ESP_ERR_INVALID_STATE if a session is running. The mic must be
 * free (not used by the WAV test recorder or `level`). */
esp_err_t session_rec_start(session_rec_done_cb_t done, session_rec_tick_cb_t tick, void *ctx);
/* Ask the running session to end; returns false if none is running. `done` follows. */
bool session_rec_stop(void);
bool session_rec_running(void);
void session_rec_get_state(session_rec_state_t *out);

#ifdef __cplusplus
}
#endif
