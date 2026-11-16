/* The end-to-end tests' control socket (headless_ctl.h). Headless backend only. */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "app_state.h"
#include "headless_ctl.h"
#include "platform.h"
#include "ui.h"

#define LOG(...) fprintf(stderr, "athena-pda: test-ctl: " __VA_ARGS__)
#define LINE_MAX_ 1024

struct headless_ctl {
    int lfd, cfd;
    char path[108];
    char buf[LINE_MAX_];
    size_t len;
    /* a command still running: the main loop (LVGL and both sockets) goes on meanwhile */
    int64_t release_at, reply_at; /* ms (now_ms), 0 = none */
    int32_t rx, ry;
    char answer[256];
};

static int64_t now_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (int64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

headless_ctl_t *headless_ctl_open(const char *path)
{
    struct sockaddr_un a = {.sun_family = AF_UNIX};
    if (strlen(path) >= sizeof(a.sun_path)) {
        LOG("path too long\n");
        return NULL;
    }
    headless_ctl_t *c = calloc(1, sizeof(*c));
    if (!c) {
        return NULL;
    }
    snprintf(c->path, sizeof(c->path), "%s", path);
    snprintf(a.sun_path, sizeof(a.sun_path), "%s", path);
    c->cfd = -1;
    c->lfd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    unlink(path);
    if (c->lfd < 0 || bind(c->lfd, (struct sockaddr *)&a, sizeof(a)) != 0 || chmod(path, 0600) != 0 ||
        listen(c->lfd, 1) != 0) {
        LOG("can't listen on %s: %s\n", path, strerror(errno));
        if (c->lfd >= 0) {
            close(c->lfd);
        }
        free(c);
        return NULL;
    }
    LOG("listening on %s (headless test control)\n", path);
    return c;
}

void headless_ctl_close(headless_ctl_t *c)
{
    if (!c) {
        return;
    }
    if (c->cfd >= 0) {
        close(c->cfd);
    }
    close(c->lfd);
    unlink(c->path);
    free(c);
}

int headless_ctl_pollfds(headless_ctl_t *c, struct pollfd *out, int max)
{
    if (!c || max < 1) {
        return 0;
    }
    out[0] = (struct pollfd){.fd = c->cfd >= 0 ? c->cfd : c->lfd, .events = POLLIN};
    return 1;
}

/* answer after ms of the main loop */
static void later(headless_ctl_t *c, uint32_t ms, const char *answer)
{
    snprintf(c->answer, sizeof(c->answer), "%s", answer);
    c->reply_at = now_ms() + ms;
}

/* a finger down now, up 80 ms later, the answer 80 ms after that */
static void tap(headless_ctl_t *c, int32_t x, int32_t y, uint32_t settle)
{
    headless_touch(x, y, true);
    c->rx = x;
    c->ry = y;
    c->release_at = now_ms() + 80;
    later(c, 160 + settle, "ok\n");
}

static lv_obj_t *find(lv_obj_t *root, const char *s, int anywhere, const lv_obj_class_t *cls)
{
    if (!root || lv_obj_has_flag(root, LV_OBJ_FLAG_HIDDEN)) {
        return NULL;
    }
    if (cls && lv_obj_check_type(root, cls)) {
        return root;
    }
    if (!cls && lv_obj_check_type(root, &lv_label_class)) {
        const char *t = lv_label_get_text(root);
        const bool hit = anywhere == 2   ? strcmp(t ? t : "", s) == 0
                         : anywhere == 1 ? t && strstr(t, s) != NULL
                                         : t && strncmp(t, s, strlen(s)) == 0;
        if (hit) {
            return root;
        }
    }
    for (uint32_t i = 0; i < lv_obj_get_child_count(root); i++) {
        lv_obj_t *r = find(lv_obj_get_child(root, (int32_t)i), s, anywhere, cls);
        if (r) {
            return r;
        }
    }
    return NULL;
}

/* the top layer first (keyboard, sheet, toasts), then the screen */
static lv_obj_t *find_any(const char *s, int anywhere, const lv_obj_class_t *cls)
{
    lv_obj_t *r = find(lv_layer_top(), s, anywhere, cls);
    return r ? r : find(lv_screen_active(), s, anywhere, cls);
}

/* the n-th (from 0) visible text field under root, depth first */
static lv_obj_t *nth_textarea_r(lv_obj_t *root, int *n)
{
    if (lv_obj_has_flag(root, LV_OBJ_FLAG_HIDDEN)) {
        return NULL;
    }
    if (lv_obj_check_type(root, &lv_textarea_class) && (*n)-- == 0) {
        return root;
    }
    for (uint32_t i = 0; i < lv_obj_get_child_count(root); i++) {
        lv_obj_t *r = nth_textarea_r(lv_obj_get_child(root, (int32_t)i), n);
        if (r) {
            return r;
        }
    }
    return NULL;
}

static lv_obj_t *nth_textarea(lv_obj_t *root, int n)
{
    return n < 0 ? NULL : nth_textarea_r(root, &n);
}

static void reply(headless_ctl_t *c, const char *fmt, const char *arg)
{
    char out[512];
    const int n = snprintf(out, sizeof(out), fmt, arg ? arg : "");
    if (n > 0 && c->cfd >= 0) {
        const size_t len = (size_t)n < sizeof(out) ? (size_t)n : sizeof(out) - 1;
        if (write(c->cfd, out, len) < 0) {
            LOG("write: %s\n", strerror(errno));
        }
    }
}

static void command(headless_ctl_t *c, char *line)
{
    char *arg = strchr(line, ' ');
    if (arg) {
        *arg++ = '\0';
    } else {
        arg = line + strlen(line);
    }
    if (strcmp(line, "tap") == 0) {
        int x, y;
        if (sscanf(arg, "%d %d", &x, &y) != 2) {
            reply(c, "err tap X Y\n", NULL);
            return;
        }
        tap(c, x, y, 0);
    } else if (strcmp(line, "click") == 0) {
        lv_obj_t *l = find_any(arg, 2, NULL); /* the exact text first, then a prefix */
        if (!l) {
            l = find_any(arg, 0, NULL);
        }
        if (!l) {
            reply(c, "err no label %s\n", arg);
            return;
        }
        lv_obj_scroll_to_view_recursive(l, LV_ANIM_OFF); /* as a finger would scroll first */
        lv_obj_update_layout(lv_screen_active());
        lv_area_t a;
        lv_obj_get_coords(l, &a);
        tap(c, (a.x1 + a.x2) / 2, (a.y1 + a.y2) / 2, 0);
    } else if (strcmp(line, "has") == 0) {
        reply(c, find_any(arg, 1, NULL) ? "ok yes\n" : "ok no\n", NULL);
    } else if (strcmp(line, "field") == 0) {
        lv_obj_t *ta = nth_textarea(lv_screen_active(), atoi(arg));
        if (!ta) {
            reply(c, "err no text field %s\n", arg);
            return;
        }
        lv_area_t a;
        lv_obj_get_coords(ta, &a);
        tap(c, (a.x1 + a.x2) / 2, (a.y1 + a.y2) / 2, 250); /* the keyboard slides in */
    } else if (strcmp(line, "type") == 0 || strcmp(line, "clear") == 0) {
        lv_obj_t *kb = find_any("", 0, &lv_keyboard_class);
        lv_obj_t *ta = kb ? lv_keyboard_get_textarea(kb) : NULL;
        if (!ta) {
            reply(c, "err no keyboard open\n", NULL);
            return;
        }
        if (strcmp(line, "clear") == 0) {
            lv_textarea_set_text(ta, "");
        } else {
            lv_textarea_add_text(ta, arg);
        }
        later(c, 50, "ok\n");
    } else if (strcmp(line, "tab") == 0) {
        const int t = atoi(arg);
        if (t < 0 || t >= UI_TABS) {
            reply(c, "err tab 0..5\n", NULL);
            return;
        }
        ui_show_tab((ui_tab_t)t, false);
        later(c, 100, "ok\n");
    } else if (strcmp(line, "clock") == 0) {
        app_set_clock_override(strtoll(arg, NULL, 10));
        ui_refresh_now();
        later(c, 50, "ok\n");
    } else if (strcmp(line, "state") == 0) {
        char st[128];
        snprintf(st, sizeof(st), "tab=%d detail=%d sheet=%d off=%d outbox=%d", (int)ui_current_tab(),
                 ui_in_detail() ? 1 : 0, ui_sheet_is_open() ? 1 : 0, ui_screen_is_off() ? 1 : 0, app_outbox_count());
        reply(c, "ok %s\n", st);
    } else if (strcmp(line, "png") == 0) {
        reply(c, headless_png(arg) == 0 ? "ok\n" : "err png\n", NULL);
    } else if (strcmp(line, "wait") == 0) {
        later(c, (uint32_t)atoi(arg), "ok\n");
    } else {
        reply(c, "err unknown command %s\n", line);
    }
}

static void run_lines(headless_ctl_t *c)
{
    char *nl;
    while (!c->reply_at && (nl = memchr(c->buf, '\n', c->len)) != NULL) {
        *nl = '\0';
        command(c, c->buf);
        const size_t used = (size_t)(nl - c->buf) + 1;
        memmove(c->buf, nl + 1, c->len - used);
        c->len -= used;
    }
}

void headless_ctl_process(headless_ctl_t *c, const struct pollfd *fds, int n)
{
    if (!c) {
        return;
    }
    const int64_t now = now_ms();
    if (c->release_at && now >= c->release_at) {
        headless_touch(c->rx, c->ry, false);
        c->release_at = 0;
    }
    if (c->reply_at && now >= c->reply_at) {
        c->reply_at = 0;
        reply(c, "%s", c->answer);
        run_lines(c);
    }
    if (n < 1 || !(fds[0].revents & (POLLIN | POLLHUP | POLLERR))) {
        return;
    }
    if (c->cfd < 0) {
        c->cfd = accept4(c->lfd, NULL, NULL, SOCK_CLOEXEC);
        c->len = 0;
        return;
    }
    const ssize_t r = read(c->cfd, c->buf + c->len, sizeof(c->buf) - 1 - c->len);
    if (r <= 0) {
        close(c->cfd);
        c->cfd = -1;
        c->reply_at = 0;
        return;
    }
    c->len += (size_t)r;
    run_lines(c);
    if (c->len == sizeof(c->buf) - 1) {
        c->len = 0; /* a line too long: dropped */
    }
}
