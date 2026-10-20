/* A small strict JSON tokenizer for the feed, details and outbox actions
 * (docs/t5-pda-plan.md §4). Pure C; host-tested (test/test_json.c).
 *
 * json_parse() fills a flat token array in document order. Each container token knows the
 * index just past its subtree (`next`), so walking objects and arrays needs no recursion
 * and no allocation. Strings stay escaped in the input; json_str() unescapes on demand. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define JSON_MAX_DEPTH 16

typedef enum {
    JSON_OBJ = 1,
    JSON_ARR,
    JSON_STR,
    JSON_NUM,
    JSON_TRUE,
    JSON_FALSE,
    JSON_NULL,
} json_type_t;

typedef struct {
    uint8_t type;   /* json_type_t */
    uint32_t start; /* first char (strings: after the quote) */
    uint32_t end;   /* one past the last char (strings: the closing quote) */
    uint32_t size;  /* objects: keys, arrays: elements */
    uint32_t next;  /* index of the token after this value's subtree */
} json_tok_t;

#define JSON_E_NOMEM -1  /* more tokens than max */
#define JSON_E_SYNTAX -2
#define JSON_E_DEPTH -3

typedef struct {
    const char *js;
    const json_tok_t *t;
    int n;
} json_doc_t;

/* Returns the number of tokens (>= 1) or a JSON_E_* code. The whole input must be one
 * value (surrounding whitespace allowed). */
int json_parse(const char *js, size_t len, json_tok_t *toks, int max);

/* Object member: the value's token index, or -1. Keys are compared after unescaping. */
int json_get(const json_doc_t *d, int obj, const char *key);
/* First element of an array (or first key of an object), or -1 if empty; then
 * json_next() moves to the following element (objects: the following key). */
int json_first(const json_doc_t *d, int container);
int json_next(const json_doc_t *d, int container, int i);

/* Unescape a string token into out (UTF-8, NUL-terminated, cut at a character boundary
 * if it doesn't fit). Returns the length written, or -1 if the token isn't a string. */
int json_str(const json_doc_t *d, int i, char *out, size_t cap);
/* Number of bytes json_str would need (without the NUL), or -1. */
int json_str_len(const json_doc_t *d, int i);
bool json_str_eq(const json_doc_t *d, int i, const char *s);

bool json_int(const json_doc_t *d, int i, int64_t *out);   /* integral numbers only */
bool json_num(const json_doc_t *d, int i, double *out);
bool json_bool(const json_doc_t *d, int i, bool *out);

/* Append s to out as a JSON string literal (with quotes), escaping as needed and cutting
 * s at a UTF-8 boundary so the whole thing fits in max_bytes. Returns bytes written. */
size_t json_quote(char *out, size_t max_bytes, const char *s);

#ifdef __cplusplus
}
#endif
