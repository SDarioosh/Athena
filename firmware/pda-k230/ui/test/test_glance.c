/* glance.sock end to end: a real Unix socket, a test client sending the T5 protocol's byte
 * frames (TIME, PUT of the demo feeds, OB_LIST / OB_ACK after a tap, BYE), STATUS_REQ and
 * the unasked STATUS, INFO, LOCAL both ways, one client at a time, bad frames. Also the
 * framing and the LOCAL parser on their own. */
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include "app_state.h"
#include "check.h"
#include "crc32.h"
#include "event_form.h"
#include "glance_frame.h"
#include "glance_server.h"
#include "local_msg.h"
#include "pda_proto.h"
#include "test_util.h"

static glance_server_t *gs;

static void pump(int rounds)
{
    for (int i = 0; i < rounds; i++) {
        struct pollfd pfd[2];
        const int n = glance_server_pollfds(gs, pfd, 2);
        poll(pfd, (nfds_t)n, 2);
        glance_server_process(gs, pfd, n);
    }
}

static int connect_to(const char *path)
{
    const int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    struct sockaddr_un sa = {.sun_family = AF_UNIX};
    snprintf(sa.sun_path, sizeof(sa.sun_path), "%s", path);
    if (connect(fd, (struct sockaddr *)&sa, sizeof(sa)) != 0) {
        perror("connect");
        exit(2);
    }
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
    return fd;
}

static void send_frame(int fd, uint8_t type, const void *p, size_t len)
{
    uint8_t b[GLANCE_FRAME_MAX];
    const size_t n = glance_frame_encode(type, p, len, b);
    CHECK(n == len + 3);
    CHECK(write(fd, b, n) == (ssize_t)n);
    pump(3);
}

/* ---- the client's receive side ---- */
#define RX_MAX 64
static struct {
    uint8_t type;
    uint8_t p[GLANCE_PAYLOAD_MAX];
    size_t len;
} rx[RX_MAX];
static int n_rx;
static glance_rx_t crx;

static void on_rx(void *ctx, uint8_t type, const uint8_t *p, size_t len)
{
    (void)ctx;
    if (n_rx < RX_MAX) {
        rx[n_rx].type = type;
        memcpy(rx[n_rx].p, p, len);
        rx[n_rx].len = len;
        n_rx++;
    }
}

/* everything the server has sent so far; returns how many frames */
static int recv_all(int fd)
{
    n_rx = 0;
    pump(3);
    uint8_t b[8192];
    for (;;) {
        const ssize_t r = read(fd, b, sizeof(b));
        if (r <= 0) {
            break;
        }
        CHECK_EQ(glance_rx_feed(&crx, b, (size_t)r, on_rx, NULL), 0);
    }
    return n_rx;
}

static void le16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}
static void le32(uint8_t *p, uint32_t v)
{
    for (int i = 0; i < 4; i++) {
        p[i] = (uint8_t)(v >> (8 * i));
    }
}
static uint32_t rd32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static void ctrl(int fd, const uint8_t *b, size_t n)
{
    send_frame(fd, GLANCE_CTRL, b, n);
}

/* PUT a blob the way syncd does: PUT_BEGIN, PUT_DATA of 507 bytes, PUT_END. Returns the
 * last response frame's opcode (PUT_OK or ERROR). */
static uint8_t put_blob(int fd, uint8_t kind, uint32_t id, const uint8_t *data, size_t len, uint32_t crc)
{
    uint8_t b[GLANCE_PAYLOAD_MAX];
    b[0] = PDA_OP_PUT_BEGIN;
    b[1] = kind;
    b[2] = b[3] = 0;
    le32(b + 4, id);
    le32(b + 8, (uint32_t)len);
    le32(b + 12, crc);
    ctrl(fd, b, 16);
    CHECK_EQ(recv_all(fd), 1);
    CHECK_EQ(rx[0].type, GLANCE_DATA);
    CHECK_EQ(rx[0].p[0], PDA_OP_PUT_READY);
    const size_t step = pda_put_payload_max(GLANCE_MTU);
    CHECK_EQ(step, 507);
    for (size_t off = 0; off < len; off += step) {
        const size_t n = len - off < step ? len - off : step;
        b[0] = PDA_OP_PUT_DATA;
        le32(b + 1, (uint32_t)off);
        memcpy(b + 5, data + off, n);
        ctrl(fd, b, 5 + n);
    }
    CHECK_EQ(recv_all(fd), 0); /* PUT_DATA answers nothing on success */
    b[0] = PDA_OP_PUT_END;
    ctrl(fd, b, 1);
    CHECK_EQ(recv_all(fd), 1);
    CHECK_EQ(rx[0].type, GLANCE_DATA);
    return rx[0].p[0];
}

