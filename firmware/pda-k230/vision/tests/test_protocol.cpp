// Contract 2 lines: encode, decode, round trips, the 1024-byte limit, hostile input.
#include <random>

#include "json.h"
#include "protocol.h"
#include "testlib.h"

using av::json::Value;

namespace {

Value parse_line(const std::string &l) {
    Value v;
    bool ok = !l.empty() && l.back() == '\n' && av::json::parse(l.substr(0, l.size() - 1), &v);
    CHECK(ok);
    CHECK(l.size() <= av::kMaxLine);
    CHECK(l.find('\n') == l.size() - 1);
    return v;
}

std::string field(const Value &v, const char *k) {
    std::string s;
    v.get_string(k, &s);
    return s;
}

double num(const Value &v, const char *k) {
    double d = -1;
    v.get_number(k, &d);
    return d;
}

}  // namespace

TEST(state_message) {
    Value v = parse_line(av::encode_state(av::CameraState::On, 14.76, "ok"));
    CHECK_EQ(field(v, "t"), std::string("state"));
    CHECK_EQ(field(v, "camera"), std::string("on"));
    CHECK_NEAR(num(v, "fps"), 14.8, 1e-9);
    CHECK_EQ(field(v, "msg"), std::string("ok"));
    v = parse_line(av::encode_state(av::CameraState::Error, NAN, "no /dev/video1"));
    CHECK_EQ(field(v, "camera"), std::string("error"));
    CHECK_EQ(num(v, "fps"), 0.0);
    v = parse_line(av::encode_state(av::CameraState::Off, 0, ""));
    CHECK_EQ(field(v, "camera"), std::string("off"));
}

TEST(gesture_hand_presence_qr) {
    Value v = parse_line(av::encode_gesture("swipe_left", 0.8312f));
    CHECK_EQ(field(v, "t"), std::string("gesture"));
    CHECK_EQ(field(v, "g"), std::string("swipe_left"));
    CHECK_NEAR(num(v, "conf"), 0.83, 1e-9);
    v = parse_line(av::encode_gesture("palm", 7.0f));
    CHECK_EQ(num(v, "conf"), 1.0);

    v = parse_line(av::encode_hand(0.1234f, 1.7f, av::Shape::ThumbsUp));
    CHECK_EQ(field(v, "t"), std::string("hand"));
    CHECK_NEAR(num(v, "x"), 0.123, 1e-9);
    CHECK_EQ(num(v, "y"), 1.0);
    CHECK_EQ(field(v, "shape"), std::string("thumbs_up"));
    v = parse_line(av::encode_hand(-1, NAN, av::Shape::None));
    CHECK_EQ(num(v, "x"), 0.0);
    CHECK_EQ(num(v, "y"), 0.0);
    CHECK_EQ(field(v, "shape"), std::string("none"));

    v = parse_line(av::encode_presence(true));
    bool b = false;
    CHECK(v.get_bool("present", &b) && b);

    std::string pair = "odysseus-pair:v1?url=https://hub.example.ts.net&code=ABCD-1234";
    v = parse_line(av::encode_qr(pair));
    CHECK_EQ(field(v, "text"), pair);
}

TEST(line_limit) {
    // a QR text that fits exactly, and one that doesn't (it isn't cut: it is dropped)
    std::string base = av::encode_qr("");  // {"t":"qr","text":""}\n
    size_t room = av::kMaxLine - base.size();
    std::string fits(room, 'a');
    std::string l = av::encode_qr(fits);
    CHECK_EQ(l.size(), av::kMaxLine);
    CHECK(av::encode_qr(fits + "a").empty());
    // escapes count: quotes double in size
    CHECK(av::encode_qr(std::string(room, '"')).empty());
    // a long state message is cut to fit, on a UTF-8 boundary
    std::string msg;
    for (int i = 0; i < 600; i++) msg += "\xC3\xA9\"";
    l = av::encode_state(av::CameraState::Error, 3, msg);
    Value v = parse_line(l);
    CHECK(l.size() <= av::kMaxLine);
    CHECK(field(v, "msg").size() > 100);
    CHECK(field(v, "msg").find("\xEF\xBF\xBD") == std::string::npos);  // no broken char
}

TEST(mode_decode) {
    av::UiMessage m = av::decode_ui_line("{\"t\":\"mode\",\"gestures\":true,\"presence\":false,\"qr\":true}\n");
    CHECK(m.kind == av::UiMessage::Kind::Mode);
    CHECK(m.mode.gestures && !m.mode.presence && m.mode.qr);
    m = av::decode_ui_line("{\"t\":\"mode\"}\r\n");
    CHECK(m.kind == av::UiMessage::Kind::Mode);
    CHECK(!m.mode.any());
    // non-booleans are false; unknown keys ignored
    m = av::decode_ui_line("{\"t\":\"mode\",\"gestures\":1,\"presence\":\"true\",\"qr\":true,\"x\":{\"y\":[1]}}");
    CHECK(m.kind == av::UiMessage::Kind::Mode);
    CHECK(!m.mode.gestures && !m.mode.presence && m.mode.qr);
    m = av::decode_ui_line("{\"t\":\"hello\",\"a\":1}");
    CHECK(m.kind == av::UiMessage::Kind::Unknown);
    CHECK_EQ(m.type, std::string("hello"));
}

TEST(mode_round_trip_all_combinations) {
    for (int bits = 0; bits < 8; bits++) {
        av::json::ObjectWriter w;
        w.str("t", "mode").boolean("gestures", bits & 1).boolean("presence", bits & 2).boolean("qr", bits & 4);
        av::UiMessage m = av::decode_ui_line(w.done() + "\n");
        CHECK(m.kind == av::UiMessage::Kind::Mode);
        CHECK(m.mode.gestures == bool(bits & 1));
        CHECK(m.mode.presence == bool(bits & 2));
        CHECK(m.mode.qr == bool(bits & 4));
        CHECK(m.mode.any() == (bits != 0));
    }
}

TEST(invalid_ui_lines) {
    const char *bad[] = {"", "\n", "mode", "[]", "{\"t\":1}", "{\"gestures\":true}", "{\"t\":\"mode\"",
                         "{\"t\":\"mode\",}", "null", "\"mode\""};
    for (const char *b : bad) {
        CHECK_MSG(av::decode_ui_line(b).kind == av::UiMessage::Kind::Invalid, b);
    }
    std::string big = "{\"t\":\"mode\",\"pad\":\"" + std::string(1100, 'x') + "\"}";
    CHECK(av::decode_ui_line(big).kind == av::UiMessage::Kind::Invalid);
    std::mt19937 rng(9);
    std::uniform_int_distribution<int> byte(0, 255);
    for (int i = 0; i < 20000; i++) {
        std::string s = "{\"t\":\"mode\",";
        int n = i % 40;
        for (int k = 0; k < n; k++) s += static_cast<char>(byte(rng));
        av::decode_ui_line(s);  // must not crash
    }
}

TEST_MAIN()
