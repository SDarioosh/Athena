#include "app_state.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "crc32.h"
#include "feed_store.h"
#include "json.h"
#include "pda_time.h"

#define LOG(...) fprintf(stderr, "athena-pda: " __VA_ARGS__)

static struct {
    char dir[APP_DIR_MAX + 1];
    feed_t *feed;
    outbox_t *outbox;
    app_pending_t *pending; /* OUTBOX_MAX */
    int n_pending;
    app_failed_t failed[APP_FAILED_MAX];
    int n_failed;
    int tz_min;
    int64_t clock_override, mono_override;
    int64_t last_touch_ms;
    int8_t battery;
    bool charging, usb;
    uint32_t boot_id;
    bool link;
    int64_t last_link_s;
    app_net_t net;
    app_tailnet_t tailnet;
    bool pair_ok;
    char pair_msg[128];
    app_vision_t vision;
    bool qr_scan;
    app_settings_t settings;
    struct {
        app_listener_t fn;
        void *ctx;
    } listeners[APP_LISTENERS_MAX];
} S = {.battery = -1, .last_touch_ms = -APP_ACTIVE_MS};

static void emit(app_ev_t ev, int32_t arg)
{
    for (int i = 0; i < APP_LISTENERS_MAX; i++) {
        if (S.listeners[i].fn) {
            S.listeners[i].fn(S.listeners[i].ctx, ev, arg);
        }
    }
}

void app_add_listener(app_listener_t fn, void *ctx)
{
    for (int i = 0; i < APP_LISTENERS_MAX; i++) {
        if (!S.listeners[i].fn) {
            S.listeners[i].fn = fn;
            S.listeners[i].ctx = ctx;
            return;
        }
    }
    LOG("too many listeners\n");
}

void app_remove_listener(app_listener_t fn, void *ctx)
{
    for (int i = 0; i < APP_LISTENERS_MAX; i++) {
        if (S.listeners[i].fn == fn && S.listeners[i].ctx == ctx) {
            S.listeners[i].fn = NULL;
        }
    }
}

/* ---- ui.json ---- */

static void settings_defaults(app_settings_t *s)
{
    *s = (app_settings_t){.hands_free = false, .presence_wake = false, .brightness = 80};
}

static void load_settings(void)
{
    settings_defaults(&S.settings);
    char path[APP_DIR_MAX + 16];
    snprintf(path, sizeof(path), "%s/ui.json", S.dir);
    FILE *f = fopen(path, "rb");
    if (!f) {
        return;
    }
    char buf[1024];
    const size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    buf[n] = 0;
    json_tok_t t[48];
    const int nt = json_parse(buf, n, t, 48);
    if (nt < 1 || t[0].type != JSON_OBJ) {
        LOG("ui.json doesn't parse; defaults\n");
        return;
    }
    const json_doc_t d = {buf, t, nt};
    bool b;
    int64_t v;
    if (json_bool(&d, json_get(&d, 0, "hands_free"), &b)) {
        S.settings.hands_free = b;
    }
    if (json_bool(&d, json_get(&d, 0, "presence_wake"), &b)) {
        S.settings.presence_wake = b;
    }
    if (json_int(&d, json_get(&d, 0, "brightness"), &v)) {
        S.settings.brightness = v < 5 ? 5 : v > 100 ? 100 : (int)v;
    }
    if (json_int(&d, json_get(&d, 0, "tz_min"), &v) && v >= -PDA_TZ_MAX_MIN && v <= PDA_TZ_MAX_MIN) {
        S.tz_min = (int)v;
    }
}

static int save_settings(void)
{
    char path[APP_DIR_MAX + 16], tmp[APP_DIR_MAX + 16];
    snprintf(path, sizeof(path), "%s/ui.json", S.dir);
    snprintf(tmp, sizeof(tmp), "%s/ui.tmp", S.dir);
    FILE *f = fopen(tmp, "wb");
    if (!f) {
        return -3;
    }
    fprintf(f, "{\"v\":1,\"hands_free\":%s,\"presence_wake\":%s,\"brightness\":%d,\"tz_min\":%d}\n",
            S.settings.hands_free ? "true" : "false", S.settings.presence_wake ? "true" : "false",
            S.settings.brightness, S.tz_min);
    bool ok = fflush(f) == 0;
    ok &= fsync(fileno(f)) == 0;
    ok &= fclose(f) == 0;
    if (!ok || rename(tmp, path) != 0) {
        remove(tmp);
        return -3;
    }
    return 0;
}

