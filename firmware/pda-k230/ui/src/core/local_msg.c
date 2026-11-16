#include "local_msg.h"

#include <stdio.h>
#include <string.h>

#include "json.h"

#define TOKS 64
#define LOCAL_EPOCH_MAX 10000000000LL /* the year 2286 */

static void str_or_empty(const json_doc_t *d, int obj, const char *key, char *out, size_t cap)
{
    out[0] = 0;
    const int v = json_get(d, obj, key);
    if (v >= 0 && json_str(d, v, out, cap) < 0) {
        out[0] = 0; /* null or not a string */
    }
}

bool local_parse(const char *json, size_t len, local_in_t *out)
{
    memset(out, 0, sizeof(*out));
    json_tok_t t[TOKS];
    const int n = json_parse(json, len, t, TOKS);
    if (n < 1 || t[0].type != JSON_OBJ) {
        return false;
    }
    const json_doc_t d = {json, t, n};
    const int tt = json_get(&d, 0, "t");
    if (tt < 0 || t[tt].type != JSON_STR) {
        return false;
    }
    if (json_str_eq(&d, tt, "net")) {
        out->type = LOCAL_NET;
        static const char *const NAMES[] = {"unpaired", "offline", "syncing", "ok", "auth_failed"};
        const int s = json_get(&d, 0, "state");
        out->net.state = APP_NET_UNKNOWN;
        for (int i = 0; i < 5; i++) {
            if (s >= 0 && json_str_eq(&d, s, NAMES[i])) {
                out->net.state = (app_net_state_t)(APP_NET_UNPAIRED + i);
            }
        }
        str_or_empty(&d, 0, "hub", out->net.hub, sizeof(out->net.hub));
        str_or_empty(&d, 0, "msg", out->net.msg, sizeof(out->net.msg));
        int64_t last = 0; /* integral epoch seconds; anything else (1e300, -1, 1.5) is "never" */
        if (json_int(&d, json_get(&d, 0, "last_ok"), &last) && last > 0 && last < LOCAL_EPOCH_MAX) {
            out->net.last_ok = last;
        }
    } else if (json_str_eq(&d, tt, "tailnet")) {
        out->type = LOCAL_TAILNET;
        static const char *const NAMES[] = {"stopped", "needs_login", "running"};
        const int s = json_get(&d, 0, "state");
        out->tailnet.state = APP_TAILNET_UNKNOWN;
        for (int i = 0; i < 3; i++) {
            if (s >= 0 && json_str_eq(&d, s, NAMES[i])) {
                out->tailnet.state = (app_tailnet_state_t)(APP_TAILNET_STOPPED + i);
            }
        }
        str_or_empty(&d, 0, "url", out->tailnet.url, sizeof(out->tailnet.url));
        str_or_empty(&d, 0, "name", out->tailnet.name, sizeof(out->tailnet.name));
    } else if (json_str_eq(&d, tt, "tz")) {
        out->type = LOCAL_TZ;
        int64_t day = 0;
        const int arr = json_get(&d, 0, "offsets");
        if (json_int(&d, json_get(&d, 0, "day"), &day) && day > 0 && day < 1000000 && arr >= 0 &&
            t[arr].type == JSON_ARR) {
            out->tz.day = (int32_t)day;
            for (int i = json_first(&d, arr); i >= 0 && out->tz.n < APP_TZ_DAYS; i = json_next(&d, arr, i)) {
                int64_t m;
                if (!json_int(&d, i, &m) || m < -720 || m > 840) {
                    out->tz.n = 0; /* all or nothing */
                    break;
                }
                out->tz.min[out->tz.n++] = (int16_t)m;
            }
        }
    } else if (json_str_eq(&d, tt, "pair_result")) {
        out->type = LOCAL_PAIR_RESULT;
        json_bool(&d, json_get(&d, 0, "ok"), &out->ok);
        str_or_empty(&d, 0, "msg", out->msg, sizeof(out->msg));
    } else {
        out->type = LOCAL_UNKNOWN;
    }
    return true;
}

void local_apply(const local_in_t *m)
{
    switch (m->type) {
    case LOCAL_NET: app_set_net(&m->net); break;
    case LOCAL_TAILNET: app_set_tailnet(&m->tailnet); break;
    case LOCAL_PAIR_RESULT: app_set_pair_result(m->ok, m->msg); break;
    case LOCAL_TZ: app_set_day_offsets(&m->tz); break;
    case LOCAL_UNKNOWN: break;
    }
}

static size_t fits(int n, size_t cap)
{
    return n > 0 && (size_t)n < cap ? (size_t)n : 0;
}

size_t local_sync_now(char *out, size_t cap)
{
    return fits(snprintf(out, cap, "{\"t\":\"sync_now\"}"), cap);
}

size_t local_unpair(char *out, size_t cap)
{
    return fits(snprintf(out, cap, "{\"t\":\"unpair\"}"), cap);
}

/* Bytes json_quote() writes for all of s, quotes included. */
static size_t quoted_len(const char *s)
{
    size_t n = 2;
    for (; *s; s++) {
        const unsigned char c = (unsigned char)*s;
        n += c == '"' || c == '\\' ? 2 : c < 0x20 ? 6 : 1;
    }
    return n;
}

size_t local_pair(char *out, size_t cap, const char *url, const char *code)
{
    /* {"t":"pair","url":<url>,"code":<code>} with nothing cut: a cut URL or code is wrong */
    static const char head[] = "{\"t\":\"pair\",\"url\":";
    static const char mid[] = ",\"code\":";
    const size_t need = sizeof(head) - 1 + quoted_len(url) + sizeof(mid) - 1 + quoted_len(code) + 1;
    if (need + 1 > cap) {
        return 0;
    }
    size_t n = sizeof(head) - 1;
    memcpy(out, head, n);
    n += json_quote(out + n, cap - n, url);
    memcpy(out + n, mid, sizeof(mid) - 1);
    n += sizeof(mid) - 1;
    n += json_quote(out + n, cap - n, code);
    out[n++] = '}';
    out[n] = 0;
    return n;
}
