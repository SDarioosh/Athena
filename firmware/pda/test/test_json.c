/* The JSON tokenizer behind the feed, details and actions. */
#include <string.h>

#include "check.h"
#include "json.h"

static json_tok_t toks[256];

static int parse(const char *s, json_doc_t *d)
{
    const int n = json_parse(s, strlen(s), toks, 256);
    *d = (json_doc_t){.js = s, .t = toks, .n = n};
    return n;
}

static void test_valid(void)
{
    json_doc_t d;
    const char *s = " {\"a\": 1, \"b\": [true, false, null, -2.5e3, \"x\"], \"c\": {\"d\": \"e\"},"
                    " \"empty\": [], \"o\": {}} ";
    CHECK(parse(s, &d) > 0);
    CHECK_EQ(toks[0].type, JSON_OBJ);
    CHECK_EQ(toks[0].size, 5);
    int64_t v;
    CHECK(json_int(&d, json_get(&d, 0, "a"), &v) && v == 1);
    const int b = json_get(&d, 0, "b");
    CHECK_EQ(toks[b].size, 5);
    int i = json_first(&d, b);
    bool bv;
    CHECK(json_bool(&d, i, &bv) && bv);
    i = json_next(&d, b, i);
    CHECK(json_bool(&d, i, &bv) && !bv);
    i = json_next(&d, b, i);
    CHECK_EQ(toks[i].type, JSON_NULL);
    i = json_next(&d, b, i);
    double num;
    CHECK(json_num(&d, i, &num) && num == -2500.0);
    CHECK(!json_int(&d, i, &v)); /* not integral */
    i = json_next(&d, b, i);
    CHECK(json_str_eq(&d, i, "x"));
    CHECK_EQ(json_next(&d, b, i), -1);
    const int c = json_get(&d, 0, "c");
    CHECK(json_str_eq(&d, json_get(&d, c, "d"), "e"));
    CHECK_EQ(json_get(&d, c, "zz"), -1);
    CHECK_EQ(json_first(&d, json_get(&d, 0, "empty")), -1);
    CHECK_EQ(json_first(&d, json_get(&d, 0, "o")), -1);
    CHECK_EQ(json_get(&d, 0, "missing"), -1);
    /* iterate the object's keys */
    int keys = 0;
    for (int k = json_first(&d, 0); k >= 0; k = json_next(&d, 0, k)) {
        keys++;
    }
    CHECK_EQ(keys, 5);
}

static void test_strings(void)
{
    json_doc_t d;
    const char *s = "[\"a\\\"b\\\\c\\/d\\n\", \"\\u00e9\\u20ac\", \"\\ud83d\\ude00\", \"caf\xc3\xa9\","
                    " \"\\ud800x\"]";
    CHECK(parse(s, &d) > 0);
    char out[32];
    int i = json_first(&d, 0);
    CHECK_EQ(json_str(&d, i, out, sizeof(out)), 8);
    CHECK(strcmp(out, "a\"b\\c/d\n") == 0);
    i = json_next(&d, 0, i);
    CHECK_EQ(json_str_len(&d, i), 5);
    json_str(&d, i, out, sizeof(out));
    CHECK(strcmp(out, "\xc3\xa9\xe2\x82\xac") == 0);
    i = json_next(&d, 0, i);
    json_str(&d, i, out, sizeof(out));
    CHECK(strcmp(out, "\xf0\x9f\x98\x80") == 0); /* surrogate pair -> U+1F600 */
    i = json_next(&d, 0, i);
    CHECK(json_str_eq(&d, i, "caf\xc3\xa9"));
    /* cut at a character boundary: "caf" + 2-byte é doesn't fit in 4 + NUL */
    CHECK_EQ(json_str(&d, i, out, 5), 3);
    CHECK(strcmp(out, "caf") == 0);
    i = json_next(&d, 0, i);
    json_str(&d, i, out, sizeof(out));
    CHECK(strcmp(out, "\xef\xbf\xbdx") == 0); /* lone surrogate -> U+FFFD */
    /* escaped keys still match */
    CHECK(parse("{\"k\\u0065y\": 5}", &d) > 0);
    int64_t v;
    CHECK(json_int(&d, json_get(&d, 0, "key"), &v) && v == 5);
}