/* ---- pending list, rebuilt from the outbox ---- */

static void rebuild_pending(void)
{
    S.n_pending = 0;
    pda_ob_entry_t e;
    for (int i = 0; i < S.outbox->n; i++) {
        app_pending_t *p = &S.pending[S.n_pending];
        p->id = S.outbox->e[i].id;
        p->listed = S.outbox->e[i].listed;
        if (outbox_read(S.outbox, p->id, &e) == 0 && pda_action_parse(e.json, e.len, &p->info)) {
            S.n_pending++;
        }
    }
}

static void sync_listed(void)
{
    for (int i = 0; i < S.n_pending; i++) {
        const outbox_meta_t *m = outbox_find(S.outbox, S.pending[i].id);
        S.pending[i].listed = m && m->listed;
    }
}

/* ---- life cycle ---- */

static int mkdirs(const char *dir)
{
    char p[APP_DIR_MAX + 1];
    snprintf(p, sizeof(p), "%s", dir);
    for (char *s = p + 1; *s; s++) {
        if (*s == '/') {
            *s = 0;
            if (mkdir(p, 0755) != 0 && errno != EEXIST) {
                return -1;
            }
            *s = '/';
        }
    }
    return mkdir(p, 0700) != 0 && errno != EEXIST ? -1 : 0;
}

int app_init(const char *dir, uint32_t id_seed)
{
    app_deinit();
    if (!dir || strlen(dir) > APP_DIR_MAX) {
        LOG("state dir too long (at most %d bytes): %s\n", APP_DIR_MAX, dir ? dir : "(null)");
        return -1;
    }
    snprintf(S.dir, sizeof(S.dir), "%s", dir);
    if (mkdirs(S.dir) != 0) {
        LOG("can't create %s: %s\n", S.dir, strerror(errno));
        return -1;
    }
    S.outbox = calloc(1, sizeof(*S.outbox));
    S.pending = calloc(OUTBOX_MAX, sizeof(*S.pending));
    if (!S.outbox || !S.pending) {
        return -3;
    }
    char ob[OUTBOX_DIR_MAX];
    snprintf(ob, sizeof(ob), "%s/ob", S.dir);
    if (feed_store_init(S.dir) != 0 || outbox_init(S.outbox, ob, id_seed & 0x7fffffffu) != 0) {
        LOG("storage init failed in %s\n", S.dir);
        return -3;
    }
    if (S.outbox->dropped_corrupt) {
        LOG("outbox: %d corrupt record(s) dropped\n", S.outbox->dropped_corrupt);
    }
    rebuild_pending();
    load_settings();
    S.boot_id = id_seed ^ 0x9e3779b9u;

    char *data;
    size_t len;
    if (feed_store_load_feed(S.dir, &data, &len) == 0) {
        feed_t *f = calloc(1, sizeof(*f));
        if (f && feed_parse(data, len, f) == 0) {
            S.feed = f;
        } else {
            LOG("stored feed doesn't parse; ignored\n");
            free(f);
        }
        free(data);
    }
    return 0;
}

void app_deinit(void)
{
    if (S.feed) {
        feed_free(S.feed);
        free(S.feed);
    }
    free(S.outbox);
    free(S.pending);
    S.feed = NULL;
    S.outbox = NULL;
    S.pending = NULL;
    S.n_pending = 0;
    S.n_failed = 0;
    S.tz_min = 0;
    S.link = false;
    S.last_link_s = 0;
    memset(&S.net, 0, sizeof(S.net));
    memset(&S.tailnet, 0, sizeof(S.tailnet));
    memset(&S.vision, 0, sizeof(S.vision));
    S.qr_scan = false;
    S.last_touch_ms = app_mono_ms() - APP_ACTIVE_MS;
}

