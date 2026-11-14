/* vision.sock: the line parser, the hands-free mapping (the plan's gesture table, the
 * thumbs-up hold), and the server over a real socket (mode on connect and on change, state
 * and presence into app_vision, events to the callback, over-long lines skipped). */
#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "app_state.h"
#include "check.h"
#include "handsfree.h"
#include "test_util.h"
#include "vision_msg.h"
#include "vision_server.h"

static vision_event_t parse(const char *s)
{
    vision_event_t e;
    CHECK(vision_parse_line(s, strlen(s), &e));
    return e;
}

static void test_parse(void)
{
    vision_event_t e = parse("{\"t\":\"state\",\"camera\":\"on\",\"fps\":14.5,\"msg\":\"\"}");
    CHECK_EQ(e.type, VIS_STATE);
    CHECK_EQ(e.camera, VIS_CAM_ON);
    CHECK(e.fps > 14.4 && e.fps < 14.6);
    e = parse("{\"t\":\"state\",\"camera\":\"on\",\"fps\":1e300,\"msg\":\"\"}");
    CHECK(e.fps == 0); /* out of range: 0, the screens cast it to int */
    e = parse("{\"t\":\"state\",\"camera\":\"on\",\"fps\":-3,\"msg\":\"\"}");
    CHECK(e.fps == 0);
    e = parse("{\"t\":\"gesture\",\"g\":\"swipe_left\",\"conf\":0.93}");
    CHECK_EQ(e.type, VIS_GESTURE);
    CHECK_EQ(e.g, VIS_G_SWIPE_LEFT);
    e = parse("{\"t\":\"gesture\",\"g\":\"thumbs_up\",\"conf\":1.7}");
    CHECK(e.conf == 1.0); /* clamped */
    e = parse("{\"t\":\"hand\",\"x\":0.25,\"y\":0.5,\"shape\":\"point\"}");
    CHECK_EQ(e.type, VIS_HAND);
    CHECK_EQ(e.shape, VIS_SHAPE_POINT);
    CHECK(e.x == 0.25 && e.y == 0.5);
    e = parse("{\"t\":\"hand\",\"shape\":\"none\"}");
    CHECK_EQ(e.shape, VIS_SHAPE_NONE);
    e = parse("{\"t\":\"hand\",\"x\":0.1,\"y\":0.1,\"shape\":\"claw\"}");
    CHECK_EQ(e.shape, VIS_SHAPE_OTHER); /* an unknown shape is "other" */
    e = parse("{\"t\":\"presence\",\"present\":true}");
    CHECK(e.present);
    e = parse("{\"t\":\"qr\",\"text\":\"odysseus-pair:v1?url=https%3A%2F%2Fhub.example&code=AB12\"}");
    CHECK(strncmp(e.text, "odysseus-pair:v1", 16) == 0);
    e = parse("{\"t\":\"later\",\"x\":1}");
    CHECK_EQ(e.type, VIS_UNKNOWN);
    vision_event_t bad;
    CHECK(!vision_parse_line("{\"t\":\"gesture\",\"g\":\"wave\"}", 26, &bad));
    CHECK(!vision_parse_line("{\"t\":\"hand\",\"shape\":\"open\"}", 27, &bad)); /* no x, y */
    CHECK(!vision_parse_line("{\"t\":\"state\",\"camera\":\"maybe\"}", 30, &bad));
    CHECK(!vision_parse_line("hello", 5, &bad));
    char m[96];
    const size_t n = vision_mode_line(m, sizeof(m), true, false, true);
    CHECK(n > 0 && m[n - 1] == '\n');
    CHECK(strcmp(m, "{\"t\":\"mode\",\"gestures\":true,\"presence\":false,\"qr\":true}\n") == 0);
}

static int run(hf_state_t *s, const char *line, bool todo, int64_t now, hf_cmd_t *out)
{
    vision_event_t e = parse(line);
    return hf_on_event(s, &e, todo, now, out);
}

