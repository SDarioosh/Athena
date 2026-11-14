/* Pairing with Odysseus from the Status screen (docs/k230-pda-plan.md "Decisions",
 * Contract 1 LOCAL `pair` / `pair_result`), without LVGL. Host-tested (test/test_pairing.c).
 *
 *   CHOOSE --scan--> SCANNING --qr(odysseus-pair:…)--> CONFIRM --yes--> WAITING --pair_result--> OK | FAILED
 *   CHOOSE --type--> TYPING   --submit(url, code)----> CONFIRM --yes--> WAITING --no answer 45 s--> FAILED
 *                                                     CONFIRM --cancel--> back to SCANNING / TYPING
 *
 * Nothing is sent before the owner said yes to "Pair with <host>?" (p->host, the host of the
 * address in the code; the screen adds that this replaces the current pairing): a QR code
 * held up to the camera can't pair the PDA with some other server by itself.
 * Scanning: vision's `qr` mode is on (pair_wants_camera); a decoded QR text that starts
 * with "odysseus-pair:" goes to syncd as is, in `url` with an empty `code` (syncd parses
 * `odysseus-pair:v1?url=…&code=…` itself and words any error); any other QR code is
 * shown as not a pairing code and scanning goes on; the UI reads the address out of the
 * code only for the question, and checks it as typed input is checked. Typed: the hub's address and the code,
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
#define PAIR_HOST_MAX 128

typedef enum {
    PAIR_CHOOSE = 0, /* scan or type */
    PAIR_SCANNING,
    PAIR_TYPING,
    PAIR_CONFIRM, /* a code was read or typed: "Pair with <host>?", nothing sent yet */
    PAIR_WAITING, /* `pair` sent, waiting for pair_result */
    PAIR_OK,
    PAIR_FAILED,
} pair_stage_t;

typedef struct {
    pair_stage_t stage;
    char msg[PAIR_MSG_MAX]; /* the last word for the screen: an error, "Paired with …", a hint */
    bool hint;              /* SCANNING / TYPING: msg is a hint for the user, not a result */
    int64_t sent_ms;
    char host[PAIR_HOST_MAX];    /* CONFIRM: the hub's host, printable */
    char pending[PAIR_LOCAL_MAX]; /* CONFIRM: the LOCAL `pair` JSON to send on yes */
    size_t pending_len;
    pair_stage_t from; /* CONFIRM: SCANNING or TYPING, where Cancel goes back to */
} pair_flow_t;

void pair_reset(pair_flow_t *p); /* CHOOSE */
void pair_start_scan(pair_flow_t *p);
void pair_start_typing(pair_flow_t *p);
/* Back to CHOOSE from SCANNING / TYPING / OK / FAILED; from CONFIRM back to SCANNING or
 * TYPING (nothing sent); not from WAITING (the hub may already have taken the code). false
 * if it stays. */
bool pair_back(pair_flow_t *p);
bool pair_wants_camera(const pair_flow_t *p);

typedef enum {
    PAIR_IGNORED = 0, /* not scanning / not typing: nothing to do */
    PAIR_ASK,         /* CONFIRM now: ask "Pair with p->host?"; pair_confirm() on yes */
    PAIR_SEND,        /* out has the LOCAL `pair` JSON: send it, then pair_sent() */
    PAIR_REJECTED,    /* not sent: msg says why (not an Odysseus QR code, a typo) */
} pair_step_t;

/* A decoded QR code while scanning: PAIR_ASK, PAIR_REJECTED or PAIR_IGNORED (out stays
 * empty). */
pair_step_t pair_on_qr(pair_flow_t *p, const char *text, char *out, size_t cap, size_t *out_len);
/* The typed address and code: PAIR_ASK, PAIR_REJECTED or PAIR_IGNORED. */
pair_step_t pair_submit(pair_flow_t *p, const char *url, const char *code, char *out, size_t cap,
                        size_t *out_len);
/* The owner said yes in CONFIRM: PAIR_SEND with the pending JSON in out, else PAIR_IGNORED. */
pair_step_t pair_confirm(pair_flow_t *p, char *out, size_t cap, size_t *out_len);
/* The host of an http(s) address ("https://hub.ts.net:7000/x" -> "hub.ts.net", "[fd7a::1]"
 * as "fd7a::1"), non-printable bytes as '?'. false if there is none. */
bool pair_url_host(const char *url, char *host, size_t cap);
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