const char *app_dir(void)
{
    return S.dir;
}

/* ---- feed, outbox, details ---- */

const feed_t *app_feed(void)
{
    return S.feed;
}

int app_pending(const app_pending_t **out)
{
    *out = S.pending;
    return S.n_pending;
}

int app_failed(const app_failed_t **out)
{
    *out = S.failed;
    return S.n_failed;
}

const app_pending_t *app_pending_item(int64_t item_id)
{
    const app_pending_t *found = NULL;
    for (int i = 0; i < S.n_pending; i++) {
        if (S.pending[i].info.type == PDA_ACT_ITEM_STATUS && S.pending[i].info.item_id == item_id) {
            found = &S.pending[i]; /* the newest wins */
        }
    }
    return found;
}

const app_pending_t *app_pending_todo(const char *note, int32_t i)
{
    const app_pending_t *found = NULL;
    for (int k = 0; k < S.n_pending; k++) {
        const pda_action_info_t *a = &S.pending[k].info;
        if (a->type == PDA_ACT_TODO_TOGGLE && a->item_id == i && strcmp(a->ref, note) == 0) {
            found = &S.pending[k];
        }
    }
    return found;
}

const app_pending_t *app_pending_fetch(const char *ref)
{
    for (int i = 0; i < S.n_pending; i++) {
        if (S.pending[i].info.type == PDA_ACT_FETCH && strcmp(S.pending[i].info.ref, ref) == 0) {
            return &S.pending[i];
        }
    }
    return NULL;
}

const app_failed_t *app_failed_todo(const char *note, int32_t i)
{
    for (int k = 0; k < S.n_failed; k++) {
        const app_failed_t *f = &S.failed[k];
        if (f->type == PDA_ACT_TODO_TOGGLE && f->item_id == i && strcmp(f->what, note) == 0) {
            return f;
        }
    }
    return NULL;
}

const app_failed_t *app_failed_item(int64_t item_id)
{
    for (int k = 0; k < S.n_failed; k++) {
        if (S.failed[k].type == PDA_ACT_ITEM_STATUS && S.failed[k].item_id == item_id) {
            return &S.failed[k];
        }
    }
    return NULL;
}

int app_queue(const char *json, size_t len)
{
    const uint64_t now_ms = app_time_valid() ? (uint64_t)app_now() * 1000u : 0;
    uint32_t id = 0;
    const int rc = outbox_add(S.outbox, json, len, now_ms, &id);
    if (rc != 0) {
        LOG("queue action failed: %d\n", rc);
        return rc;
    }
    rebuild_pending();
    LOG("queued action %08x: %.*s\n", (unsigned)id, (int)len, json);
    emit(APP_EV_OUTBOX, 0);
    return 0;
}

int app_cancel(uint32_t action_id)
{
    const outbox_meta_t *m = outbox_find(S.outbox, action_id);
    if (!m || m->listed) {
        return -1;
    }
    bool was;
    const int rc = outbox_remove(S.outbox, action_id, &was) == 0 ? 0 : -3;
    rebuild_pending();
    if (rc == 0) {
        LOG("action %08x cancelled\n", (unsigned)action_id);
        emit(APP_EV_OUTBOX, 0);
    }
    return rc;
}

int app_outbox_count(void)
{
    return S.outbox ? S.outbox->n : 0;
}

const outbox_t *app_outbox(void)
{
    return S.outbox;
}

detail_t *app_load_detail(const char *ref)
{
    char *data;
    size_t len;
    if (feed_store_load_detail(S.dir, ref, &data, &len) != 0) {
        return NULL;
    }
    detail_t *d = calloc(1, sizeof(*d));
    if (d && detail_parse(data, len, d) != 0) {
        free(d);
        d = NULL;
    }
    free(data);
    if (d && strcmp(d->ref, ref) != 0) { /* a cache-key collision */
        detail_free(d);
        free(d);
        d = NULL;
    }
    return d;
}