static void test_mapping(void)
{
    hf_state_t s;
    hf_cmd_t c[HF_CMDS_MAX];
    hf_reset(&s);
    /* the table */
    static const struct {
        const char *g;
        hf_kind_t k;
    } T[] = {
        {"swipe_left", HF_NEXT}, {"swipe_right", HF_PREV},    {"swipe_up", HF_PAGE_DOWN},
        {"swipe_down", HF_PAGE_UP}, {"palm", HF_HOME},        {"fist", HF_SCREEN_OFF},
    };
    for (size_t i = 0; i < sizeof(T) / sizeof(T[0]); i++) {
        char line[96];
        snprintf(line, sizeof(line), "{\"t\":\"gesture\",\"g\":\"%s\",\"conf\":0.9}", T[i].g);
        CHECK_EQ(run(&s, line, false, 0, c), 1);
        CHECK_EQ(c[0].kind, T[i].k);
    }
    /* low confidence: ignored */
    CHECK_EQ(run(&s, "{\"t\":\"gesture\",\"g\":\"swipe_left\",\"conf\":0.2}", false, 0, c), 0);
    /* point without a hand on screen: nothing; with one: focus at the cursor */
    CHECK_EQ(run(&s, "{\"t\":\"gesture\",\"g\":\"point\",\"conf\":0.9}", false, 0, c), 0);
    CHECK_EQ(run(&s, "{\"t\":\"hand\",\"x\":0.3,\"y\":0.4,\"shape\":\"point\"}", false, 0, c), 1);
    CHECK_EQ(c[0].kind, HF_CURSOR);
    CHECK_EQ(run(&s, "{\"t\":\"gesture\",\"g\":\"point\",\"conf\":0.9}", false, 0, c), 1);
    CHECK_EQ(c[0].kind, HF_FOCUS_AT);
    CHECK(c[0].x == 0.3 && c[0].y == 0.4);

    /* thumbs-up with nothing focused: no hold */
    CHECK_EQ(run(&s, "{\"t\":\"gesture\",\"g\":\"thumbs_up\",\"conf\":0.9}", false, 0, c), 0);
    /* with a to-do focused: the hand turning thumbs-up starts it; 1 s commits */
    CHECK_EQ(run(&s, "{\"t\":\"hand\",\"x\":0.3,\"y\":0.4,\"shape\":\"thumbs_up\"}", true, 1000, c), 2);
    CHECK_EQ(c[0].kind, HF_CURSOR);
    CHECK_EQ(c[1].kind, HF_HOLD_START);
    CHECK(hf_hold_progress(&s, 1500) > 0.49 && hf_hold_progress(&s, 1500) < 0.51);
    CHECK_EQ(run(&s, "{\"t\":\"hand\",\"x\":0.31,\"y\":0.4,\"shape\":\"thumbs_up\"}", true, 1100, c), 1);
    CHECK_EQ(run(&s, "{\"t\":\"gesture\",\"g\":\"thumbs_up\",\"conf\":0.9}", true, 1200, c), 0); /* already */
    for (int64_t t = 1200; t <= 1900; t += 100) { /* vision's ~10 hands a second */
        CHECK_EQ(run(&s, "{\"t\":\"hand\",\"x\":0.31,\"y\":0.4,\"shape\":\"thumbs_up\"}", true, t, c), 1);
    }
    CHECK_EQ(hf_tick(&s, 1999, c), 0);
    CHECK_EQ(hf_tick(&s, 2000, c), 1);
    CHECK_EQ(c[0].kind, HF_HOLD_COMMIT);
    CHECK_EQ(hf_tick(&s, 2100, c), 0); /* once */
    /* aborted by another shape */
    CHECK_EQ(run(&s, "{\"t\":\"hand\",\"x\":0.3,\"y\":0.4,\"shape\":\"open\"}", true, 3000, c), 1);
    CHECK_EQ(run(&s, "{\"t\":\"hand\",\"x\":0.3,\"y\":0.4,\"shape\":\"thumbs_up\"}", true, 3100, c), 2);
    CHECK_EQ(run(&s, "{\"t\":\"hand\",\"x\":0.3,\"y\":0.4,\"shape\":\"open\"}", true, 3500, c), 2);
    CHECK_EQ(c[1].kind, HF_HOLD_CANCEL);
    CHECK_EQ(hf_tick(&s, 4200, c), 0);
    /* aborted by the hand going (cancel, then the cursor hides) */
    CHECK_EQ(run(&s, "{\"t\":\"gesture\",\"g\":\"thumbs_up\",\"conf\":0.9}", true, 5000, c), 1);
    CHECK_EQ(c[0].kind, HF_HOLD_START);
    CHECK_EQ(run(&s, "{\"t\":\"hand\",\"shape\":\"none\"}", true, 5400, c), 2);
    CHECK_EQ(c[0].kind, HF_HOLD_CANCEL);
    CHECK_EQ(c[1].kind, HF_CURSOR_HIDE);
    /* aborted by another gesture, which still runs */
    CHECK_EQ(run(&s, "{\"t\":\"gesture\",\"g\":\"thumbs_up\",\"conf\":0.9}", true, 6000, c), 1);
    CHECK_EQ(run(&s, "{\"t\":\"gesture\",\"g\":\"swipe_left\",\"conf\":0.9}", true, 6200, c), 2);
    CHECK_EQ(c[0].kind, HF_HOLD_CANCEL);
    CHECK_EQ(c[1].kind, HF_NEXT);
    /* aborted by the focus moving */
    CHECK_EQ(run(&s, "{\"t\":\"gesture\",\"g\":\"thumbs_up\",\"conf\":0.9}", true, 7000, c), 1);
    CHECK_EQ(hf_focus_changed(&s, c), 1);
    CHECK_EQ(c[0].kind, HF_HOLD_CANCEL);
    CHECK_EQ(hf_tick(&s, 9000, c), 0);

    /* the timer alone never commits: no thumbs-up hand for HF_THUMB_FRESH_MS cancels */
    CHECK_EQ(run(&s, "{\"t\":\"hand\",\"x\":0.3,\"y\":0.4,\"shape\":\"thumbs_up\"}", true, 10000, c), 2);
    CHECK_EQ(c[1].kind, HF_HOLD_START);
    CHECK_EQ(hf_tick(&s, 10000 + HF_THUMB_FRESH_MS, c), 0);
    CHECK_EQ(hf_tick(&s, 10000 + HF_THUMB_FRESH_MS + 1, c), 1);
    CHECK_EQ(c[0].kind, HF_HOLD_CANCEL);
    CHECK_EQ(hf_tick(&s, 11000, c), 0); /* and never commits */
    /* a gesture-started hold counts the gesture as a sighting, then needs hands */
    CHECK_EQ(run(&s, "{\"t\":\"hand\",\"x\":0.3,\"y\":0.4,\"shape\":\"open\"}", true, 12000, c), 1);
    CHECK_EQ(run(&s, "{\"t\":\"gesture\",\"g\":\"thumbs_up\",\"conf\":0.9}", true, 12000, c), 1);
    CHECK_EQ(c[0].kind, HF_HOLD_START);
    CHECK_EQ(hf_tick(&s, 12300, c), 0);
    CHECK_EQ(hf_tick(&s, 12000 + HF_THUMB_FRESH_MS + 1, c), 1);
    CHECK_EQ(c[0].kind, HF_HOLD_CANCEL);
    /* the screen going off or vision dropping: cancel and hide the cursor */
    CHECK_EQ(run(&s, "{\"t\":\"hand\",\"x\":0.3,\"y\":0.4,\"shape\":\"thumbs_up\"}", true, 13000, c), 2);
    CHECK_EQ(hf_input_lost(&s, c), 2);
    CHECK_EQ(c[0].kind, HF_HOLD_CANCEL);
    CHECK_EQ(c[1].kind, HF_CURSOR_HIDE);
    CHECK_EQ(hf_tick(&s, 14500, c), 0);
    CHECK_EQ(hf_input_lost(&s, c), 0);
}

