/* The app's console commands (firmware/pda/README.md). `put` loads a blob through the same
 * PUT state machine and store as BLE, so a feed loaded over USB is checked and stored
 * exactly like one from the phone. */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ble_pda.h"
#include "esp_log.h"
#include "gesture.h"
#include "isotime.h"
#include "mbedtls/base64.h"
#include "pda_app.h"
#include "pda_console.h"
#include "pda_epd.h"
#include "pda_proto.h"
#include "t5_board.h"
#include "ui_pda.h"

static pda_put_t s_put; /* console task only */
static uint8_t s_frame[PDA_PROTO_FRAME_MAX];

static const char *err_name(uint8_t code)
{
    static const char *const names[] = {"ok",       "bad_length", "unknown_cmd", "not_found",
                                        "busy",     "mtu_too_small", "io_error", "bad_time",
                                        "bad_crc",  "too_large",  "bad_state"};
    return code < sizeof(names) / sizeof(names[0]) ? names[code] : "?";
}

static uint32_t rd32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/* Print what a BLE relay would get on `data`. */
static int console_send(void *ctx, const uint8_t *f, size_t len)
{
    (void)ctx;
    (void)len;
    switch (f[0]) {
    case PDA_OP_PUT_READY: printf("put: ready (kind %u, id %" PRIu32 ")\n", f[1], rd32(f + 4)); break;
    case PDA_OP_PUT_OK: printf("put: ok (kind %u, id %" PRIu32 ")\n", f[1], rd32(f + 4)); break;
    case PDA_OP_ERROR:
        printf("put: ERR %s (cmd 0x%02x, arg %" PRIu32 ")\n", err_name(f[2]), f[1], rd32(f + 4));
        break;
    default: printf("put: frame 0x%02x\n", f[0]); break;
    }
    return 0;
}

static void run_put(const uint8_t *cmd, size_t len)
{
    pda_cmd_t c;
    const int rc = pda_parse_cmd(cmd, len, &c);
    if (rc != 0) {
        printf("put: ERR %s\n", err_name((uint8_t)rc));
        return;
    }
    pda_execute(&c, &s_put, pda_app_store(), NULL, console_send, NULL, s_frame);
}

static void le32(uint8_t *p, uint32_t v)
{
    for (int i = 0; i < 4; i++) {
        p[i] = (uint8_t)(v >> (8 * i));
    }
}

static int cmd_put(int argc, char **argv)
{
    if (argc >= 5 && strcmp(argv[1], "begin") == 0) {
        uint8_t b[16] = {PDA_OP_PUT_BEGIN, (uint8_t)atoi(argv[2])};
        le32(b + 4, argc >= 6 ? (uint32_t)strtoul(argv[5], NULL, 10) : 0);
        le32(b + 8, (uint32_t)strtoul(argv[3], NULL, 10));
        le32(b + 12, (uint32_t)strtoul(argv[4], NULL, 16));
        run_put(b, sizeof(b));
        return 0;
    }
    if (argc >= 3 && strcmp(argv[1], "data") == 0) {
        static uint8_t b[PDA_PROTO_PUT_DATA_HDR_LEN + 400];
        size_t n = 0;
        if (mbedtls_base64_decode(b + 5, sizeof(b) - 5, &n, (const unsigned char *)argv[2],
                                  strlen(argv[2])) != 0) {
            printf("put: ERR base64\n");
            return 1;
        }
        b[0] = PDA_OP_PUT_DATA;
        le32(b + 1, s_put.received);
        run_put(b, 5 + n);
        return 0;
    }
    if (argc >= 2 && strcmp(argv[1], "end") == 0) {
        const uint8_t b[1] = {PDA_OP_PUT_END};
        run_put(b, 1);
        return 0;
    }
    if (argc >= 2 && strcmp(argv[1], "abort") == 0) {
        pda_put_abort(&s_put);
        printf("put: aborted\n");
        return 0;
    }
    printf("usage: put begin <kind 1|2> <bytes> <crc32 hex> [blob_id] | put data <base64> | "
           "put end | put abort\n");
    return 1;
}

