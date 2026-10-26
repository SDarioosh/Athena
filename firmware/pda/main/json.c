#include "json.h"

#include <stdlib.h>
#include <string.h>

typedef struct {
    const char *js;
    size_t len, pos;
    json_tok_t *t;
    int max, n;
} parser_t;

static void skip_ws(parser_t *p)
{
    while (p->pos < p->len) {
        const char c = p->js[p->pos];
        if (c != ' ' && c != '\t' && c != '\n' && c != '\r') {
            break;
        }
        p->pos++;
    }
}

static int new_tok(parser_t *p, json_type_t type, size_t start)
{
    if (p->n >= p->max) {
        return JSON_E_NOMEM;
    }
    const int i = p->n++;
    p->t[i] = (json_tok_t){.type = (uint8_t)type, .start = (uint32_t)start, .end = (uint32_t)start};
    return i;
}

static int hexval(char c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

static int parse_string(parser_t *p)
{
    /* p->pos is at the opening quote */
    const int i = new_tok(p, JSON_STR, p->pos + 1);
    if (i < 0) {
        return i;
    }
    p->pos++;
    while (p->pos < p->len) {
        const unsigned char c = (unsigned char)p->js[p->pos];
        if (c == '"') {
            p->t[i].end = (uint32_t)p->pos;
            p->t[i].next = (uint32_t)p->n;
            p->pos++;
            return i;
        }
        if (c < 0x20) {
            return JSON_E_SYNTAX;
        }
        if (c == '\\') {
            if (p->pos + 1 >= p->len) {
                return JSON_E_SYNTAX;
            }
            const char e = p->js[p->pos + 1];
            if (e == 'u') {
                if (p->pos + 6 > p->len) {
                    return JSON_E_SYNTAX;
                }
                for (int k = 2; k < 6; k++) {
                    if (hexval(p->js[p->pos + k]) < 0) {
                        return JSON_E_SYNTAX;
                    }
                }
                p->pos += 6;
                continue;
            }
            if (!strchr("\"\\/bfnrt", e)) {
                return JSON_E_SYNTAX;
            }
            p->pos += 2;
            continue;
        }
        p->pos++;
    }
    return JSON_E_SYNTAX;
}

static bool is_digit(char c)
{
    return c >= '0' && c <= '9';
}

static int parse_number(parser_t *p)
{
    const size_t s = p->pos;
    size_t q = p->pos;
    if (q < p->len && p->js[q] == '-') {
        q++;
    }
    if (q >= p->len || !is_digit(p->js[q])) {
        return JSON_E_SYNTAX;
    }
    if (p->js[q] == '0') {
        q++;
    } else {
        while (q < p->len && is_digit(p->js[q])) {
            q++;
        }
    }
    if (q < p->len && p->js[q] == '.') {
        q++;
        if (q >= p->len || !is_digit(p->js[q])) {
            return JSON_E_SYNTAX;
        }
        while (q < p->len && is_digit(p->js[q])) {
            q++;
        }
    }
    if (q < p->len && (p->js[q] == 'e' || p->js[q] == 'E')) {
        q++;
        if (q < p->len && (p->js[q] == '+' || p->js[q] == '-')) {
            q++;
        }
        if (q >= p->len || !is_digit(p->js[q])) {
            return JSON_E_SYNTAX;
        }
        while (q < p->len && is_digit(p->js[q])) {
            q++;
        }
    }
    const int i = new_tok(p, JSON_NUM, s);
    if (i < 0) {
        return i;
    }
    p->t[i].end = (uint32_t)q;
    p->t[i].next = (uint32_t)p->n;
    p->pos = q;
    return i;
}

static int parse_literal(parser_t *p, const char *word, json_type_t type)
{
    const size_t n = strlen(word);
    if (p->pos + n > p->len || memcmp(p->js + p->pos, word, n) != 0) {
        return JSON_E_SYNTAX;
    }
    const int i = new_tok(p, type, p->pos);
    if (i < 0) {
        return i;
    }
    p->pos += n;
    p->t[i].end = (uint32_t)p->pos;
    p->t[i].next = (uint32_t)p->n;
    return i;
}

static int parse_value(parser_t *p, int depth);

static int parse_container(parser_t *p, int depth, bool obj)
{
    if (depth >= JSON_MAX_DEPTH) {
        return JSON_E_DEPTH;
    }
    const int i = new_tok(p, obj ? JSON_OBJ : JSON_ARR, p->pos);
    if (i < 0) {
        return i;
    }
    const char close = obj ? '}' : ']';
    p->pos++;
    skip_ws(p);
    if (p->pos < p->len && p->js[p->pos] == close) {
        p->pos++;
    } else {
        for (;;) {
            skip_ws(p);
            if (obj) {
                if (p->pos >= p->len || p->js[p->pos] != '"') {
                    return JSON_E_SYNTAX;
                }
                const int k = parse_string(p);
                if (k < 0) {
                    return k;
                }
                skip_ws(p);
                if (p->pos >= p->len || p->js[p->pos] != ':') {
                    return JSON_E_SYNTAX;
                }
                p->pos++;
            }
            const int v = parse_value(p, depth + 1);
            if (v < 0) {
                return v;
            }
            p->t[i].size++;
            skip_ws(p);
            if (p->pos >= p->len) {
                return JSON_E_SYNTAX;
            }
            if (p->js[p->pos] == ',') {
                p->pos++;
                continue;
            }
            if (p->js[p->pos] == close) {
                p->pos++;
                break;
            }
            return JSON_E_SYNTAX;
        }
    }
    p->t[i].end = (uint32_t)p->pos;
    p->t[i].next = (uint32_t)p->n;
    return i;
}

static int parse_value(parser_t *p, int depth)
{
    skip_ws(p);
    if (p->pos >= p->len) {
        return JSON_E_SYNTAX;
    }
    switch (p->js[p->pos]) {
    case '{': return parse_container(p, depth, true);
    case '[': return parse_container(p, depth, false);
    case '"': return parse_string(p);
    case 't': return parse_literal(p, "true", JSON_TRUE);
    case 'f': return parse_literal(p, "false", JSON_FALSE);
    case 'n': return parse_literal(p, "null", JSON_NULL);
    default: return parse_number(p);
    }
}

int json_parse(const char *js, size_t len, json_tok_t *toks, int max)
{
    parser_t p = {.js = js, .len = len, .t = toks, .max = max};
    const int rc = parse_value(&p, 0);
    if (rc < 0) {
        return rc;
    }
    skip_ws(&p);
    return p.pos == len ? p.n : JSON_E_SYNTAX;
}

/* ---- access ---- */

int json_first(const json_doc_t *d, int c)
{
    if (c < 0 || c >= d->n || (d->t[c].type != JSON_OBJ && d->t[c].type != JSON_ARR) ||
        d->t[c].size == 0) {
        return -1;
    }
    return c + 1;
}

int json_next(const json_doc_t *d, int c, int i)
{
    if (i < 0) {
        return -1;
    }
    /* objects: i is a key, its value is i + 1 */
    const int next = d->t[c].type == JSON_OBJ ? (int)d->t[i + 1].next : (int)d->t[i].next;
    return next < (int)d->t[c].next ? next : -1;
}

/* Decode one escape at s (after the backslash); returns the code point, *used = chars eaten. */
static uint32_t unescape_one(const char *s, size_t avail, size_t *used)
{
    const char e = s[0];
    *used = 1;
    switch (e) {
    case 'b': return '\b';
    case 'f': return '\f';
    case 'n': return '\n';
    case 'r': return '\r';
    case 't': return '\t';
    case 'u': {
        uint32_t cp = 0;
        for (int k = 1; k <= 4; k++) {
            cp = (cp << 4) | (uint32_t)hexval(s[k]);
        }
        *used = 5;
        /* surrogate pair */
        if (cp >= 0xD800 && cp < 0xDC00 && avail >= 11 && s[5] == '\\' && s[6] == 'u') {
            uint32_t lo = 0;
            bool ok = true;
            for (int k = 7; k <= 10; k++) {
                const int h = hexval(s[k]);
                ok &= h >= 0;
                lo = (lo << 4) | (uint32_t)(h < 0 ? 0 : h);
            }
            if (ok && lo >= 0xDC00 && lo < 0xE000) {
                *used = 11;
                return 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
            }
        }
        if (cp >= 0xD800 && cp < 0xE000) {
            return 0xFFFD;
        }
        return cp;
    }
    default: return (unsigned char)e; /* " \ / */
    }
}

static size_t utf8_put(uint32_t cp, char *o)
{
    if (cp < 0x80) {
        o[0] = (char)cp;
        return 1;
    }
    if (cp < 0x800) {
        o[0] = (char)(0xC0 | (cp >> 6));
        o[1] = (char)(0x80 | (cp & 0x3F));
        return 2;
    }
    if (cp < 0x10000) {
        o[0] = (char)(0xE0 | (cp >> 12));
        o[1] = (char)(0x80 | ((cp >> 6) & 0x3F));
        o[2] = (char)(0x80 | (cp & 0x3F));
        return 3;
    }
    o[0] = (char)(0xF0 | (cp >> 18));
    o[1] = (char)(0x80 | ((cp >> 12) & 0x3F));
    o[2] = (char)(0x80 | ((cp >> 6) & 0x3F));
    o[3] = (char)(0x80 | (cp & 0x3F));
    return 4;
}

/* Walk a string token; writes to out if non-NULL (cap includes the NUL). */
static int str_walk(const json_doc_t *d, int i, char *out, size_t cap)
{
    if (i < 0 || i >= d->n || d->t[i].type != JSON_STR) {
        return -1;
    }
    const char *s = d->js + d->t[i].start;
    const size_t n = d->t[i].end - d->t[i].start;
    size_t w = 0;
    for (size_t k = 0; k < n;) {
        char buf[4];
        size_t blen;
        if (s[k] == '\\') {
            size_t used;
            blen = utf8_put(unescape_one(s + k + 1, n - k - 1, &used), buf);
            k += 1 + used;
        } else {
            /* copy one whole UTF-8 sequence, so a cut never splits it; a broken one (a stray
             * or missing continuation byte, or not a lead byte) becomes U+FFFD */
            const unsigned char c = (unsigned char)s[k];
            blen = c < 0x80 ? 1 : c < 0xC0 ? 0 : c < 0xE0 ? 2 : c < 0xF0 ? 3 : c < 0xF8 ? 4 : 0;
            size_t j = 1;
            while (j < blen && k + j < n && ((unsigned char)s[k + j] & 0xC0) == 0x80) {
                j++;
            }
            if (blen > 0 && j == blen) {
                memcpy(buf, s + k, blen);
                k += blen;
            } else {
                blen = utf8_put(0xFFFD, buf);
                k += j; /* the lead byte and the continuation bytes it had */
            }
        }
        if (out) {
            if (w + blen + 1 > cap) {
                break;
            }
            memcpy(out + w, buf, blen);
        }
        w += blen;
    }
    if (out && cap > 0) {
        out[w < cap ? w : cap - 1] = 0;
    }
    return (int)w;
}

int json_str(const json_doc_t *d, int i, char *out, size_t cap)
{
    if (!out || cap == 0) {
        return -1;
    }
    return str_walk(d, i, out, cap);
}

int json_str_len(const json_doc_t *d, int i)
{
    return str_walk(d, i, NULL, 0);
}

bool json_str_eq(const json_doc_t *d, int i, const char *s)
{
    if (i < 0 || i >= d->n || d->t[i].type != JSON_STR) {
        return false;
    }
    const size_t raw = d->t[i].end - d->t[i].start;
    const size_t want = strlen(s);
    if (memchr(d->js + d->t[i].start, '\\', raw) == NULL) {
        return raw == want && memcmp(d->js + d->t[i].start, s, raw) == 0;
    }
    char buf[64];
    if (want >= sizeof(buf)) {
        return false;
    }
    const int n = json_str(d, i, buf, sizeof(buf));
    return n == (int)want && memcmp(buf, s, want) == 0;
}

int json_get(const json_doc_t *d, int obj, const char *key)
{
    if (obj < 0 || obj >= d->n || d->t[obj].type != JSON_OBJ) {
        return -1;
    }
    for (int k = json_first(d, obj); k >= 0; k = json_next(d, obj, k)) {
        if (json_str_eq(d, k, key)) {
            return k + 1;
        }
    }
    return -1;
}

bool json_num(const json_doc_t *d, int i, double *out)
{
    if (i < 0 || i >= d->n || d->t[i].type != JSON_NUM) {
        return false;
    }
    char buf[40];
    const size_t n = d->t[i].end - d->t[i].start;
    if (n >= sizeof(buf)) {
        return false;
    }
    memcpy(buf, d->js + d->t[i].start, n);
    buf[n] = 0;
    *out = strtod(buf, NULL);
    return true;
}

bool json_int(const json_doc_t *d, int i, int64_t *out)
{
    if (i < 0 || i >= d->n || d->t[i].type != JSON_NUM) {
        return false;
    }
    const char *s = d->js + d->t[i].start;
    const size_t n = d->t[i].end - d->t[i].start;
    size_t k = 0;
    bool neg = false;
    if (s[0] == '-') {
        neg = true;
        k = 1;
    }
    int64_t v = 0;
    for (; k < n; k++) {
        if (!is_digit(s[k]) || v > (INT64_MAX - 9) / 10) {
            return false; /* a fraction, an exponent or too big */
        }
        v = v * 10 + (s[k] - '0');
    }
    *out = neg ? -v : v;
    return true;
}

bool json_bool(const json_doc_t *d, int i, bool *out)
{
    if (i < 0 || i >= d->n || (d->t[i].type != JSON_TRUE && d->t[i].type != JSON_FALSE)) {
        return false;
    }
    *out = d->t[i].type == JSON_TRUE;
    return true;
}

size_t json_quote(char *out, size_t max_bytes, const char *s)
{
    if (max_bytes < 2) {
        return 0;
    }
    size_t w = 0;
    out[w++] = '"';
    for (size_t k = 0; s[k];) {
        const unsigned char c = (unsigned char)s[k];
        char esc[8];
        size_t elen;
        size_t adv = 1;
        if (c == '"' || c == '\\') {
            esc[0] = '\\';
            esc[1] = (char)c;
            elen = 2;
        } else if (c < 0x20) {
            static const char hex[] = "0123456789abcdef";
            memcpy(esc, "\\u00", 4);
            esc[4] = hex[c >> 4];
            esc[5] = hex[c & 15];
            elen = 6;
        } else {
            adv = c < 0x80 ? 1 : c < 0xE0 ? 2 : c < 0xF0 ? 3 : 4;
            for (size_t j = 1; j < adv; j++) {
                if (!s[k + j]) {
                    adv = j;
                    break;
                }
            }
            memcpy(esc, s + k, adv);
            elen = adv;
        }
        if (w + elen + 1 > max_bytes) {
            break; /* room for the closing quote */
        }
        memcpy(out + w, esc, elen);
        w += elen;
        k += adv;
    }
    out[w++] = '"';
    return w;
}