/* ---- the server ---- */

static vision_server_t *vs;
static int n_events;
static vision_event_t last;

static void on_event(void *ctx, const vision_event_t *ev)
{
    (void)ctx;
    n_events++;
    last = *ev;
}

static void pump(void)
{
    for (int i = 0; i < 3; i++) {
        struct pollfd pfd[2];
        const int n = vision_server_pollfds(vs, pfd, 2);
        poll(pfd, (nfds_t)n, 2);
        vision_server_process(vs, pfd, n);
    }
}

static int read_line(int fd, char *out, size_t cap)
{
    pump();
    usleep(1000);
    const ssize_t r = read(fd, out, cap - 1);
    out[r > 0 ? r : 0] = 0;
    return (int)r;
}

static void say(int fd, const char *s)
{
    CHECK(write(fd, s, strlen(s)) == (ssize_t)strlen(s));
    pump();
}

static void test_server(const char *dir)
{
    char sock[96];
    snprintf(sock, sizeof(sock), "%s/vision.sock", dir);
    vs = vision_server_open(sock, on_event, NULL);
    CHECK(vs != NULL);
    const int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    struct sockaddr_un sa = {.sun_family = AF_UNIX};
    snprintf(sa.sun_path, sizeof(sa.sun_path), "%s", sock);
    CHECK(connect(fd, (struct sockaddr *)&sa, sizeof(sa)) == 0);
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
    char line[2048];
    /* mode on connect: everything off (camera off) */
    CHECK(read_line(fd, line, sizeof(line)) > 0);
    CHECK(strcmp(line, "{\"t\":\"mode\",\"gestures\":false,\"presence\":false,\"qr\":false}\n") == 0);
    CHECK(app_vision()->connected);
    /* Hands-free on: mode follows */
    app_settings_t s = *app_settings();
    s.hands_free = true;
    app_set_settings(&s);
    CHECK(read_line(fd, line, sizeof(line)) > 0);
    CHECK(strcmp(line, "{\"t\":\"mode\",\"gestures\":true,\"presence\":false,\"qr\":false}\n") == 0);
    CHECK(read_line(fd, line, sizeof(line)) <= 0); /* not again */

    say(fd, "{\"t\":\"state\",\"camera\":\"on\",\"fps\":15,\"msg\":\"\"}\n");
    CHECK_EQ(app_vision()->camera, APP_CAMERA_ON);
    say(fd, "{\"t\":\"presence\",\"present\":true}\n{\"t\":\"gesture\",\"g\":\"palm\",\"conf\":0.8}\n");
    CHECK(app_vision()->present);
    CHECK_EQ(last.type, VIS_GESTURE);
    CHECK_EQ(last.g, VIS_G_PALM);
    /* a line split over two writes */
    say(fd, "{\"t\":\"hand\",\"x\":0.5,");
    const int before = n_events;
    say(fd, "\"y\":0.5,\"shape\":\"open\"}\n");
    CHECK_EQ(n_events, before + 1);
    CHECK_EQ(last.shape, VIS_SHAPE_OPEN);
    /* a line over 1024 bytes is skipped, the next one works */
    char big[1300];
    memset(big, 'x', sizeof(big));
    big[0] = '{';
    big[sizeof(big) - 1] = '\n';
    CHECK(write(fd, big, sizeof(big)) == (ssize_t)sizeof(big));
    say(fd, "{\"t\":\"gesture\",\"g\":\"fist\",\"conf\":0.9}\n");
    CHECK_EQ(last.g, VIS_G_FIST);
    vision_stats_t st;
    vision_server_stats(vs, &st);
    CHECK_EQ(st.long_lines, 1);
    /* vision goes: camera off, the hand gone */
    close(fd);
    pump();
    CHECK(!app_vision()->connected);
    CHECK_EQ(app_vision()->camera, APP_CAMERA_OFF);
    CHECK_EQ(last.type, VIS_HAND);
    CHECK_EQ(last.shape, VIS_SHAPE_NONE);
    vision_server_close(vs);
}

int main(void)
{
    test_parse();
    test_mapping();
    const char *dir = temp_dir();
    char state[64];
    snprintf(state, sizeof(state), "%s/s", dir);
    CHECK_EQ(app_init(state, 3), 0);
    test_server(dir);
    app_deinit();
    return checks_done("test_vision");
}
