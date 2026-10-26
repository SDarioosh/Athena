/* Event log: what started and ended recordings, kept in flash (NVS) so it survives a reset or
 * a power cut. The console log only exists while USB is plugged in, and `status` loses the
 * uptime and reset reason at the next reset, so neither could explain a recording that
 * stopped and started again on its own during a phone call (2026-10-23).
 *
 * One entry per boot, button event (with the PMU interrupt bits behind a PWR press, so a
 * real key press can be told from a phantom one), recording start, stop request, recording
 * end, mute, low battery and card power cycle.
 *
 * This file is the pure part (entries, the ring, names, the text form; host-tested in
 * test/test_event_log.c). event_log_store.c keeps the ring in NVS. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define EVENT_LOG_BLOCK 8                     /* entries per NVS blob */
#define EVENT_LOG_BLOCKS 16
#define EVENT_LOG_CAP (EVENT_LOG_BLOCK * EVENT_LOG_BLOCKS) /* 128: several days of use */
#define EVENT_LOG_LINE 128

typedef enum {
    EL_NONE = 0,       /* an empty slot */
    EL_BOOT = 1,       /* why: el_reset_t; arg: 1 = woken from standby, 2 = recording resumed */
    EL_BUTTON = 2,     /* why: el_button_t; arg: PMU interrupt bits (PWR), or ms since the
                        * previous short press (EL_BTN_PWR_SHORT_IGNORED) */
    EL_START = 3,      /* why: el_trigger_t; aux: el_rec_t */
    EL_STOP = 4,       /* a stop was asked for. why: el_trigger_t; aux: el_rec_t */
    EL_END = 5,        /* a recording ended. why: el_end_t; arg: seconds; aux: el_rec_t */
    EL_MUTE = 6,       /* why: 1 muted, 0 unmuted */
    EL_BATT_LOW = 7,   /* why: 1 low (recordings refused), 0 OK again; arg: mV */
    EL_CARD_RESET = 8, /* card powered off and on after a write error. why: 1 mounted again,
                        * 0 not; arg: the write error (esp_err_t) */
    EL_KIND_COUNT_,
} el_kind_t;

/* The reset reason, as esp_reset_reason() gives it (mapped in event_log_store.c). */
typedef enum {
    EL_RST_UNKNOWN,
    EL_RST_POWERON, /* power came on, or a reset over USB/RST */
    EL_RST_EXT,
    EL_RST_SW,
    EL_RST_PANIC,
    EL_RST_INT_WDT,
    EL_RST_TASK_WDT,
    EL_RST_WDT,
    EL_RST_DEEPSLEEP,
    EL_RST_BROWNOUT,
    EL_RST_USB,
    EL_RST_JTAG,
    EL_RST_OTHER,
    EL_RST_COUNT_,
} el_reset_t;

typedef enum {
    EL_BTN_PWR_SHORT = 1,
    EL_BTN_PWR_SHORT_IGNORED, /* within the guard time of the previous short press */
    EL_BTN_PWR_LONG,
    EL_BTN_BOOT_SHORT,
    EL_BTN_BOOT_DOUBLE,
    EL_BTN_BOOT_LONG,
    EL_BTN_COUNT_,
} el_button_t;

/* What asked for a start or a stop. */
typedef enum {
    EL_TRIG_PWR = 1,      /* PWR short press */
    EL_TRIG_PWR_LONG,     /* PWR held: a voice note */
    EL_TRIG_CONSOLE,      /* USB console */
    EL_TRIG_RESUME,       /* after a crash or watchdog restart */
    EL_TRIG_AFTER_NOTE,   /* a conversation that a note interrupted goes on */
    EL_TRIG_NOTE,         /* a conversation ended so that a note can start */
    EL_TRIG_MUTE,
    EL_TRIG_BATT_LOW,
    EL_TRIG_COUNT_,
} el_trigger_t;

typedef enum {
    EL_REC_CONVERSATION = 0,
    EL_REC_NOTE = 1,
} el_rec_t;

/* session_rec_end_t, kept here as numbers so the log stays readable if that enum changes. */
typedef enum {
    EL_END_STOPPED = 0,
    EL_END_CARD_FULL = 1,
    EL_END_ERROR = 2,
    EL_END_MAX = 3,
    EL_END_COUNT_,
} el_end_t;

typedef struct {
    uint32_t t;    /* UTC epoch seconds; 0 = clock unknown */
    uint32_t up_s; /* seconds since this boot */
    int32_t arg;
    uint8_t kind;  /* el_kind_t; EL_NONE = empty */
    uint8_t why;
    int8_t batt;   /* state of charge in %, -1 = not read */
    uint8_t aux;
} el_entry_t;

/* The ring, as kept in RAM: `next` counts every entry ever added (it wraps at 2^16, a
 * multiple of EVENT_LOG_CAP), so entry n is in slot n % EVENT_LOG_CAP. */
typedef struct {
    el_entry_t e[EVENT_LOG_CAP];
    uint16_t next;
} el_ring_t;

void el_ring_init(el_ring_t *r, uint16_t next);
/* Store `e` in the next slot; returns its block (the NVS blob to write back). A new block
 * is cleared first, so its slots after the new entry read as empty. */
unsigned el_ring_add(el_ring_t *r, const el_entry_t *e);
/* The i-th entry, oldest first, for i in [0, EVENT_LOG_CAP); NULL for an empty slot. */
const el_entry_t *el_ring_get(const el_ring_t *r, unsigned i);

const char *el_reset_name(int why);
const char *el_button_name(int why);
const char *el_trigger_name(int why);
const char *el_end_name(int why);

/* One line, UTC: "2026-10-23T13:40:28Z +62000s  button PWR short (PMU 0x000b10: key edges
 * + -); batt 73%". "no key edges" means the PMU reported a press without the key moving. */
void el_format(const el_entry_t *e, char *out, size_t cap);

#ifdef __cplusplus
}
#endif
