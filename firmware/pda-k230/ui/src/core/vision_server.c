#include "vision_server.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include "app_state.h"

#define LOG(...) fprintf(stderr, "athena-pda: vision: " __VA_ARGS__)

struct vision_server {
    char path[108];
    int lfd, cfd;
    char line[VISION_LINE_MAX + 1];
    size_t have;
    bool skipping; /* in a line longer than 1024 bytes: drop up to its "\n" */
    int sent_mode; /* bit0 gestures, bit1 presence, bit2 qr; -1 none sent */
    vision_event_cb cb;
    void *ctx;
    vision_stats_t st;
};

static void set_nonblock(int fd)
{
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
    fcntl(fd, F_SETFD, FD_CLOEXEC);
}

vision_server_t *vision_server_open(const char *path, vision_event_cb cb, void *ctx)
{
    struct sockaddr_un sa = {.sun_family = AF_UNIX};
    if (strlen(path) >= sizeof(sa.sun_path)) {
        LOG("socket path too long: %s\n", path);
        return NULL;
    }
    vision_server_t *vs = calloc(1, sizeof(*vs));
    if (!vs) {
        return NULL;
    }
    snprintf(vs->path, sizeof(vs->path), "%s", path);
    snprintf(sa.sun_path, sizeof(sa.sun_path), "%s", path);
    vs->cfd = -1;
    vs->sent_mode = -1;
    vs->cb = cb;
    vs->ctx = ctx;
    vs->lfd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (vs->lfd < 0) {
        free(vs);
        return NULL;
    }
    unlink(path);
    const mode_t old = umask(0177);
    const int rc = bind(vs->lfd, (struct sockaddr *)&sa, sizeof(sa));
    umask(old);
    if (rc != 0 || chmod(path, 0600) != 0 || listen(vs->lfd, 2) != 0) {
        LOG("listen on %s: %s\n", path, strerror(errno));
        close(vs->lfd);
        free(vs);
        return NULL;
    }
    set_nonblock(vs->lfd);
    return vs;
}

static void drop_client(vision_server_t *vs)
{
    if (vs->cfd < 0) {
        return;
    }
    close(vs->cfd);
    vs->cfd = -1;
    vs->have = 0;
    vs->skipping = false;
    vs->sent_mode = -1;
    app_vision_t v = *app_vision();
    v.connected = false;
    v.camera = APP_CAMERA_OFF; /* the process is gone: so is its camera */
    v.fps = 0;
    v.present = false;
    app_set_vision(&v);
    /* a hand that was on screen is gone too */
    vision_event_t gone = {.type = VIS_HAND, .shape = VIS_SHAPE_NONE};
    if (vs->cb) {
        vs->cb(vs->ctx, &gone);
    }
    LOG("athena-vision disconnected\n");
}

void vision_server_close(vision_server_t *vs)
{
    if (!vs) {
        return;
    }
    drop_client(vs);
    close(vs->lfd);
    unlink(vs->path);
    free(vs);
}

static void send_mode(vision_server_t *vs)
{
    bool g, p, q;
    app_vision_mode(&g, &p, &q);
    const int m = (g ? 1 : 0) | (p ? 2 : 0) | (q ? 4 : 0);
    if (vs->cfd < 0 || m == vs->sent_mode) {
        return;
    }
    char line[96];
    const size_t n = vision_mode_line(line, sizeof(line), g, p, q);
    /* a few dozen bytes on a fresh socket: a short write here means the client is gone */
    if (send(vs->cfd, line, n, MSG_NOSIGNAL) == (ssize_t)n) {
        vs->sent_mode = m;
        vs->st.modes_sent++;
    } else {
        drop_client(vs);
    }
}

void vision_server_sync_mode(vision_server_t *vs)
{
    if (vs) {
        send_mode(vs);
    }
}

static void on_line(vision_server_t *vs, const char *line, size_t len)
{
    vs->st.lines++;
    vision_event_t *ev = malloc(sizeof(*ev));
    if (!ev) {
        return;
    }
    if (!vision_parse_line(line, len, ev)) {
        vs->st.bad_lines++;
        free(ev);
        return;
    }
    if (ev->type == VIS_STATE || ev->type == VIS_PRESENCE) {
        app_vision_t v = *app_vision();
        v.connected = true;
        if (ev->type == VIS_STATE) {
            v.camera = ev->camera == VIS_CAM_ON ? APP_CAMERA_ON
                       : ev->camera == VIS_CAM_ERROR ? APP_CAMERA_ERROR
                                                     : APP_CAMERA_OFF;
            v.fps = ev->fps;
            snprintf(v.msg, sizeof(v.msg), "%s", ev->msg);
        } else {
            v.present = ev->present;
        }
        app_set_vision(&v);
    }
    if (vs->cb && ev->type != VIS_UNKNOWN) {
        vs->cb(vs->ctx, ev);
    }
    free(ev);
}

static void read_client(vision_server_t *vs)
{
    char buf[2048];
    for (;;) {
        const ssize_t r = recv(vs->cfd, buf, sizeof(buf), 0);
        if (r == 0 || (r < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
            drop_client(vs);
            return;
        }
        if (r < 0) {
            return;
        }
        for (ssize_t i = 0; i < r; i++) {
            const char c = buf[i];
            if (c == '\n') {
                if (!vs->skipping && vs->have > 0) {
                    vs->line[vs->have] = 0;
                    on_line(vs, vs->line, vs->have);
                }
                vs->have = 0;
                vs->skipping = false;
                if (vs->cfd < 0) {
                    return;
                }
            } else if (!vs->skipping) {
                if (vs->have == VISION_LINE_MAX) {
                    vs->skipping = true;
                    vs->st.long_lines++;
                    vs->have = 0;
                } else {
                    vs->line[vs->have++] = c;
                }
            }
        }
    }
}

int vision_server_pollfds(const vision_server_t *vs, struct pollfd *pfd, int max)
{
    int n = 0;
    if (max < 1) {
        return 0;
    }
    pfd[n++] = (struct pollfd){.fd = vs->lfd, .events = POLLIN};
    if (vs->cfd >= 0 && n < max) {
        pfd[n++] = (struct pollfd){.fd = vs->cfd, .events = POLLIN};
    }
    return n;
}

void vision_server_process(vision_server_t *vs, const struct pollfd *pfd, int n)
{
    for (int i = 0; i < n; i++) {
        if (pfd[i].fd == vs->lfd && (pfd[i].revents & POLLIN)) {
            for (;;) {
                const int fd = accept(vs->lfd, NULL, NULL);
                if (fd < 0) {
                    break;
                }
                if (vs->cfd >= 0) {
                    close(fd);
                    vs->st.refused++;
                    continue;
                }
                set_nonblock(fd);
                vs->cfd = fd;
                vs->st.clients++;
                vs->sent_mode = -1;
                app_vision_t v = *app_vision();
                v.connected = true;
                app_set_vision(&v);
                LOG("athena-vision connected\n");
                send_mode(vs);
            }
        } else if (pfd[i].fd == vs->cfd && vs->cfd >= 0 && (pfd[i].revents & (POLLIN | POLLHUP | POLLERR))) {
            read_client(vs);
        }
    }
    send_mode(vs);
}

bool vision_server_connected(const vision_server_t *vs)
{
    return vs && vs->cfd >= 0;
}

void vision_server_stats(const vision_server_t *vs, vision_stats_t *out)
{
    *out = vs->st;
}
