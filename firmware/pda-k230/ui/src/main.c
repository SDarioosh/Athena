/* athena-pda: the K230 PDA's UI process (docs/k230-pda-plan.md). One thread: LVGL's timers
 * and the two sockets in one poll() loop.
 *
 * Environment:
 *   ATHENA_PDA_DIR      state (feed.json, d/, ob/, ui.json), default /var/lib/athena-pda
 *   ATHENA_PDA_RUN      sockets (glance.sock, vision.sock), default /run/athena-pda
 *   ATHENA_PDA_BACKEND  headless | sdl | device (default: the best compiled in)
 *   ATHENA_PDA_DRM, ATHENA_PDA_TOUCH  the device's DRM card and evdev touch node */
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>
#include <sys/stat.h>
#include <unistd.h>

#include "app_state.h"
#include "board.h"
#include "glance_server.h"
#include "lvgl.h"
#include "platform.h"
#include "power.h"
#include "theme.h"
#include "ui.h"
#include "vision_server.h"

#define LOG(...) fprintf(stderr, "athena-pda: " __VA_ARGS__)
#define POWER_EVERY_MS 60000

static volatile sig_atomic_t s_quit;
static glance_server_t *s_glance;
static const char *s_run;
static platform_kind_t s_kind;

static void on_signal(int sig)
{
    (void)sig;
    s_quit = 1;
}

static int send_local(const char *json, size_t len)
{
    return glance_server_send_local(s_glance, json, len);
}

static void on_vision(void *ctx, const vision_event_t *ev)
{
    (void)ctx;
    ui_vision_event(ev);
}

static void on_touch(lv_event_t *e)
{
    (void)e;
    app_touch_activity();
}

static platform_kind_t pick_backend(void)
{
    const char *b = getenv("ATHENA_PDA_BACKEND");
    if (b && strcmp(b, "headless") == 0) {
        return PLATFORM_HEADLESS;
    }
    if (b && strcmp(b, "sdl") == 0) {
        return PLATFORM_SDL;
    }
    if (b && strcmp(b, "device") == 0) {
        return PLATFORM_DEVICE;
    }
    return platform_available(PLATFORM_DEVICE) ? PLATFORM_DEVICE
           : platform_available(PLATFORM_SDL)  ? PLATFORM_SDL
                                               : PLATFORM_HEADLESS;
}

static int start_launcher(void)
{
    if (board_start_launcher(s_run) != 0) {
        LOG("can't create %s/start-launcher\n", s_run);
        return -1;
    }
    LOG("handing over to LilyGO's launcher\n");
    s_quit = 1; /* exit 0: S92athena-pda's supervisor starts the launcher */
    return 0;
}

/* sysfs first; the K230 image has none (BOARD.md "Battery"), so on the device the
 * athena-board helper (the keyboard base's gauge, root only); else unknown */
static void update_power(void)
{
    power_info_t p;
    power_read("/sys/class/power_supply", &p);
    if (p.battery_pct < 0 && s_kind == PLATFORM_DEVICE) {
        board_read_battery(BOARD_HELPER, &p);
    }
    app_set_power(p.battery_pct, p.charging, p.usb);
}

int main(void)
{
    const char *dir = getenv("ATHENA_PDA_DIR");
    const char *run = getenv("ATHENA_PDA_RUN");
    dir = dir && dir[0] ? dir : APP_DIR_DEFAULT;
    run = run && run[0] ? run : APP_RUN_DEFAULT;
    s_run = run;

    uint32_t seed = 0;
    if (getrandom(&seed, sizeof(seed), 0) != sizeof(seed)) {
        seed = (uint32_t)getpid() * 2654435761u;
    }
    if (app_init(dir, seed) != 0) {
        LOG("can't use the state directory %s\n", dir);
        return 1;
    }
    if (mkdir(run, 0700) != 0 && errno != EEXIST) {
        LOG("can't create %s: %s\n", run, strerror(errno));
        return 1;
    }

    lv_init();
    const platform_kind_t kind = pick_backend();
    s_kind = kind;
    lv_display_t *disp = platform_create(kind);
    if (!disp) {
        return 1;
    }
    theme_init(disp);
    const ui_hooks_t hooks = {
        .send_local = send_local,
        .screen_power = platform_screen_power,
        .brightness = platform_brightness,
        .start_launcher = start_launcher,
    };
    ui_set_hooks(&hooks);
    update_power();
    ui_init(disp);
    platform_brightness(app_settings()->brightness);
    for (lv_indev_t *in = lv_indev_get_next(NULL); in; in = lv_indev_get_next(in)) {
        lv_indev_add_event_cb(in, on_touch, LV_EVENT_PRESSED, NULL);
    }

    char path[256];
    snprintf(path, sizeof(path), "%s/glance.sock", run);
    s_glance = glance_server_open(path);
    snprintf(path, sizeof(path), "%s/vision.sock", run);
    vision_server_t *vision = vision_server_open(path, on_vision, NULL);
    if (!s_glance || !vision) {
        return 1;
    }
    LOG("started (%s, state in %s, sockets in %s)\n",
        kind == PLATFORM_DEVICE ? "device" : kind == PLATFORM_SDL ? "sdl" : "headless", dir, run);

    struct sigaction sa = {.sa_handler = on_signal};
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    signal(SIGPIPE, SIG_IGN);

    int64_t next_power = app_mono_ms() + POWER_EVERY_MS;
    while (!s_quit) {
        uint32_t wait = lv_timer_handler();
        if (wait > 33) {
            wait = 33;
        }
        struct pollfd pfd[4];
        const int ng = glance_server_pollfds(s_glance, pfd, 2);
        const int nv = vision_server_pollfds(vision, pfd + ng, 2);
        if (poll(pfd, (nfds_t)(ng + nv), (int)wait) < 0 && errno != EINTR) {
            LOG("poll: %s\n", strerror(errno));
            break;
        }
        glance_server_process(s_glance, pfd, ng);
        vision_server_process(vision, pfd + ng, nv);
        if (app_mono_ms() >= next_power) {
            next_power = app_mono_ms() + POWER_EVERY_MS;
            update_power();
        }
    }
    LOG("stopping\n");
    glance_server_close(s_glance);
    vision_server_close(vision);
    app_deinit();
    return 0;
}