int app_commit_blob(uint8_t kind, uint32_t blob_id, const uint8_t *data, size_t len)
{
    if (kind == PDA_KIND_FEED) {
        feed_t *nf = calloc(1, sizeof(*nf));
        if (!nf) {
            return PDA_E_IO;
        }
        const int prc = feed_parse((const char *)data, len, nf);
        if (prc != 0) {
            LOG("feed %u rejected: parse error %d\n", (unsigned)blob_id, prc);
            free(nf);
            return PDA_E_BAD_STATE;
        }
        if (feed_store_put_feed(S.dir, data, len) != 0) {
            feed_free(nf);
            free(nf);
            return PDA_E_IO;
        }
        if (S.feed) {
            feed_free(S.feed);
            free(S.feed);
        }
        S.feed = nf;
        S.n_failed = 0; /* the new feed shows the server's truth */
        LOG("feed %u stored: %d conversations, %d events, %d checklists, %u bytes\n",
            (unsigned)nf->version, nf->n_conv, nf->n_events, nf->n_todos, (unsigned)len);
        emit(APP_EV_FEED, 0);
        return 0;
    }
    char ref[PDA_REF_MAX];
    if (!detail_peek_ref((const char *)data, len, ref, sizeof(ref))) {
        LOG("detail %u rejected: no ref / not JSON\n", (unsigned)blob_id);
        return PDA_E_BAD_STATE;
    }
    if (feed_store_put_detail(S.dir, ref, data, len) != 0) {
        return PDA_E_IO;
    }
    LOG("detail %s stored (%u bytes)\n", ref, (unsigned)len);
    emit(APP_EV_DETAIL, (int32_t)crc32_update(0, ref, strlen(ref)));
    return 0;
}

int app_wipe(void)
{
    int rc = feed_store_wipe(S.dir);
    rc |= outbox_clear(S.outbox);
    if (S.feed) {
        feed_free(S.feed);
        free(S.feed);
        S.feed = NULL;
    }
    S.n_failed = 0;
    rebuild_pending();
    feed_store_init(S.dir);
    LOG("cache wiped (feed, details, outbox)\n");
    emit(APP_EV_FEED, 0);
    emit(APP_EV_OUTBOX, 0);
    return rc == 0 ? 0 : -3;
}

/* ---- the store pda_proto drives ---- */

static int st_time(void *ctx, int64_t epoch_ms, int16_t tz_min)
{
    (void)ctx;
    return app_set_time(epoch_ms, tz_min);
}

static int st_commit(void *ctx, uint8_t kind, uint32_t blob_id, const uint8_t *data, size_t len)
{
    (void)ctx;
    return app_commit_blob(kind, blob_id, data, len);
}

static size_t st_list(void *ctx, uint32_t after, pda_ob_entry_t *out, size_t max, uint32_t *rem)
{
    (void)ctx;
    const size_t n = outbox_list(S.outbox, after, out, max, rem);
    sync_listed();
    return n;
}

/* The words a toast names for a refused action: its "text" or "title". */
static void action_label(uint32_t id, char *out, size_t cap)
{
    out[0] = 0;
    pda_ob_entry_t e;
    if (outbox_read(S.outbox, id, &e) != 0) {
        return;
    }
    json_tok_t t[32];
    const int nt = json_parse(e.json, e.len, t, 32);
    if (nt < 1) {
        return;
    }
    const json_doc_t d = {e.json, t, nt};
    int v = json_get(&d, 0, "text");
    if (v < 0) {
        v = json_get(&d, 0, "title");
    }
    if (v >= 0) {
        json_str(&d, v, out, cap);
    }
}

