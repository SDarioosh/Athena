/* The UI end of glance.sock (docs/k230-pda-plan.md Contract 1): a Unix stream socket,
 * mode 0600, one client (syncd) at a time; a second is accepted and closed at once. CTRL
 * frames run through the T5's pda_proto.c (the gate at MTU 517, then pda_execute against
 * app_store()); STATUS_REQ, INFO_REQ and LOCAL are answered here, and STATUS is sent
 * unasked when it changes (at most once a second). Non-blocking: the UI loop polls the
 * fds and calls glance_server_process(). */
#pragma once

#include <poll.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct glance_server glance_server_t;

/* Listen on path (a stale socket file is replaced). NULL on error (logged). */
glance_server_t *glance_server_open(const char *path);
void glance_server_close(glance_server_t *gs);

/* The fds to poll: the listener and, if connected, the client (POLLOUT while output is
 * queued). Returns how many were written to pfd (<= max, max >= 2). */
int glance_server_pollfds(const glance_server_t *gs, struct pollfd *pfd, int max);
/* Handle what poll() reported for those fds, then glance_server_tick(). */
void glance_server_process(glance_server_t *gs, const struct pollfd *pfd, int n);
/* The status notify; call at least every 250 ms (process() calls it). */
void glance_server_tick(glance_server_t *gs);

bool glance_server_connected(const glance_server_t *gs);
/* Send a LOCAL JSON object to syncd: 0, or -1 not connected / too long. */
int glance_server_send_local(glance_server_t *gs, const char *json, size_t len);

/* Statistics for the Status screen and the tests. */
typedef struct {
    unsigned clients, refused, frames_in, frames_out, bad_frames;
} glance_stats_t;
void glance_server_stats(const glance_server_t *gs, glance_stats_t *out);

#ifdef __cplusplus
}
#endif