static void test_session(const char *sock)
{
    int fd = connect_to(sock);
    pump(3);
    CHECK(glance_server_connected(gs));
    CHECK(app_link_up());

    /* INFO */
    send_frame(fd, GLANCE_INFO_REQ, NULL, 0);
    CHECK_EQ(recv_all(fd), 1);
    CHECK_EQ(rx[0].type, GLANCE_INFO);
    rx[0].p[rx[0].len] = 0;
    CHECK(strcmp((char *)rx[0].p, "proto=1;device=k230-pda;fw=" APP_FW_VERSION ";board=t_display_k230") == 0);

    /* STATUS_REQ before anything */
    send_frame(fd, GLANCE_STATUS_REQ, NULL, 0);
    CHECK_EQ(recv_all(fd), 1);
    CHECK_EQ(rx[0].type, GLANCE_STATUS);
    CHECK_EQ(rx[0].len, 16);
    CHECK_EQ(rx[0].p[0], 1);
    CHECK_EQ(rd32(rx[0].p + 8), 0); /* no feed */

    /* TIME: TIME_OK, the display offset */
    uint8_t t[14] = {PDA_OP_TIME, 0, 0, 0};
    const uint64_t ms = (uint64_t)DEMO_NOW * 1000;
    for (int i = 0; i < 8; i++) {
        t[4 + i] = (uint8_t)(ms >> (8 * i));
    }
    le16(t + 12, DEMO_TZ);
    ctrl(fd, t, 14);
    CHECK_EQ(recv_all(fd), 1);
    CHECK_EQ(rx[0].p[0], PDA_OP_TIME_OK);
    CHECK_EQ(app_tz_min(), DEMO_TZ);
    /* a TIME out of range: ERROR bad_time */
    uint8_t bad[14] = {PDA_OP_TIME};
    ctrl(fd, bad, 14);
    CHECK_EQ(recv_all(fd), 1);
    CHECK_EQ(rx[0].p[0], PDA_OP_ERROR);
    CHECK_EQ(rx[0].p[2], PDA_E_BAD_TIME);

    /* PUT the insight feed */
    size_t len;
    char *json = read_fixture("feed_insight_demo.json", &len);
    CHECK_EQ(put_blob(fd, PDA_KIND_FEED, 1792479600u, (uint8_t *)json, len, crc32_update(0, json, len)),
             PDA_OP_PUT_OK);
    CHECK(app_feed() != NULL && app_feed()->has_insight);
    /* a bad CRC: ERROR bad_crc, the feed stays */
    const feed_t *before = app_feed();
    CHECK_EQ(put_blob(fd, PDA_KIND_FEED, 5, (uint8_t *)json, len, 0x12345678u), PDA_OP_ERROR);
    CHECK_EQ(rx[0].p[2], PDA_E_BAD_CRC);
    CHECK(app_feed() == before);
    free(json);

    /* the unasked STATUS: feed_version changed (at most once a second) */
    app_set_mono_override(app_mono_ms() + 1500);
    CHECK_EQ(recv_all(fd), 1);
    CHECK_EQ(rx[0].type, GLANCE_STATUS);
    CHECK_EQ(rd32(rx[0].p + 8), 1792479600u);
    CHECK_EQ(rx[0].p[4] | rx[0].p[5] << 8, 0);
    CHECK_EQ(recv_all(fd), 0); /* nothing changed: nothing more */

    /* a tap on Today queues a todo_toggle: STATUS says one waits */
    const int work = note_index(app_feed(), "note_demo_work");
    CHECK_EQ(app_todo_tap(work, 0), APP_TODO_QUEUED);
    app_set_mono_override(app_mono_ms() + 1100);
    CHECK_EQ(recv_all(fd), 1);
    CHECK_EQ(rx[0].type, GLANCE_STATUS);
    CHECK_EQ(rx[0].p[4] | rx[0].p[5] << 8, 1);

    /* OB_LIST: the entry, then OB_END */
    uint8_t ol[7] = {PDA_OP_OB_LIST};
    le32(ol + 1, 0);
    le16(ol + 5, 16);
    ctrl(fd, ol, 7);
    CHECK_EQ(recv_all(fd), 2);
    CHECK_EQ(rx[0].p[0], PDA_OP_OB_ENTRY);
    const uint16_t jl = (uint16_t)(rx[0].p[2] | rx[0].p[3] << 8);
    const uint32_t id = rd32(rx[0].p + 4);
    char entry[512];
    memcpy(entry, rx[0].p + 16, jl);
    entry[jl] = 0;
    CHECK(strcmp(entry, "{\"t\":\"todo_toggle\",\"note\":\"note_demo_work\",\"i\":0,"
                        "\"text\":\"Send the Q4 budget sheet to Alice\",\"done\":true}") == 0);
    CHECK_EQ(rx[1].p[0], PDA_OP_OB_END);
    CHECK_EQ(rx[1].p[2] | rx[1].p[3] << 8, 1);
    CHECK_EQ(rd32(rx[1].p + 4), 0);
    /* listed now: a second tap can't take it back, it queues the reverse */
    CHECK(app_pending_todo("note_demo_work", 0)->listed);

    /* OB_ACK done: OB_ACKED result 0, the outbox empties */
    uint8_t ack[6] = {PDA_OP_OB_ACK};
    le32(ack + 1, id);
    ack[5] = 0;
    ctrl(fd, ack, 6);
    CHECK_EQ(recv_all(fd), 1);
    CHECK_EQ(rx[0].p[0], PDA_OP_OB_ACKED);
    CHECK_EQ(rx[0].p[1], 0);
    CHECK_EQ(rd32(rx[0].p + 4), id);
    CHECK_EQ(app_outbox_count(), 0);
    /* the same ack again (a lost OB_ACKED): result 1 "wasn't queued", still success */
    ctrl(fd, ack, 6);
    CHECK_EQ(recv_all(fd), 1);
    CHECK_EQ(rx[0].p[0], PDA_OP_OB_ACKED);
    CHECK_EQ(rx[0].p[1], 1);

    /* the plain feed too (PUT replaces the insight feed) */
    json = read_fixture("feed_demo.json", &len);
    CHECK_EQ(put_blob(fd, PDA_KIND_FEED, 1792479601u, (uint8_t *)json, len, crc32_update(0, json, len)),
             PDA_OP_PUT_OK);
    free(json);
    CHECK(app_feed() != NULL && !app_feed()->has_insight);

    /* LOCAL from syncd */
    const char *net = "{\"t\":\"net\",\"state\":\"ok\",\"hub\":\"hub.example.ts.net\",\"last_ok\":1792479000,"
                      "\"msg\":\"\",\"future\":1}";
    send_frame(fd, GLANCE_LOCAL, net, strlen(net));
    CHECK_EQ(app_net()->state, APP_NET_OK);
    CHECK(strcmp(app_net()->hub, "hub.example.ts.net") == 0);
    CHECK_EQ(app_net()->last_ok, 1792479000);
    const char *tn = "{\"t\":\"tailnet\",\"state\":\"needs_login\",\"url\":\"https://login.example/a1\",\"name\":null}";
    send_frame(fd, GLANCE_LOCAL, tn, strlen(tn));
    CHECK_EQ(app_tailnet()->state, APP_TAILNET_NEEDS_LOGIN);
    CHECK(strcmp(app_tailnet()->url, "https://login.example/a1") == 0);
    CHECK_EQ(app_tailnet()->name[0], 0);
    const char *pr = "{\"t\":\"pair_result\",\"ok\":false,\"msg\":\"Code expired\"}";
    send_frame(fd, GLANCE_LOCAL, pr, strlen(pr));
    const char *msg;
    CHECK(!app_pair_result(&msg));
    CHECK(strcmp(msg, "Code expired") == 0);
    const char *unknown = "{\"t\":\"later_feature\",\"x\":1}";
    send_frame(fd, GLANCE_LOCAL, unknown, strlen(unknown));
    send_frame(fd, GLANCE_LOCAL, "not json", 8);
    glance_stats_t st;
    glance_server_stats(gs, &st);
    CHECK_EQ(st.bad_frames, 1);

    /* LOCAL to syncd: sync_now (pull to sync), pair */
    char lb[256];
    CHECK_EQ(glance_server_send_local(gs, lb, local_sync_now(lb, sizeof(lb))), 0);
    CHECK_EQ(local_pair(lb, sizeof(lb), "https://hub.example.ts.net", "AB12-CD34"),
             strlen("{\"t\":\"pair\",\"url\":\"https://hub.example.ts.net\",\"code\":\"AB12-CD34\"}"));
    CHECK_EQ(glance_server_send_local(gs, lb, strlen(lb)), 0);
    CHECK_EQ(recv_all(fd), 2);
    CHECK_EQ(rx[0].type, GLANCE_LOCAL);
    CHECK(rx[0].len == 16 && memcmp(rx[0].p, "{\"t\":\"sync_now\"}", 16) == 0);
    CHECK_EQ(rx[1].type, GLANCE_LOCAL);

    /* BYE ends the sync, not the connection */
    const uint8_t bye = PDA_OP_BYE;
    ctrl(fd, &bye, 1);
    CHECK_EQ(recv_all(fd), 1);
    CHECK_EQ(rx[0].p[0], PDA_OP_BYE_OK);
    pump(5);
    CHECK(glance_server_connected(gs));

    /* a second client is refused, the first stays */
    const int fd2 = connect_to(sock);
    pump(3);
    char c;
    usleep(1000);
    CHECK_EQ(read(fd2, &c, 1), 0); /* closed */
    close(fd2);
    CHECK(glance_server_connected(gs));
    glance_server_stats(gs, &st);
    CHECK_EQ(st.refused, 1);

    /* disconnect in the middle of a PUT: discarded, the feed stays */
    before = app_feed();
    uint8_t b[16] = {PDA_OP_PUT_BEGIN, PDA_KIND_FEED};
    le32(b + 4, 99);
    le32(b + 8, 1000);
    le32(b + 12, 0);
    ctrl(fd, b, 16);
    recv_all(fd);
    close(fd);
    pump(3);
    CHECK(!glance_server_connected(gs));
    CHECK(!app_link_up());
    CHECK(app_feed() == before);
    CHECK_EQ(app_net()->state, APP_NET_UNKNOWN); /* unknown until syncd says again */

    /* reconnect; a frame over 512 bytes drops the client */
    fd = connect_to(sock);
    pump(3);
    CHECK(glance_server_connected(gs));
    const uint8_t huge[3] = {GLANCE_LOCAL, 0x58, 0x02}; /* len 600 */
    CHECK(write(fd, huge, 3) == 3);
    pump(3);
    CHECK(!glance_server_connected(gs));
    close(fd);
}