static int st_ack(void *ctx, uint32_t action_id, uint8_t result, bool *was_queued)
{
    (void)ctx;
    int failed_idx = -1;
    if (result != 0) {
        for (int i = 0; i < S.n_pending; i++) {
            const app_pending_t *p = &S.pending[i];
            if (p->id != action_id) {
                continue;
            }
            if (S.n_failed == APP_FAILED_MAX) {
                memmove(&S.failed[0], &S.failed[1], sizeof(S.failed[0]) * (APP_FAILED_MAX - 1));
                S.n_failed--;
            }
            app_failed_t *f = &S.failed[S.n_failed];
            f->type = p->info.type;
            f->item_id = p->info.item_id;
            snprintf(f->what, sizeof(f->what), "%s",
                     p->info.type == PDA_ACT_CAL_ADD ? p->info.title : p->info.ref);
            action_label(action_id, f->label, sizeof(f->label));
            if (!f->label[0]) {
                const size_t n = strnlen(f->what, sizeof(f->label) - 1);
                memcpy(f->label, f->what, n);
                f->label[n] = 0;
            }
            failed_idx = S.n_failed++;
        }
    }
    const int rc = outbox_remove(S.outbox, action_id, was_queued);
    rebuild_pending();
    if (rc != 0) {
        return PDA_E_IO;
    }
    LOG("action %08x acked: %s%s\n", (unsigned)action_id, result ? "rejected" : "done",
        *was_queued ? "" : " (was not queued)");
    emit(APP_EV_OUTBOX, result ? 1 : 0);
    if (failed_idx >= 0) {
        emit(APP_EV_FAILED, failed_idx);
    }
    return 0;
}

static const pda_store_t STORE = {st_time, st_commit, st_list, st_ack};

const pda_store_t *app_store(void)
{
    return &STORE;
}

/* ---- checklist items ---- */

void app_todo_view(int note_idx, int item_idx, app_todo_view_t *out)
{
    *out = (app_todo_view_t){0};
    const feed_t *f = S.feed;
    if (!f || note_idx < 0 || note_idx >= f->n_todos || item_idx < 0 ||
        item_idx >= f->todos[note_idx].n_items) {
        return;
    }
    const feed_todo_t *n = &f->todos[note_idx];
    const feed_todo_item_t *it = &n->items[item_idx];
    const app_pending_t *p = app_pending_todo(n->id, it->i);
    out->done = p ? p->info.done : it->done;
    out->pending = p != NULL;
    out->failed = app_failed_todo(n->id, it->i) != NULL;
}

app_todo_result_t app_todo_tap(int note_idx, int item_idx)
{
    const feed_t *f = S.feed;
    if (!f || note_idx < 0 || note_idx >= f->n_todos || item_idx < 0 ||
        item_idx >= f->todos[note_idx].n_items) {
        return APP_TODO_ERROR;
    }
    const feed_todo_t *note = &f->todos[note_idx];
    const feed_todo_item_t *it = &note->items[item_idx];
    const app_pending_t *p = app_pending_todo(note->id, it->i);
    if (p && !p->listed && app_cancel(p->id) == 0) {
        return APP_TODO_UNDONE;
    }
    const bool want = p ? !p->info.done : !it->done;
    char json[PDA_ACTION_BUF];
    const size_t n = pda_action_todo_toggle(json, note->id, it->i, it->text, want);
    const bool reverse = p != NULL;
    const int rc = n ? app_queue(json, n) : -2;
    if (rc == -1) {
        return APP_TODO_FULL;
    }
    if (rc != 0) {
        return APP_TODO_ERROR;
    }
    return reverse ? APP_TODO_REVERSE : APP_TODO_QUEUED;
}

/* ---- clock ---- */

int64_t app_now(void)
{
    return S.clock_override ? S.clock_override : (int64_t)time(NULL);
}

bool app_time_valid(void)
{
    return app_now() >= PDA_TIME_MIN_MS / 1000;
}

int app_tz_min(void)
{
    return S.tz_min;
}

int32_t app_today(void)
{
    return pda_local_day(app_now(), S.tz_min);
}

int app_set_time(int64_t epoch_ms, int16_t tz_min)
{
    (void)epoch_ms; /* NTP owns the system clock; pda_proto already range-checked it */
    if (tz_min != S.tz_min) {
        S.tz_min = tz_min;
        if (save_settings() != 0) {
            LOG("ui.json: can't save the display offset\n");
        }
    }
    emit(APP_EV_CLOCK, 0);
    return 0;
}

