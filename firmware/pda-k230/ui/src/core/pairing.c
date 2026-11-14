#include "pairing.h"

#include <ctype.h>
#include <stdio.h>
#include <string.h>

#include "local_msg.h"

#define MAKE_NEW "In Odysseus open Settings \xe2\x86\x92 Integrations \xe2\x86\x92 Pair a phone."

static void set_msg(pair_flow_t *p, bool hint, const char *m)
{
    snprintf(p->msg, sizeof(p->msg), "%s", m ? m : "");
    p->hint = hint;
}

void pair_reset(pair_flow_t *p)
{
    *p = (pair_flow_t){.stage = PAIR_CHOOSE};
}

void pair_start_scan(pair_flow_t *p)
{
    if (p->stage == PAIR_WAITING) {
        return;
    }
    p->stage = PAIR_SCANNING;
    set_msg(p, false, "");
}

void pair_start_typing(pair_flow_t *p)
{
    if (p->stage == PAIR_WAITING) {
        return;
    }
    p->stage = PAIR_TYPING;
    set_msg(p, false, "");
}

bool pair_back(pair_flow_t *p)
{
    if (p->stage == PAIR_WAITING) {
        return false;
    }
    if (p->stage == PAIR_CONFIRM) {
        const pair_stage_t from = p->from;
        pair_reset(p);
        p->stage = from == PAIR_TYPING ? PAIR_TYPING : PAIR_SCANNING;
        return true;
    }
    pair_reset(p);
    return true;
}

bool pair_wants_camera(const pair_flow_t *p)
{
    return p->stage == PAIR_SCANNING;
}

/* the text without leading / trailing white space, into buf */
static const char *trimmed(const char *s, char *buf, size_t cap)
{
    while (s && isspace((unsigned char)*s)) {
        s++;
    }
    size_t n = s ? strlen(s) : 0;
    while (n > 0 && isspace((unsigned char)s[n - 1])) {
        n--;
    }
    if (n >= cap) {
        n = cap - 1;
    }
    if (n) {
        memcpy(buf, s, n);
    }
    buf[n] = 0;
    return buf;
}

bool pair_url_host(const char *url, char *host, size_t cap)
{
    const char *s = strstr(url, "://");
    if (!s || cap == 0) {
        return false;
    }
    s += 3;
    size_t n;
    if (*s == '[') {
        s++;
        const char *e = strchr(s, ']');
        if (!e) {
            return false;
        }
        n = (size_t)(e - s);
    } else {
        n = strcspn(s, "/:?#");
    }
    if (n == 0) {
        return false;
    }
    if (n >= cap) {
        n = cap - 1;
        while (n > 0 && ((unsigned char)s[n] & 0xC0) == 0x80) {
            n--; /* cut before a character, not inside one */
        }
    }
    for (size_t i = 0; i < n; i++) {
        const unsigned char c = (unsigned char)s[i];
        host[i] = c < 0x20 || c == 0x7f ? '?' : (char)c;
    }
    host[n] = 0;
    return true;
}

static int hexval(char c)
{
    return c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 : c >= 'A' && c <= 'F' ? c - 'A' + 10 : -1;
}

/* The `url` field of odysseus-pair:v1?…, percent-decoded (as syncd's unquote_plus). */
static bool link_url(const char *link, char *url, size_t cap)
{
    static const char V1[] = PAIR_PREFIX "v1?";
    if (strncmp(link, V1, sizeof(V1) - 1) != 0) {
        return false;
    }
    for (const char *f = link + sizeof(V1) - 1; *f;) {
        const size_t len = strcspn(f, "&");
        if (len > 4 && strncmp(f, "url=", 4) == 0) {
            size_t w = 0;
            for (size_t i = 4; i < len; i++) {
                char c = f[i];
                if (c == '+') {
                    c = ' ';
                } else if (c == '%') {
                    const int h = i + 2 < len ? hexval(f[i + 1]) : -1, l = h >= 0 ? hexval(f[i + 2]) : -1;
                    if (h < 0 || l < 0) {
                        return false;
                    }
                    c = (char)(h * 16 + l);
                    i += 2;
                }
                if (c == 0 || w + 1 >= cap) {
                    return false;
                }
                url[w++] = c;
            }
            url[w] = 0;
            return true; /* the first url= wins, as in syncd */
        }
        f += len;
        if (*f == '&') {
            f++;
        }
    }
    return false;
}

static pair_step_t ask(pair_flow_t *p, const char *url, const char *json, size_t n)
{
    const pair_stage_t from = p->stage;
    if (!pair_url_host(url, p->host, sizeof(p->host))) {
        set_msg(p, true, "The pairing link is damaged. " MAKE_NEW);
        return PAIR_REJECTED;
    }
    memcpy(p->pending, json, n);
    p->pending[n] = 0;
    p->pending_len = n;
    p->from = from;
    p->stage = PAIR_CONFIRM;
    set_msg(p, false, "");
    return PAIR_ASK;
}

