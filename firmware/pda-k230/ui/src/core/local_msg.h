/* LOCAL messages on glance.sock (docs/k230-pda-plan.md Contract 1, frame type 0x10): one
 * UTF-8 JSON object, `"t"` names the type; unknown types and keys are ignored. Pure C;
 * host-tested (test/test_glance.c). */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "app_state.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    LOCAL_UNKNOWN = 0,
    LOCAL_NET,
    LOCAL_PAIR_RESULT,
    LOCAL_TAILNET,
} local_type_t;

typedef struct {
    local_type_t type;
    app_net_t net;         /* LOCAL_NET */
    app_tailnet_t tailnet; /* LOCAL_TAILNET */
    bool ok;               /* LOCAL_PAIR_RESULT */
    char msg[128];         /* LOCAL_PAIR_RESULT */
} local_in_t;

/* syncd -> UI. false: not a JSON object with a string "t" (ignored). An unknown "t" is
 * true with LOCAL_UNKNOWN. */
bool local_parse(const char *json, size_t len, local_in_t *out);

/* Apply a parsed message to the app state (app_set_net / _tailnet / _pair_result). */
void local_apply(const local_in_t *m);

/* UI -> syncd; each returns the length (<= cap - 1), 0 if it doesn't fit. */
size_t local_sync_now(char *out, size_t cap);
size_t local_pair(char *out, size_t cap, const char *url, const char *code);
size_t local_unpair(char *out, size_t cap);

#ifdef __cplusplus
}
#endif