void app_set_clock_override(int64_t epoch_s)
{
    S.clock_override = epoch_s;
    emit(APP_EV_CLOCK, 0);
}

int64_t app_mono_ms(void)
{
    if (S.mono_override) {
        return S.mono_override;
    }
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

void app_set_mono_override(int64_t ms)
{
    S.mono_override = ms;
}

/* ---- status ---- */

void app_touch_activity(void)
{
    S.last_touch_ms = app_mono_ms();
}

bool app_user_active(void)
{
    return app_mono_ms() - S.last_touch_ms < APP_ACTIVE_MS;
}

void app_set_power(int8_t battery_pct, bool charging, bool usb)
{
    if (battery_pct == S.battery && charging == S.charging && usb == S.usb) {
        return;
    }
    S.battery = battery_pct;
    S.charging = charging;
    S.usb = usb;
    emit(APP_EV_POWER, 0);
}

int8_t app_battery_pct(void)
{
    return S.battery;
}

bool app_charging(void)
{
    return S.charging;
}

uint32_t app_boot_id(void)
{
    return S.boot_id;
}

void app_status(pda_status_t *out)
{
    *out = (pda_status_t){0};
    out->flags = (uint8_t)((app_time_valid() ? PDA_STATUS_TIME_VALID : 0) |
                           (S.charging ? PDA_STATUS_CHARGING : 0) | (S.usb ? PDA_STATUS_USB : 0) |
                           (app_user_active() ? PDA_STATUS_ACTIVE : 0)); /* bit3 never */
    out->battery_pct = S.battery;
    out->outbox = (uint16_t)app_outbox_count();
    out->feed_version = S.feed ? S.feed->version : 0;
    out->boot_id = S.boot_id;
}

/* ---- syncd ---- */

void app_set_link(bool up)
{
    if (up == S.link) {
        return;
    }
    S.link = up;
    S.last_link_s = app_now();
    if (!up) {
        memset(&S.net, 0, sizeof(S.net)); /* unknown until syncd says again */
    }
    emit(APP_EV_LINK, up);
}

bool app_link_up(void)
{
    return S.link;
}

int64_t app_last_link_s(void)
{
    return S.link ? app_now() : S.last_link_s;
}

const app_net_t *app_net(void)
{
    return &S.net;
}

void app_set_net(const app_net_t *n)
{
    S.net = *n;
    emit(APP_EV_NET, n->state);
}

const app_tailnet_t *app_tailnet(void)
{
    return &S.tailnet;
}

void app_set_tailnet(const app_tailnet_t *t)
{
    S.tailnet = *t;
    emit(APP_EV_TAILNET, t->state);
}

void app_set_pair_result(bool ok, const char *msg)
{
    S.pair_ok = ok;
    snprintf(S.pair_msg, sizeof(S.pair_msg), "%s", msg ? msg : "");
    emit(APP_EV_PAIR_RESULT, ok);
}

bool app_pair_result(const char **msg)
{
    if (msg) {
        *msg = S.pair_msg;
    }
    return S.pair_ok;
}

/* ---- vision ---- */

const app_vision_t *app_vision(void)
{
    return &S.vision;
}

void app_set_vision(const app_vision_t *v)
{
    S.vision = *v;
    emit(APP_EV_VISION, v->camera);
}

void app_set_qr_scan(bool on)
{
    if (on != S.qr_scan) {
        S.qr_scan = on;
        emit(APP_EV_SETTINGS, 0); /* the vision mode follows settings */
    }
}

void app_vision_mode(bool *gestures, bool *presence, bool *qr)
{
    *gestures = S.settings.hands_free;
    *presence = S.settings.presence_wake;
    *qr = S.qr_scan;
}

/* ---- settings ---- */

const app_settings_t *app_settings(void)
{
    return &S.settings;
}

int app_set_settings(const app_settings_t *s)
{
    S.settings = *s;
    S.settings.brightness = s->brightness < 5 ? 5 : s->brightness > 100 ? 100 : s->brightness;
    const int rc = S.dir[0] ? save_settings() : -3;
    emit(APP_EV_SETTINGS, 0);
    return rc;
}