pair_step_t pair_on_qr(pair_flow_t *p, const char *text, char *out, size_t cap, size_t *out_len)
{
    *out_len = 0;
    if (cap) {
        out[0] = 0;
    }
    if (p->stage != PAIR_SCANNING) {
        return PAIR_IGNORED;
    }
    char t[PAIR_LOCAL_MAX];
    trimmed(text, t, sizeof(t));
    if (strncmp(t, PAIR_PREFIX, strlen(PAIR_PREFIX)) != 0) {
        set_msg(p, true, "That QR code isn't an Odysseus pairing code. " MAKE_NEW);
        return PAIR_REJECTED;
    }
    char url[320];
    if (!link_url(t, url, sizeof(url))) {
        set_msg(p, true, "The pairing link is damaged. " MAKE_NEW);
        return PAIR_REJECTED;
    }
    const char *e = pair_check_url(url);
    if (e) {
        set_msg(p, true, e);
        return PAIR_REJECTED;
    }
    char json[PAIR_LOCAL_MAX];
    const size_t n = local_pair(json, sizeof(json), t, "");
    if (n == 0) {
        set_msg(p, true, "That pairing code is too long to send.");
        return PAIR_REJECTED;
    }
    return ask(p, url, json, n);
}

pair_step_t pair_confirm(pair_flow_t *p, char *out, size_t cap, size_t *out_len)
{
    *out_len = 0;
    if (p->stage != PAIR_CONFIRM || p->pending_len == 0 || p->pending_len >= cap) {
        return PAIR_IGNORED;
    }
    memcpy(out, p->pending, p->pending_len + 1);
    *out_len = p->pending_len;
    return PAIR_SEND;
}

const char *pair_check_url(const char *url)
{
    char u[320];
    trimmed(url, u, sizeof(u));
    const char *host;
    if (strncmp(u, "http://", 7) == 0) {
        host = u + 7;
    } else if (strncmp(u, "https://", 8) == 0) {
        host = u + 8;
    } else {
        return "The address must start with http:// or https://";
    }
    if (!*host || *host == '/' || *host == ':') {
        return "The address needs a host name after //";
    }
    for (const char *s = u; *s; s++) {
        if (*s == '@' || *s == '?' || *s == '#') {
            return "The address can't have a user, ?query or #part";
        }
        if (isspace((unsigned char)*s)) {
            return "The address can't contain spaces";
        }
    }
    if (strlen(u) > 300) {
        return "The address is too long";
    }
    return NULL;
}

const char *pair_check_code(const char *code)
{
    char c[128];
    trimmed(code, c, sizeof(c));
    const size_t n = strlen(c);
    if (n < 8 || n > 100) {
        return "A pairing code has 8 to 100 characters";
    }
    for (const char *s = c; *s; s++) {
        if (!isalnum((unsigned char)*s) && *s != '_' && *s != '-') {
            return "A pairing code has only letters, digits, _ and -";
        }
    }
    return NULL;
}

pair_step_t pair_submit(pair_flow_t *p, const char *url, const char *code, char *out, size_t cap,
                        size_t *out_len)
{
    *out_len = 0;
    if (cap) {
        out[0] = 0;
    }
    if (p->stage != PAIR_TYPING) {
        return PAIR_IGNORED;
    }
    const char *e = pair_check_url(url);
    if (!e) {
        e = pair_check_code(code);
    }
    if (e) {
        set_msg(p, true, e);
        return PAIR_REJECTED;
    }
    char u[320], c[128];
    trimmed(url, u, sizeof(u));
    trimmed(code, c, sizeof(c));
    size_t un = strlen(u);
    while (un > 0 && u[un - 1] == '/') {
        u[--un] = 0;
    }
    char json[PAIR_LOCAL_MAX];
    const size_t n = local_pair(json, sizeof(json), u, c);
    if (n == 0) {
        set_msg(p, true, "The address is too long to send.");
        return PAIR_REJECTED;
    }
    return ask(p, u, json, n);
}

void pair_sent(pair_flow_t *p, bool ok, int64_t now_ms)
{
    if (ok) {
        p->stage = PAIR_WAITING;
        p->sent_ms = now_ms;
        set_msg(p, false, "");
    } else {
        p->stage = PAIR_FAILED;
        set_msg(p, false, "The sync service isn't running, so the code couldn't be sent. Try again in a moment.");
    }
}

bool pair_on_result(pair_flow_t *p, bool ok, const char *msg)
{
    if (p->stage != PAIR_WAITING) {
        return false;
    }
    p->stage = ok ? PAIR_OK : PAIR_FAILED;
    set_msg(p, false, msg && msg[0] ? msg : ok ? "Paired." : "Pairing failed. " MAKE_NEW);
    return true;
}

bool pair_tick(pair_flow_t *p, int64_t now_ms)
{
    if (p->stage == PAIR_WAITING && now_ms - p->sent_ms >= PAIR_WAIT_MS) {
        p->stage = PAIR_FAILED;
        set_msg(p, false, "No answer from the sync service. Check Status and try again.");
        return true;
    }
    return false;
}
