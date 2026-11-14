#include "glance_server.h"

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
#include "glance_frame.h"
#include "local_msg.h"
#include "pda_proto.h"

#define LOG(...) fprintf(stderr, "athena-pda: glance: " __VA_ARGS__)
#define OUT_MAX (1024 * 1024) /* queued output beyond this: the client isn't reading */
#define NOTIFY_MIN_MS 1000

struct glance_server {
    char path[108];
    int lfd, cfd;
    glance_rx_t rx;
    pda_gate_t gate;
    pda_put_t put;
    uint8_t *out;
    size_t out_len, out_cap;
    bool drop; /* close the client after this round */
    uint8_t last_status[PDA_PROTO_STATUS_LEN];
    int64_t last_notify_ms;
    glance_stats_t st;
};

static void set_nonblock(int fd)
{
    const int fl = fcntl(fd, F_GETFL, 0);
    fcntl(fd, F_SETFL, fl | O_NONBLOCK);
    fcntl(fd, F_SETFD, FD_CLOEXEC);
}

glance_server_t *glance_server_open(const char *path)
{
    struct sockaddr_un sa = {.sun_family = AF_UNIX};
    if (strlen(path) >= sizeof(sa.sun_path)) {
        LOG("socket path too long: %s\n", path);
        return NULL;
    }
    glance_server_t *gs = calloc(1, sizeof(*gs));
    if (!gs) {
        return NULL;
    }
    snprintf(gs->path, sizeof(gs->path), "%s", path);
    snprintf(sa.sun_path, sizeof(sa.sun_path), "%s", path);
    gs->cfd = -1;
    gs->lfd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (gs->lfd < 0) {
        LOG("socket: %s\n", strerror(errno));
        free(gs);
        return NULL;
    }
    unlink(path);
    /* 0600 from the start: no window in which another user could connect */
    const mode_t old = umask(0177);
    const int rc = bind(gs->lfd, (struct sockaddr *)&sa, sizeof(sa));
    umask(old);
    if (rc != 0 || chmod(path, 0600) != 0 || listen(gs->lfd, 2) != 0) {
        LOG("listen on %s: %s\n", path, strerror(errno));
        close(gs->lfd);
        free(gs);
        return NULL;
    }
    set_nonblock(gs->lfd);
    return gs;
}

static void drop_client(glance_server_t *gs)
{
    if (gs->cfd < 0) {
        return;
    }
    close(gs->cfd);
    gs->cfd = -1;
    gs->drop = false;
    gs->out_len = 0;
    glance_rx_reset(&gs->rx);
    pda_put_abort(&gs->put); /* a disconnect mid-PUT discards it (§3.3) */
    gs->gate.busy = false;
    app_set_link(false);
    LOG("syncd disconnected\n");
}

void glance_server_close(glance_server_t *gs)
{
    if (!gs) {
        return;
    }
    drop_client(gs);
    close(gs->lfd);
    unlink(gs->path);
    free(gs->out);
    free(gs);
}

static int queue_frame(glance_server_t *gs, uint8_t type, const void *payload, size_t len)
{
    if (gs->cfd < 0 || len > GLANCE_PAYLOAD_MAX) {
        return -1;
    }
    if (gs->out_len + GLANCE_FRAME_MAX > gs->out_cap) {
        if (gs->out_cap >= OUT_MAX) {
            LOG("syncd isn't reading: dropped\n");
            gs->drop = true;
            return -1;
        }
        const size_t cap = gs->out_cap ? gs->out_cap * 2 : 16384;
        uint8_t *n = realloc(gs->out, cap);
        if (!n) {
            gs->drop = true;
            return -1;
        }
        gs->out = n;
        gs->out_cap = cap;
    }
    gs->out_len += glance_frame_encode(type, payload, len, gs->out + gs->out_len);
    gs->st.frames_out++;
    return 0;
}

static void flush_out(glance_server_t *gs)
{
    while (gs->cfd >= 0 && gs->out_len > 0) {
        const ssize_t w = send(gs->cfd, gs->out, gs->out_len, MSG_NOSIGNAL);
        if (w < 0) {
            if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
                gs->drop = true;
            }
            return;
        }
        memmove(gs->out, gs->out + w, gs->out_len - (size_t)w);
        gs->out_len -= (size_t)w;
    }
}

/* pda_proto's send: one DATA frame per response (a BLE notification) */
static int send_data(void *ctx, const uint8_t *frame, size_t len)
{
    return queue_frame(ctx, GLANCE_DATA, frame, len);
}

static void send_status(glance_server_t *gs)
{
    pda_status_t s;
    app_status(&s);
    pda_encode_status(&s, gs->last_status);
    queue_frame(gs, GLANCE_STATUS, gs->last_status, PDA_PROTO_STATUS_LEN);
    gs->last_notify_ms = app_mono_ms();
}