static int cmd_ob(int argc, char **argv)
{
    if (argc >= 2 && strcmp(argv[1], "ack") == 0 && argc >= 3) {
        bool was = false;
        const uint32_t id = (uint32_t)strtoul(argv[2], NULL, 16);
        const uint8_t result = argc >= 4 ? (uint8_t)atoi(argv[3]) : 0;
        pda_app_store()->ob_ack(NULL, id, result, &was);
        printf("ob: %08" PRIx32 " %s\n", id, was ? "removed" : "was not queued");
        ble_pda_kick();
        return 0;
    }
    pda_app_lock();
    const outbox_t *ob = pda_app_outbox();
    printf("outbox: %d waiting, next id %08" PRIx32 "\n", ob->n, ob->next_id);
    pda_ob_entry_t *e = malloc(sizeof(*e));
    for (int i = 0; e && i < ob->n; i++) {
        if (outbox_read(ob, ob->e[i].id, e) == 0) {
            printf("  %08" PRIx32 "%s %.*s\n", e->action_id, ob->e[i].listed ? " (listed)" : "",
                   (int)e->len, e->json);
        }
    }
    free(e);
    pda_app_unlock();
    return 0;
}

static void post_gesture(gesture_kind_t k, int x, int y)
{
    const pda_ev_t ev = {.type = PDA_EV_GESTURE, .gesture = (uint8_t)k, .x = (int16_t)x, .y = (int16_t)y};
    pda_app_post(&ev);
}

static int cmd_tap(int argc, char **argv)
{
    if (argc < 3) {
        printf("usage: tap|hold <x> <y>\n");
        return 1;
    }
    post_gesture(strcmp(argv[0], "hold") == 0 ? GESTURE_LONG : GESTURE_TAP, atoi(argv[1]), atoi(argv[2]));
    return 0;
}

static int cmd_swipe(int argc, char **argv)
{
    static const struct {
        const char *n;
        gesture_kind_t k;
    } m[] = {{"left", GESTURE_SWIPE_LEFT}, {"right", GESTURE_SWIPE_RIGHT}, {"up", GESTURE_SWIPE_UP},
             {"down", GESTURE_SWIPE_DOWN}};
    for (size_t i = 0; argc >= 2 && i < 4; i++) {
        if (strcmp(argv[1], m[i].n) == 0) {
            post_gesture(m[i].k, 480, 270);
            return 0;
        }
    }
    printf("usage: swipe left|right|up|down\n");
    return 1;
}

static int cmd_screen(int argc, char **argv)
{
    const int s = argc >= 2 ? ui_screen_from_name(argv[1]) : -1;
    if (s < 0) {
        printf("usage: screen <");
        for (int i = 0; i < UI_SCREEN_COUNT; i++) {
            printf("%s%s", i ? "|" : "", ui_screen_name((ui_screen_t)i));
        }
        printf(">\n");
        return 1;
    }
    const pda_ev_t ev = {.type = PDA_EV_SCREEN, .arg = s};
    pda_app_post(&ev);
    return 0;
}

static int cmd_redraw(int argc, char **argv)
{
    const pda_ev_t ev = {.type = PDA_EV_REDRAW, .arg = argc >= 2 && strcmp(argv[1], "clean") == 0};
    pda_app_post(&ev);
    return 0;
}

static int cmd_button(int argc, char **argv)
{
    if (argc < 2) {
        printf("usage: button boot|side [long]\n");
        return 1;
    }
    const int b = strcmp(argv[1], "side") == 0 ? T5_BUTTON_SIDE : T5_BUTTON_BOOT;
    const pda_ev_t ev = {.type = PDA_EV_BUTTON, .arg = (b << 1) | (argc >= 3 && strcmp(argv[2], "long") == 0)};
    pda_app_post(&ev);
    return 0;
}

static int cmd_light(int argc, char **argv)
{
    if (argc >= 2) {
        pda_app_set_light(atoi(argv[1]));
    }
    printf("front light %d%% (off after %d s idle; a touch or button turns it on)\n", pda_app_light(),
           PDA_LIGHT_IDLE_MS / 1000);
    return 0;
}

static int cmd_sleep(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    printf("deep sleep: press BOOT to wake (the USB console goes away)\n");
    const pda_ev_t ev = {.type = PDA_EV_SLEEP};
    pda_app_post(&ev);
    return 0;
}

static int cmd_ble(int argc, char **argv)
{
    if (argc >= 2 && strcmp(argv[1], "pair") == 0) {
        ble_pda_open_window();
    } else if (argc >= 2 && strcmp(argv[1], "forget") == 0) {
        ble_pda_forget_bonds();
    }
    ble_pda_print();
    return 0;
}

