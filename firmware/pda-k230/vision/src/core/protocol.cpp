#include "protocol.h"

#include <algorithm>

#include "json.h"

namespace av {

namespace {

float clamp01(float v) {
    if (!(v >= 0.0f)) return 0.0f;
    return v > 1.0f ? 1.0f : v;
}

// Cut s to at most n bytes without splitting a UTF-8 sequence.
std::string_view cut_utf8(std::string_view s, size_t n) {
    if (s.size() <= n) return s;
    size_t i = n;
    while (i > 0 && (static_cast<unsigned char>(s[i]) & 0xC0) == 0x80) i--;
    return s.substr(0, i);
}

std::string line(const json::ObjectWriter &w) { return w.done() + "\n"; }

}  // namespace

const char *camera_state_name(CameraState s) {
    switch (s) {
    case CameraState::On: return "on";
    case CameraState::Error: return "error";
    case CameraState::Off: break;
    }
    return "off";
}

std::string encode_state(CameraState camera, double fps, std::string_view msg) {
    if (!(fps >= 0.0) || fps > 1000.0) fps = 0.0;
    std::string_view m = msg;
    while (true) {
        json::ObjectWriter w;
        w.str("t", "state").str("camera", camera_state_name(camera)).num("fps", fps, 1)
            .str("msg", m);
        std::string out = line(w);
        if (out.size() <= kMaxLine) return out;
        size_t excess = out.size() - kMaxLine;
        // escapes can make the encoded text longer than the raw one: cut generously
        size_t keep = m.size() > excess + 8 ? m.size() - excess - 8 : 0;
        m = cut_utf8(m, keep);
    }
}

std::string encode_gesture(std::string_view g, float conf) {
    json::ObjectWriter w;
    w.str("t", "gesture").str("g", g.substr(0, 32)).num("conf", clamp01(conf), 2);
    return line(w);
}

std::string encode_hand(float x, float y, Shape shape) {
    json::ObjectWriter w;
    w.str("t", "hand").num("x", clamp01(x), 3).num("y", clamp01(y), 3)
        .str("shape", shape_name(shape));
    return line(w);
}

std::string encode_presence(bool present) {
    json::ObjectWriter w;
    w.str("t", "presence").boolean("present", present);
    return line(w);
}

std::string encode_qr(std::string_view text) {
    json::ObjectWriter w;
    w.str("t", "qr").str("text", text);
    std::string out = line(w);
    if (out.size() > kMaxLine) return std::string();
    return out;
}

UiMessage decode_ui_line(std::string_view l) {
    UiMessage m;
    if (l.size() > kMaxLine) return m;
    while (!l.empty() && (l.back() == '\n' || l.back() == '\r')) l.remove_suffix(1);
    json::Value v;
    json::ParseLimits lim;
    lim.max_bytes = kMaxLine;
    if (!json::parse(l, &v, nullptr, lim) || !v.is_object()) return m;
    std::string t;
    if (!v.get_string("t", &t)) return m;
    if (t == "mode") {
        m.kind = UiMessage::Kind::Mode;
        bool b;
        m.mode.gestures = v.get_bool("gestures", &b) && b;
        m.mode.presence = v.get_bool("presence", &b) && b;
        m.mode.qr = v.get_bool("qr", &b) && b;
        return m;
    }
    m.kind = UiMessage::Kind::Unknown;
    m.type = t.substr(0, 64);
    return m;
}

}  // namespace av
