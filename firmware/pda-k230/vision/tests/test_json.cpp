// The tiny JSON reader and writer, including malformed and hostile input.
#include <random>

#include "json.h"
#include "testlib.h"

using av::json::Value;

TEST(parses_objects_arrays_scalars) {
    Value v;
    CHECK(av::json::parse(R"( {"a":1,"b":[true,false,null],"c":{"d":"x"},"e":-2.5e2} )", &v));
    CHECK(v.is_object());
    double d;
    CHECK(v.get_number("a", &d) && d == 1);
    CHECK(v.get_number("e", &d) && d == -250);
    const Value *b = v.get("b");
    CHECK(b && b->type == Value::Type::Array && b->arr.size() == 3);
    CHECK(b->arr[0].type == Value::Type::Bool && b->arr[0].b);
    CHECK(b->arr[2].type == Value::Type::Null);
    std::string s;
    CHECK(v.get("c")->get_string("d", &s) && s == "x");
    CHECK(!v.get_string("a", &s));
    bool bb;
    CHECK(!v.get_bool("missing", &bb));
}

TEST(string_escapes_and_unicode) {
    Value v;
    CHECK(av::json::parse(R"({"s":"a\"b\\c\/d\n\t\u00e9\ud83d\ude00"})", &v));
    std::string s;
    CHECK(v.get_string("s", &s));
    CHECK_EQ(s, std::string("a\"b\\c/d\n\t\xC3\xA9\xF0\x9F\x98\x80"));
    // lone surrogates become U+FFFD instead of failing
    CHECK(av::json::parse(R"({"s":"\udc00x"})", &v));
    CHECK(v.get_string("s", &s) && s == "\xEF\xBF\xBDx");
}

TEST(rejects_malformed) {
    const char *bad[] = {
        "", "{", "}", "{\"a\"}", "{\"a\":}", "{\"a\":1,}", "[1,2", "{\"a\":tru}", "nul",
        "{\"a\":01}", "{\"a\":1.}", "{\"a\":.5}", "{\"a\":1e}", "{\"a\":\"\\x\"}",
        "{\"a\":\"unterminated}", "{\"a\":1} x", "{\"a\":\"\\u12\"}", "{a:1}",
        "{\"a\":\"\x01\"}", "{\"a\":1e999}", "{\"a\":--1}", "{\"\\ud800\\u0041\":1}",
    };
    for (const char *b : bad) {
        Value v;
        std::string err;
        CHECK_MSG(!av::json::parse(b, &v, &err), b);
        CHECK(!err.empty());
    }
}

TEST(limits_depth_and_size) {
    std::string deep(100, '[');
    deep += std::string(100, ']');
    Value v;
    CHECK(!av::json::parse(deep, &v));
    av::json::ParseLimits lim;
    lim.max_bytes = 10;
    CHECK(!av::json::parse("{\"a\":\"0123456789\"}", &v, nullptr, lim));
}

TEST(random_bytes_never_crash) {
    std::mt19937 rng(42);
    const std::string alphabet = "{}[]\":,0123456789.eE+-truefalsnul\\u \n\t\xff\x80";
    std::uniform_int_distribution<size_t> pick(0, alphabet.size() - 1), len(0, 80);
    int parsed = 0;
    for (int i = 0; i < 50000; i++) {
        std::string s;
        size_t n = len(rng);
        for (size_t k = 0; k < n; k++) s += alphabet[pick(rng)];
        Value v;
        parsed += av::json::parse(s, &v);
    }
    CHECK(parsed >= 0);
    // truncations of a valid line
    std::string good = R"({"t":"mode","gestures":true,"presence":false,"qr":true,"x":[1,2,{"y":"z"}]})";
    for (size_t n = 0; n < good.size(); n++) {
        Value v;
        CHECK(!av::json::parse(good.substr(0, n), &v));
    }
}

TEST(writer_round_trip) {
    av::json::ObjectWriter w;
    w.str("t", "qr").str("text", "line1\nquote\" back\\ tab\t \x01 \xC3\xA9")
        .num("n", 0.12345).num("i", 42).num("neg", -0.0001).boolean("b", true).null("z");
    std::string out = w.done();
    Value v;
    CHECK(av::json::parse(out, &v));
    std::string s;
    CHECK(v.get_string("text", &s) && s == "line1\nquote\" back\\ tab\t \x01 \xC3\xA9");
    double d;
    CHECK(v.get_number("n", &d) && d == 0.123);
    CHECK(v.get_number("i", &d) && d == 42);
    CHECK(v.get_number("neg", &d) && d == 0);
    CHECK(out.find("\"i\":42") != std::string::npos);
    CHECK(out.find("\"neg\":0") != std::string::npos);
}

TEST(writer_cleans_bad_utf8_and_numbers) {
    std::string out;
    av::json::append_string(out, std::string("a\xff" "b\xC3", 4));
    CHECK_EQ(out, std::string("\"a\xEF\xBF\xBD" "b\xEF\xBF\xBD\""));
    Value v;
    CHECK(av::json::parse("{\"s\":" + out + "}", &v));
    std::string n;
    av::json::append_number(n, NAN);
    CHECK_EQ(n, std::string("0"));
    n.clear();
    av::json::append_number(n, 1.5, 1);
    CHECK_EQ(n, std::string("1.5"));
    n.clear();
    av::json::append_number(n, 3.0, 2);
    CHECK_EQ(n, std::string("3"));
}

TEST_MAIN()
