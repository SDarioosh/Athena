/* The UI end of vision.sock (docs/k230-pda-plan.md Contract 2): a Unix stream socket, mode
 * 0600, one client (athena-vision); a second is closed at once. Lines of JSON, at most 1024
 * bytes (a longer line is skipped up to its "\n"). `mode` goes out on connect and whenever
 * app_vision_mode() changes; `state` and `presence` update app_vision(); every parsed
 * event also goes to the callback (the UI's hands-free input). */
#pragma once

#include <poll.h>
#include <stdbool.h>

#include "vision_msg.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct vision_server vision_server_t;
typedef void (*vision_event_cb)(void *ctx, const vision_event_t *ev);

vision_server_t *vision_server_open(const char *path, vision_event_cb cb, void *ctx);
void vision_server_close(vision_server_t *vs);

int vision_server_pollfds(const vision_server_t *vs, struct pollfd *pfd, int max);
void vision_server_process(vision_server_t *vs, const struct pollfd *pfd, int n);
/* Send `mode` if app_vision_mode() differs from what vision was last told. */
void vision_server_sync_mode(vision_server_t *vs);
bool vision_server_connected(const vision_server_t *vs);

typedef struct {
    unsigned clients, refused, lines, bad_lines, long_lines, modes_sent;
} vision_stats_t;
void vision_server_stats(const vision_server_t *vs, vision_stats_t *out);

#ifdef __cplusplus
}
#endif
