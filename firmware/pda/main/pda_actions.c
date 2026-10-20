#include "pda_actions.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "json.h"

#define MAX PDA_PROTO_OB_JSON_MAX

typedef struct {
    char *out;
    size_t w;
    bool overflow;
} sb_t;

static void lit(sb_t *b, const char *s)
{
    const size_t n = strlen(s);
    if (b->w + n > MAX) {
        b->overflow = true;
        return;
    }
    memcpy(b->out + b->w, s, n);
    b->w += n;
}

/* A quoted string, cut to leave `reserve` bytes for what follows. */
static void quoted(sb_t *b, const char *s, size_t reserve)
{
    if (b->w + 2 + reserve > MAX) {
        b->overflow = true;
        return;
    }
    b->w += json_quote(b->out + b->w, MAX - b->w - reserve, s);
}

static size_t finish(sb_t *b)
{
    if (b->overflow) {
        b->out[0] = 0;
        return 0;
    }
    b->out[b->w] = 0;
    return b->w;
}

size_t pda_action_item_status(char *out, int64_t id, const char *status)
{
    sb_t b = {.out = out};
    char num[24];
    snprintf(num, sizeof(num), "%" PRId64, id);
    lit(&b, "{\"t\":\"item_status\",\"id\":");
    lit(&b, num);
    lit(&b, ",\"status\":");
    quoted(&b, status, 1);
    lit(&b, "}");
    return finish(&b);
}

size_t pda_action_cal_add(char *out, const char *title, const char *start, const char *end,
                          bool all_day, int64_t from_item)
{
    /* Everything after the title is fixed size: build it first to know the reserve. */
    char tail[MAX + 1];
    sb_t t = {.out = tail};
    lit(&t, ",\"start\":");
    quoted(&t, start, 0);
    if (all_day) {
        lit(&t, ",\"all_day\":true");
    } else if (end && *end) {
        lit(&t, ",\"end\":");
        quoted(&t, end, 0);
    }
    if (from_item >= 0) {
        char num[24];
        snprintf(num, sizeof(num), "%" PRId64, from_item);
        lit(&t, ",\"from_item\":");
        lit(&t, num);
    }
    lit(&t, "}");
    if (t.overflow || t.w >= sizeof(tail)) {
        out[0] = 0;
        return 0;
    }
    tail[t.w] = 0;
    sb_t b = {.out = out};
    lit(&b, "{\"t\":\"cal_add\",\"title\":");
    quoted(&b, title, t.w);
    lit(&b, tail);
    return finish(&b);
}

static size_t one_string(char *out, const char *type, const char *key, const char *val)
{
    sb_t b = {.out = out};
    lit(&b, "{\"t\":\"");
    lit(&b, type);
    lit(&b, "\",\"");
    lit(&b, key);
    lit(&b, "\":");
    quoted(&b, val, 1);
    lit(&b, "}");
    return finish(&b);
}

size_t pda_action_cal_delete(char *out, const char *id)
{
    return one_string(out, "cal_delete", "id", id);
}

size_t pda_action_fetch(char *out, const char *ref)
{
    return one_string(out, "fetch", "ref", ref);
}

size_t pda_action_digest_refresh(char *out, const char *date)
{
    return one_string(out, "digest_refresh", "date", date);
}

/* The first `chars` code points of s, as a new string in buf. */
static const char *first_chars(const char *s, int chars, char *buf, size_t cap)
{
    size_t n = 0;
    for (int c = 0; s[n] && c < chars; c++) {
        size_t step = 1;
        while (s[n + step] && ((unsigned char)s[n + step] & 0xC0) == 0x80) {
            step++; /* continuation bytes of the same character */
        }
        if (n + step >= cap) {
            break;
        }
        n += step;
    }
    memcpy(buf, s, n);
    buf[n] = 0;
    return buf;
}

size_t pda_action_todo_toggle(char *out, const char *note, int32_t i, const char *text, bool done)
{
    char head[MAX + 1];
    sb_t h = {.out = head};
    lit(&h, "{\"t\":\"todo_toggle\",\"note\":");
    quoted(&h, note, 0);
    if (h.overflow || h.w >= sizeof(head) || (size_t)strlen(note) + 2 + 26 > h.w) {
        /* the note id was cut: it has to be exact */
        out[0] = 0;
        return 0;
    }
    char num[16];
    snprintf(num, sizeof(num), ",\"i\":%" PRId32 ",\"text\":", i);
    lit(&h, num);
    if (h.overflow) {
        out[0] = 0;
        return 0;
    }
    const char *tail = done ? ",\"done\":true}" : ",\"done\":false}";
    char t80[PDA_TODO_TEXT_CHARS * 4 + 1];
    first_chars(text ? text : "", PDA_TODO_TEXT_CHARS, t80, sizeof(t80));
    sb_t b = {.out = out};
    memcpy(out, head, h.w);
    b.w = h.w;
    quoted(&b, t80, strlen(tail)); /* cut further only if 400 bytes would pass */
    lit(&b, tail);
    return finish(&b);
}

bool pda_action_parse(const char *json, size_t len, pda_action_info_t *a)
{
    memset(a, 0, sizeof(*a));
    a->item_id = -1;
    json_tok_t toks[48];
    const int n = json_parse(json, len, toks, 48);
    if (n < 1 || toks[0].type != JSON_OBJ) {
        return false;
    }
    const json_doc_t d = {.js = json, .t = toks, .n = n};
    const int t = json_get(&d, 0, "t");
    if (json_str_eq(&d, t, "item_status")) {
        a->type = PDA_ACT_ITEM_STATUS;
        json_int(&d, json_get(&d, 0, "id"), &a->item_id);
        json_str(&d, json_get(&d, 0, "status"), a->status, sizeof(a->status));
    } else if (json_str_eq(&d, t, "cal_add")) {
        a->type = PDA_ACT_CAL_ADD;
        json_str(&d, json_get(&d, 0, "title"), a->title, sizeof(a->title));
        json_str(&d, json_get(&d, 0, "start"), a->start, sizeof(a->start));
        json_str(&d, json_get(&d, 0, "end"), a->end, sizeof(a->end));
        json_bool(&d, json_get(&d, 0, "all_day"), &a->all_day);
        json_int(&d, json_get(&d, 0, "from_item"), &a->item_id);
    } else if (json_str_eq(&d, t, "cal_delete")) {
        a->type = PDA_ACT_CAL_DELETE;
        json_str(&d, json_get(&d, 0, "id"), a->ref, sizeof(a->ref));
    } else if (json_str_eq(&d, t, "fetch")) {
        a->type = PDA_ACT_FETCH;
        json_str(&d, json_get(&d, 0, "ref"), a->ref, sizeof(a->ref));
    } else if (json_str_eq(&d, t, "digest_refresh")) {
        a->type = PDA_ACT_DIGEST_REFRESH;
        json_str(&d, json_get(&d, 0, "date"), a->ref, sizeof(a->ref));
    } else if (json_str_eq(&d, t, "todo_toggle")) {
        a->type = PDA_ACT_TODO_TOGGLE;
        json_str(&d, json_get(&d, 0, "note"), a->ref, sizeof(a->ref));
        json_int(&d, json_get(&d, 0, "i"), &a->item_id);
        json_str(&d, json_get(&d, 0, "text"), a->title, sizeof(a->title));
        json_bool(&d, json_get(&d, 0, "done"), &a->done);
    } else {
        return false;
    }
    return true;
}
