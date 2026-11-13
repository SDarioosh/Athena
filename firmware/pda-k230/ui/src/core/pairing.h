/* Pairing with Odysseus from the Status screen (docs/k230-pda-plan.md "Decisions",
 * Contract 1 LOCAL `pair` / `pair_result`), without LVGL. Host-tested (test/test_pairing.c).
 *
 *   CHOOSE --scan--> SCANNING --qr(odysseus-pair:…)--> WAITING --pair_result--> OK | FAILED
 *   CHOOSE --type--> TYPING   --submit(url, code)----> WAITING --no answer 45 s--> FAILED
 *
 * Scanning: vision's `qr` mode is on (pair_wants_camera); a decoded QR text that starts
 * with "odysseus-pair:" goes to syncd as is, in `url` with an empty `code` (syncd parses
 * `odysseus-pair:v1?url=…&code=…` itself and words any error); any other QR code is
 * shown as not a pairing code and scanning goes on. Typed: the hub's address and the code,
 * checked here like syncd does (http(s) and a host; 8-100 of A-Z a-z 0-9 _ -) so a typo
 * is caught before anything is sent. A pair_result is only taken while WAITING. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PAIR_WAIT_MS 45000 /* syncd's redeem times out at 20 s */
#define PAIR_PREFIX "odysseus-pair:"
#define PAIR_MSG_MAX 200
#define PAIR_LOCAL_MAX 512 /* a LOCAL frame's payload (glance.sock len <= 512) */

typedef enum {
    PAIR_CHOOSE = 0, /* scan or type */
    PAIR_SCANNING,
    PAIR_TYPING,
    PAIR_WAITING, /* `pair` sent, waiting for pair_result */
    PAIR_OK,
    PAIR_FAILED,
} pair_stage_t;

typedef struct {
    pair_stage_t stage;
    char msg[PAIR_MSG_MAX]; /* the last word for the screen: an error, "Paired with …", a hint */
    bool hint;              /* SCANNING / TYPING: msg is a hint for the user, not a result */
    int64_t sent_ms;
} pair_flow_t;

void pair_reset(pair_flow_t *p); /* CHOOSE */
void pair_start_scan(pair_flow_t *p);
void pair_start_typing(pair_flow_t *p);
/* Back to CHOOSE from SCANNING / TYPING / OK / FAILED (not WAITING: the hub may already
 * have taken the code). false if it stays. */
bool pair_back(pair_flow_t *p);
bool pair_wants_camera(const pair_flow_t *p);

typedef enum {
    PAIR_IGNORED = 0, /* not scanning / not typing: nothing to do */
    PAIR_SEND,        /* out has the LOCAL `pair` JSON: send it, then pair_sent() */
    PAIR_REJECTED,    /* not sent: msg says why (not an Odysseus QR code, a typo) */
} pair_step_t;

/* A decoded QR code while scanning. */
pair_step_t pair_on_qr(pair_flow_t *p, const char *text, char *out, size_t cap, size_t *out_len);
/* The typed address and code. */
pair_step_t pair_submit(pair_flow_t *p, const char *url, const char *code, char *out, size_t cap,
                        size_t *out_len);
/* After PAIR_SEND: sent (WAITING from now) or the send failed (FAILED: syncd isn't there). */
void pair_sent(pair_flow_t *p, bool ok, int64_t now_ms);

/* syncd's pair_result: true if taken (WAITING -> OK / FAILED). */
bool pair_on_result(pair_flow_t *p, bool ok, const char *msg);
/* Time passing: WAITING too long -> FAILED. true if the stage changed. */
bool pair_tick(pair_flow_t *p, int64_t now_ms);

/* The checks of syncd's pairing.check_url / check_code, for typed input: NULL if fine,
 * else one short line. */
const char *pair_check_url(const char *url);
const char *pair_check_code(const char *code);

#ifdef __cplusplus
}
#endif