static void test_framing(void)
{
    /* frames split anywhere reassemble; zero-length frames work */
    static const uint8_t s[] = {0x03, 0, 0, 0x10, 3, 0, 'a', 'b', 'c', 0x05, 0, 0};
    glance_rx_t r;
    glance_rx_reset(&r);
    n_rx = 0;
    for (size_t i = 0; i < sizeof(s); i++) {
        CHECK_EQ(glance_rx_feed(&r, s + i, 1, on_rx, NULL), 0);
    }
    CHECK_EQ(n_rx, 3);
    CHECK_EQ(rx[0].type, GLANCE_STATUS_REQ);
    CHECK_EQ(rx[1].type, GLANCE_LOCAL);
    CHECK_EQ(rx[1].len, 3);
    CHECK_EQ(rx[2].type, GLANCE_INFO_REQ);
    uint8_t out[GLANCE_FRAME_MAX];
    CHECK_EQ(glance_frame_encode(1, out, GLANCE_PAYLOAD_MAX + 1, out), 0);
    const uint8_t over[3] = {1, 0x01, 0x02}; /* 513 */
    CHECK_EQ(glance_rx_feed(&r, over, 3, on_rx, NULL), -1);

    /* LOCAL parsing */
    local_in_t m;
    const char *af = "{\"t\":\"net\",\"state\":\"auth_failed\",\"hub\":null,\"last_ok\":0,\"msg\":\"401\"}";
    CHECK(local_parse(af, strlen(af), &m));
    CHECK_EQ(m.type, LOCAL_NET);
    CHECK_EQ(m.net.state, APP_NET_AUTH_FAILED);
    CHECK_EQ(m.net.hub[0], 0);
    /* LOCAL tz: the offsets of the next days; the new-event form uses the chosen day's */
    CHECK_EQ(app_tz_min_on(20750), app_tz_min()); /* none known: today's display offset */
    const char *tz = "{\"t\":\"tz\",\"day\":20746,\"offsets\":[120,120,120,120,120,60,60,60,60,60,60,60,60,60]}";
    CHECK(local_parse(tz, strlen(tz), &m));
    CHECK_EQ(m.type, LOCAL_TZ);
    CHECK_EQ(m.tz.n, 14);
    local_apply(&m);
    CHECK_EQ(app_tz_min_on(20746), 120);
    CHECK_EQ(app_tz_min_on(20751), 60); /* Sunday 25 Oct: winter time */
    CHECK_EQ(app_tz_min_on(20759), 60);
    CHECK_EQ(app_tz_min_on(20760), app_tz_min()); /* past the 14 days */
    CHECK_EQ(app_tz_min_on(20745), app_tz_min());
    {
        event_form_t f;
        event_form_init(&f, 20746, DEMO_NOW, 120);
        CHECK(event_form_set_day(&f, 20752, 20746)); /* Mon 26 Oct */
        event_form_set_hour(&f, 9);
        event_form_set_title(&f, "Fictional dentist");
        char act[PDA_ACTION_BUF];
        CHECK(event_form_cal_add(&f, 20746, app_tz_min_on(f.day), act) > 0);
        CHECK(strstr(act, "\"start\":\"2026-10-26T09:00:00+01:00\"") != NULL);
    }
    /* a bad offset or day: nothing taken */
    const char *bad[] = {
        "{\"t\":\"tz\",\"day\":20746,\"offsets\":[120,9999]}",
        "{\"t\":\"tz\",\"day\":20746,\"offsets\":[120,60.5]}",
        "{\"t\":\"tz\",\"day\":-1,\"offsets\":[120]}",
        "{\"t\":\"tz\",\"offsets\":[120]}",
        "{\"t\":\"tz\",\"day\":20746,\"offsets\":120}",
    };
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        CHECK(local_parse(bad[i], strlen(bad[i]), &m));
        CHECK_EQ(m.type, LOCAL_TZ);
        CHECK_EQ(m.tz.n, 0);
    }
    local_apply(&m); /* forgets the offsets */
    CHECK_EQ(app_tz_min_on(20751), app_tz_min());
    CHECK(!local_parse("[1]", 3, &m));
    CHECK(!local_parse("{\"x\":1}", 7, &m));
    char lb[32];
    CHECK_EQ(local_pair(lb, sizeof(lb), "https://a-very-long-hub-url.example", "X"), 0); /* never cut */
    char q[128];
    CHECK(local_pair(q, sizeof(q), "https://h/\"x\"", "1") > 0);
    CHECK(strstr(q, "\\\"x\\\"") != NULL);
}

int main(void)
{
    const char *dir = temp_dir();
    char state[64], sock[96];
    snprintf(state, sizeof(state), "%s/s", dir);
    snprintf(sock, sizeof(sock), "%s/glance.sock", dir);
    CHECK_EQ(app_init(state, 42), 0);
    app_set_clock_override(DEMO_NOW);
    gs = glance_server_open(sock);
    CHECK(gs != NULL);
    struct stat stt;
    CHECK(stat(sock, &stt) == 0 && (stt.st_mode & 0777) == 0600);
    test_framing();
    test_session(sock);
    glance_server_close(gs);
    app_deinit();
    return checks_done("test_glance");
}
