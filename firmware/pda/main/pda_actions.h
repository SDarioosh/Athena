/* Outbox actions (docs/t5-pda-plan.md §4.2): build the JSON for a tap, and read a queued
 * action back to show it as pending. Pure C; host-tested (test/test_outbox.c).
 *
 * Every builder writes at most PDA_PROTO_OB_JSON_MAX (400) bytes plus a NUL; free text
 * (titles) is cut at a UTF-8 boundary to fit. Builders return the length, 0 if the
 * fixed fields alone don't fit. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "pda_proto.h"

#ifdef __cplusplus
extern "C" {
#endif

#define PDA_ACTION_BUF (PDA_PROTO_OB_JSON_MAX + 1)
#define PDA_REF_MAX 96

typedef enum {
    PDA_ACT_UNKNOWN,
    PDA_ACT_ITEM_STATUS,
    PDA_ACT_CAL_ADD,
    PDA_ACT_CAL_DELETE,
    PDA_ACT_FETCH,
    PDA_ACT_DIGEST_REFRESH,
} pda_action_type_t;

/* {"t":"item_status","id":42,"status":"done"}; status: done, dismissed, open. */
size_t pda_action_item_status(char *out, int64_t id, const char *status);
/* {"t":"cal_add","title":"…","start":"2026-10-21T09:00:00+02:00","end":"…"} or, for an
 * all-day event, "start":"2026-10-23","all_day":true. from_item < 0: none. */
size_t pda_action_cal_add(char *out, const char *title, const char *start, const char *end,
                          bool all_day, int64_t from_item);
size_t pda_action_cal_delete(char *out, const char *id);
/* ref: conv:<id>, transcript:<id> or transcript:<id>@<from>-<to> */
size_t pda_action_fetch(char *out, const char *ref);
size_t pda_action_digest_refresh(char *out, const char *date);

typedef struct {
    pda_action_type_t type;
    int64_t item_id;                 /* item_status, cal_add from_item (-1 none) */
    char status[12];                 /* item_status */
    char ref[PDA_REF_MAX];           /* fetch: ref; cal_delete: event id; digest: date */
    char title[96];                  /* cal_add */
    char start[32], end[32];         /* cal_add */
    bool all_day;
} pda_action_info_t;

/* Read an action's JSON back. false if it isn't a JSON object with a known "t". */
bool pda_action_parse(const char *json, size_t len, pda_action_info_t *out);

#ifdef __cplusplus
}
#endif