static int cmd_time(int argc, char **argv)
{
    if (argc >= 2) {
        int64_t t;
        if (!isotime_parse(argv[1], &t)) {
            printf("usage: time [<epoch | YYYY-MM-DDTHH:MM:SSZ> [utc_offset_min]]\n");
            return 1;
        }
        pda_app_set_time(t * 1000, (int16_t)(argc >= 3 ? atoi(argv[2]) : pda_app_tz_min()));
    }
    char buf[24];
    isotime_format(pda_app_now(), buf, sizeof(buf));
    printf("time: %s (%s), UTC%+d min\n", buf, pda_app_time_valid() ? "set" : "NOT set", pda_app_tz_min());
    return 0;
}

static int cmd_rotate(int argc, char **argv)
{
    if (argc >= 2) {
        const int deg = atoi(argv[1]);
        if (deg != 90 && deg != 270) {
            printf("usage: rotate [90|270]\n");
            return 1;
        }
        pda_epd_set_rotation(deg);
        const pda_ev_t ev = {.type = PDA_EV_REDRAW};
        pda_app_post(&ev);
    }
    printf("rotation: portrait %d (270 = LilyGO's default hold, 90 = turned 180)\n",
           pda_epd_rotation());
    return 0;
}

static int cmd_open(int argc, char **argv)
{
    if (argc < 2 || strncmp(argv[1], "transcript:", 11) != 0) {
        printf("usage: open transcript:<id>@<from>-<to>\n");
        return 1;
    }
    ui_pda_open(argv[1]);
    return 0;
}

static int cmd_wipe(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    return pda_app_wipe() == ESP_OK ? 0 : 1;
}

static int cmd_feed(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    pda_app_lock();
    const feed_t *f = pda_app_feed();
    if (!f) {
        printf("feed: none\n");
    } else {
        printf("feed: version %" PRIu32 ", %d conversations, %d actions, %d events, %d templates, "
               "briefing %s\n",
               f->version, f->n_conv, f->n_actions, f->n_events, f->n_templates,
               f->has_briefing ? (f->brief_date ? f->brief_date : "?") : "none");
    }
    pda_app_unlock();
    return 0;
}

static int cmd_detail(int argc, char **argv)
{
    if (argc < 2) {
        printf("usage: detail <ref>\n");
        return 1;
    }
    detail_t *d = pda_app_load_detail(argv[1]);
    if (!d) {
        printf("detail %s: not cached\n", argv[1]);
        return 1;
    }
    printf("detail %s: %d segments, %d points, next %s\n", d->ref, d->n_seg, d->n_points,
           d->next ? d->next : "none");
    detail_free(d);
    free(d);
    return 0;
}

void pda_console_register_app(void)
{
    pda_console_register("put", "Load a blob: put begin <kind> <bytes> <crc32 hex>, put data <b64>, put end",
                         cmd_put);
    pda_console_register("ob", "Outbox: ob | ob ack <id hex> [result]", cmd_ob);
    pda_console_register("tap", "tap <x> <y>: a touch tap", cmd_tap);
    pda_console_register("hold", "hold <x> <y>: a long press", cmd_tap);
    pda_console_register("swipe", "swipe left|right|up|down", cmd_swipe);
    pda_console_register("screen", "screen <name>: show a screen", cmd_screen);
    pda_console_register("redraw", "redraw [clean]", cmd_redraw);
    pda_console_register("button", "button boot|side [long]", cmd_button);
    pda_console_register("light", "light [0-100]: front light level (kept in NVS)", cmd_light);
    pda_console_register("sleep", "Deep sleep until BOOT is pressed (as holding BOOT)", cmd_sleep);
    pda_console_register("ble", "ble [pair|forget]: state, pairing window, delete bonds", cmd_ble);
    pda_console_register("time", "time [<epoch|ISO> [utc_offset_min]]", cmd_time);
    pda_console_register("feed", "Summary of the stored feed", cmd_feed);
    pda_console_register("detail", "detail <ref>: a cached detail", cmd_detail);
    pda_console_register("rotate", "rotate [90|270]: portrait orientation (kept in NVS)", cmd_rotate);
    pda_console_register("open", "open transcript:<ref>: show a transcript page", cmd_open);
    pda_console_register("wipe", "Delete the feed, details and outbox", cmd_wipe);
}
