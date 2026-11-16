/* A control socket for end-to-end tests (firmware/pda-k230/tests/e2e/), headless only.
 *
 * main.c opens it only when the backend is headless AND $ATHENA_PDA_TEST_CTL names a socket
 * path; the device and SDL backends never have it. It drives the UI the way a finger does
 * (taps go through LVGL's input, so the screens' own handlers run) and reads back what is
 * on screen; it never touches the feed, the outbox or the sockets directly.
 *
 * One client at a time; one command per line, one answer line each ("ok …" or "err …"):
 *
 *   tap X Y            a tap at (X, Y)
 *   click TEXT         a tap on the centre of the first visible label that is TEXT (else
 *                      the first that starts with it)
 *   has TEXT           "ok yes" / "ok no": a visible label contains TEXT
 *   field N            a tap on the N-th (from 0) visible text field: opens the keyboard
 *   type TEXT          TEXT into the keyboard's text field (as its keys would)
 *   clear              empty the keyboard's text field
 *   tab N              show tab N (0 Today .. 5 Status)
 *   clock EPOCH_S      a fixed clock (0: the real one), then a refresh
 *   state              "ok tab=N detail=0|1 sheet=0|1 off=0|1 outbox=N"
 *   png PATH           the screen as a PNG
 *   wait MS            run the UI loop MS ms before answering */
#pragma once

#include <poll.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct headless_ctl headless_ctl_t;

headless_ctl_t *headless_ctl_open(const char *path); /* NULL on error (logged) */
void headless_ctl_close(headless_ctl_t *c);
int headless_ctl_pollfds(headless_ctl_t *c, struct pollfd *out, int max);
void headless_ctl_process(headless_ctl_t *c, const struct pollfd *fds, int n);

#ifdef __cplusplus
}
#endif