static void test_invalid(void)
{
    static const char *bad[] = {
        "",          "{",          "{\"a\"}",       "{\"a\":}",      "[1,]",
        "[1 2]",     "{a:1}",      "\"unterminated", "[\"\\x\"]",   "01",
        "1.",        "-",          "tru",           "[1] x",        "{\"a\":1,}",
        "\"\x01\"",  "[\"\\u12\"]", "1e",
    };
    json_doc_t d;
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        if (parse(bad[i], &d) >= 0) {
            fprintf(stderr, "accepted: %s\n", bad[i]);
            checks_failed++;
        }
    }
    /* too many tokens */
    CHECK_EQ(json_parse("[1,2,3]", 7, toks, 3), JSON_E_NOMEM);
    /* too deep */
    char deep[64];
    memset(deep, '[', 20);
    memset(deep + 20, ']', 20);
    deep[40] = 0;
    CHECK_EQ(json_parse(deep, 40, toks, 256), JSON_E_DEPTH);
}

/* QC P5: broken UTF-8 in the input comes out as U+FFFD, never as part of a sequence, and
 * json_str_len agrees with what json_str writes */
static void test_bad_utf8(void)
{
    static const struct {
        const char *in, *out;
    } cases[] = {
        {"[\"ab\xe2\x82\"]", "ab\xef\xbf\xbd"},          /* truncated at the end */
        {"[\"\xe2\x82x\"]", "\xef\xbf\xbdx"},            /* truncated before ASCII */
        {"[\"\xc3" "A\"]", "\xef\xbf\xbd" "A"},              /* lead byte, then ASCII */
        {"[\"\xc3\\n\"]", "\xef\xbf\xbd\n"},            /* lead byte, then an escape */
        {"[\"\x80x\"]", "\xef\xbf\xbdx"},                 /* lone continuation byte */
        {"[\"\xffx\"]", "\xef\xbf\xbdx"},                 /* never a lead byte */
        {"[\"\xf0\x9f\x98\x80\xc3\xa9\"]", "\xf0\x9f\x98\x80\xc3\xa9"}, /* valid */
    };
    for (size_t c = 0; c < sizeof(cases) / sizeof(cases[0]); c++) {
        json_doc_t d;
        CHECK(parse(cases[c].in, &d) > 0);
        const int i = json_first(&d, 0);
        char out[32];
        const int n = json_str(&d, i, out, sizeof(out));
        if (n < 0 || strcmp(out, cases[c].out) != 0 || json_str_len(&d, i) != n ||
            n != (int)strlen(cases[c].out)) {
            fprintf(stderr, "bad utf8 case %zu: got %d bytes\n", c, n);
            checks_failed++;
        }
    }
    /* a cut still keeps whole characters: "ab" + U+FFFD doesn't fit in 4 + NUL */
    json_doc_t d;
    CHECK(parse("[\"ab\xe2\x82\"]", &d) > 0);
    char out[8];
    CHECK_EQ(json_str(&d, json_first(&d, 0), out, 5), 2);
    CHECK(strcmp(out, "ab") == 0);
}

static void test_quote(void)
{
    char out[64];
    size_t n = json_quote(out, sizeof(out), "a\"b\\c\n\x01");
    out[n] = 0;
    CHECK(strcmp(out, "\"a\\\"b\\\\c\\u000a\\u0001\"") == 0);
    /* cut, keeping whole UTF-8 sequences and the closing quote */
    n = json_quote(out, 7, "ab\xc3\xa9\xc3\xa9");
    out[n] = 0;
    CHECK(strcmp(out, "\"ab\xc3\xa9\"") == 0);
    n = json_quote(out, 2, "abc");
    out[n] = 0;
    CHECK(strcmp(out, "\"\"") == 0);
    /* round trip */
    char q[64];
    const char *orig = "line\nwith \"quotes\" and \xe2\x82\xac";
    n = json_quote(q, sizeof(q), orig);
    q[n] = 0;
    json_doc_t d;
    CHECK(parse(q, &d) == 1);
    json_str(&d, 0, out, sizeof(out));
    CHECK(strcmp(out, orig) == 0);
}

int main(void)
{
    test_valid();
    test_strings();
    test_invalid();
    test_bad_utf8();
    test_quote();
    return checks_done("test_json");
}
