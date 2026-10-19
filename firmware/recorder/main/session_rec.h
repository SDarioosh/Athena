/* Session recorder: mic -> Opus -> 30 s Ogg/Opus chunks in the queue on the SD card
 * (docs/twatch-ble-protocol.md §2). Replaces the milestone-1 WAV test recorder for real
 * recordings. One session at a time; it runs in its own task and keeps the card mounted
 * until it ends.
 *
 * A session is a conversation recording or a voice note. A note is its own short session:
 * every chunk carries CHUNK_FLAG_NOTE and it ends by itself after SESSION_REC_NOTE_MAX_S of
 * audio. A session ends on session_rec_stop() (PWR, mute, console), when the card has less
 * than SESSION_REC_MIN_FREE_BYTES free, at the note limit, or on a mic/encoder/card error.
 * Its last chunk always has the LAST flag, and a session that never produced audio leaves
 * no chunk. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define SESSION_REC_CHUNK_PACKETS 1500 /* 30 s of 20 ms packets */
#define SESSION_REC_MIN_FREE_BYTES (8ull * 1024 * 1024)
#define SESSION_REC_NOTE_MAX_S 300 /* a voice note stops itself after 5 min (10 chunks) */

typedef enum {
    SESSION_KIND_CONVERSATION, /* an ordinary recording session */
    SESSION_KIND_NOTE,         /* a voice note: NOTE flag on every chunk, max 5 min */
} session_rec_kind_t;

typedef enum {
    SESSION_END_REQUEST,   /* session_rec_stop() */
    SESSION_END_CARD_FULL, /* less than 8 MB free: the app buzzes 3 times */
    SESSION_END_ERROR,     /* mic, encoder or card failure; `result` says which */
    SESSION_END_MAX,       /* a note reached SESSION_REC_NOTE_MAX_S */
} session_rec_end_t;

typedef struct {
    bool running;
    session_rec_kind_t kind;  /* of the current or last session */
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
esp_err_t session_rec_start(session_rec_kind_t kind, session_rec_done_cb_t done,
                            session_rec_tick_cb_t tick, void *ctx);
/* Ask the running session to end; returns false if none is running. `done` follows. */
bool session_rec_stop(void);
bool session_rec_running(void);
void session_rec_get_state(session_rec_state_t *out);

#ifdef __cplusplus
}
#endif