static void on_ctrl(glance_server_t *gs, const uint8_t *p, size_t len)
{
    pda_cmd_t cmd;
    uint8_t err[PDA_PROTO_ERROR_LEN];
    if (!pda_gate_submit(&gs->gate, p, len, GLANCE_MTU, &cmd, err)) {
        queue_frame(gs, GLANCE_DATA, err, sizeof(err));
        return;
    }
    uint8_t frame_buf[PDA_PROTO_FRAME_MAX];
    const pda_result_t r = pda_execute(&cmd, &gs->put, app_store(), NULL, send_data, gs, frame_buf);
    pda_gate_done(&gs->gate);
    if (r == PDA_LINK_LOST) {
        gs->drop = true;
    }
    /* PDA_BYE ends a sync, not the connection: syncd stays (Contract 1) */
}

static void on_frame(void *ctx, uint8_t type, const uint8_t *p, size_t len)
{
    glance_server_t *gs = ctx;
    gs->st.frames_in++;
    switch (type) {
    case GLANCE_CTRL:
        on_ctrl(gs, p, len);
        break;
    case GLANCE_STATUS_REQ:
        send_status(gs);
        break;
    case GLANCE_INFO_REQ: {
        char info[96];
        const int n = snprintf(info, sizeof(info), "proto=%d;device=k230-pda;fw=%s;board=t_display_k230",
                               PDA_PROTO_VERSION, APP_FW_VERSION);
        queue_frame(gs, GLANCE_INFO, info, (size_t)n);
        break;
    }
    case GLANCE_LOCAL: {
        local_in_t m;
        if (local_parse((const char *)p, len, &m)) {
            local_apply(&m);
        } else {
            gs->st.bad_frames++;
        }
        break;
    }
    default:
        gs->st.bad_frames++; /* DATA, STATUS, INFO come from us; anything else is unknown */
        break;
    }
}

static void accept_clients(glance_server_t *gs)
{
    for (;;) {
        const int fd = accept(gs->lfd, NULL, NULL);
        if (fd < 0) {
            return;
        }
        if (gs->cfd >= 0) {
            close(fd); /* one client at a time */
            gs->st.refused++;
            LOG("a second client refused\n");
            continue;
        }
        set_nonblock(fd);
        gs->cfd = fd;
        gs->st.clients++;
        glance_rx_reset(&gs->rx);
        gs->gate.busy = false;
        /* the baseline for "changed": syncd asks with STATUS_REQ first */
        pda_status_t s;
        app_status(&s);
        pda_encode_status(&s, gs->last_status);
        gs->last_notify_ms = app_mono_ms();
        LOG("syncd connected\n");
        app_set_link(true);
    }
}

static void read_client(glance_server_t *gs)
{
    uint8_t buf[4096];
    for (;;) {
        const ssize_t r = recv(gs->cfd, buf, sizeof(buf), 0);
        if (r == 0) {
            gs->drop = true;
            return;
        }
        if (r < 0) {
            if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
                gs->drop = true;
            }
            return;
        }
        if (glance_rx_feed(&gs->rx, buf, (size_t)r, on_frame, gs) != 0) {
            LOG("frame longer than %d bytes: dropped\n", GLANCE_PAYLOAD_MAX);
            gs->st.bad_frames++;
            gs->drop = true;
            return;
        }
        if (gs->drop || gs->cfd < 0) {
            return;
        }
    }
}

int glance_server_pollfds(const glance_server_t *gs, struct pollfd *pfd, int max)
{
    int n = 0;
    if (max < 1) {
        return 0;
    }
    pfd[n++] = (struct pollfd){.fd = gs->lfd, .events = POLLIN};
    if (gs->cfd >= 0 && n < max) {
        pfd[n++] = (struct pollfd){.fd = gs->cfd, .events = (short)(POLLIN | (gs->out_len ? POLLOUT : 0))};
    }
    return n;
}

void glance_server_process(glance_server_t *gs, const struct pollfd *pfd, int n)
{
    for (int i = 0; i < n; i++) {
        if (pfd[i].fd == gs->lfd && (pfd[i].revents & POLLIN)) {
            accept_clients(gs);
        } else if (pfd[i].fd == gs->cfd && gs->cfd >= 0) {
            if (pfd[i].revents & (POLLIN | POLLHUP | POLLERR)) {
                read_client(gs);
            }
        }
    }
    glance_server_tick(gs);
}

void glance_server_tick(glance_server_t *gs)
{
    if (gs->cfd >= 0 && !gs->drop) {
        pda_status_t s;
        uint8_t now[PDA_PROTO_STATUS_LEN];
        app_status(&s);
        pda_encode_status(&s, now);
        if (memcmp(now, gs->last_status, sizeof(now)) != 0 &&
            app_mono_ms() - gs->last_notify_ms >= NOTIFY_MIN_MS) {
            send_status(gs);
        }
    }
    flush_out(gs);
    if (gs->drop) {
        drop_client(gs);
    }
}

bool glance_server_connected(const glance_server_t *gs)
{
    return gs && gs->cfd >= 0;
}

int glance_server_send_local(glance_server_t *gs, const char *json, size_t len)
{
    if (!glance_server_connected(gs) || len > GLANCE_PAYLOAD_MAX) {
        return -1;
    }
    const int rc = queue_frame(gs, GLANCE_LOCAL, json, len);
    flush_out(gs);
    return rc;
}

void glance_server_stats(const glance_server_t *gs, glance_stats_t *out)
{
    *out = gs->st;
}
