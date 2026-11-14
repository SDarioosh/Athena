#include "track.h"

#include <cmath>

#include "json.h"

namespace av {

std::string encode_track_header(const TrackHeader &h) {
    json::ObjectWriter w;
    w.str("track", "athena-vision").num("v", 1).num("w", h.width).num("h", h.height);
    if (h.has_orientation) {
        w.num("rotate", h.orientation.rotate).boolean("mirror", h.orientation.mirror);
    }
    return w.done() + "\n";
}

std::string encode_track_frame(const FrameResult &f, bool write_size) {
    json::ObjectWriter w;
    w.num("t", f.t, 3);
    if (write_size) w.num("w", f.width).num("h", f.height);
    if (f.hand_ran) {
        std::string hands = "[";
        for (size_t i = 0; i < f.hands.size(); i++) {
            const HandDetection &h = f.hands[i];
            if (i) hands += ',';
            json::ObjectWriter hw;
            std::string box = "[";
            const float b[4] = {h.x1, h.y1, h.x2, h.y2};
            for (int k = 0; k < 4; k++) {
                if (k) box += ',';
                json::append_number(box, b[k], 1);
            }
            box += ']';
            hw.raw("box", box).num("score", h.score, 3);
            if (h.has_keypoints) {
                std::string kp = "[";
                for (int k = 0; k < kNumKeypoints; k++) {
                    if (k) kp += ',';
                    json::append_number(kp, h.kp[k].x, 1);
                    kp += ',';
                    json::append_number(kp, h.kp[k].y, 1);
                }
                kp += ']';
                hw.raw("kp", kp);
            }
            hands += hw.done();
        }
        hands += ']';
        w.raw("hands", hands);
    }
    if (f.face_ran) w.boolean("face", f.face_seen);
    return w.done() + "\n";
}

namespace {

bool read_numbers(const json::Value *v, float *out, size_t n) {
    if (!v || v->type != json::Value::Type::Array || v->arr.size() != n) return false;
    for (size_t i = 0; i < n; i++) {
        if (v->arr[i].type != json::Value::Type::Number) return false;
        out[i] = static_cast<float>(v->arr[i].num);
    }
    return true;
}

bool read_int(const json::Value &v, const char *key, int *out) {
    double d;
    if (!v.get_number(key, &d) || d < 0 || d > 100000) return false;
    *out = static_cast<int>(d);
    return true;
}

}  // namespace

TrackLine decode_track_line(std::string_view line, TrackHeader *header, FrameResult *frame,
                            std::string *err) {
    while (!line.empty() && (line.back() == '\n' || line.back() == '\r' || line.back() == ' ')) {
        line.remove_suffix(1);
    }
    size_t a = 0;
    while (a < line.size() && (line[a] == ' ' || line[a] == '\t')) a++;
    line.remove_prefix(a);
    if (line.empty() || line[0] == '#') return TrackLine::Blank;

    json::Value v;
    if (!json::parse(line, &v, err) || !v.is_object()) {
        if (err && err->empty()) *err = "not a JSON object";
        return TrackLine::Invalid;
    }
    std::string kind;
    if (v.get_string("track", &kind)) {
        TrackHeader h = *header;
        read_int(v, "w", &h.width);
        read_int(v, "h", &h.height);
        double r;
        bool m;
        if (v.get_number("rotate", &r) && valid_rotation(static_cast<int>(r))) {
            h.orientation.rotate = static_cast<int>(r);
            h.has_orientation = true;
        }
        if (v.get_bool("mirror", &m)) {
            h.orientation.mirror = m;
            h.has_orientation = true;
        }
        *header = h;
        return TrackLine::Header;
    }

    FrameResult f;
    if (!v.get_number("t", &f.t) || !std::isfinite(f.t)) {
        if (err) *err = "frame without \"t\"";
        return TrackLine::Invalid;
    }
    f.width = header->width;
    f.height = header->height;
    read_int(v, "w", &f.width);
    read_int(v, "h", &f.height);
    if (const json::Value *hands = v.get("hands")) {
        if (hands->type != json::Value::Type::Array) {
            if (err) *err = "\"hands\" is no array";
            return TrackLine::Invalid;
        }
        f.hand_ran = true;
        for (const json::Value &hv : hands->arr) {
            HandDetection h;
            float box[4];
            if (!hv.is_object() || !read_numbers(hv.get("box"), box, 4)) {
                if (err) *err = "hand without a 4-number \"box\"";
                return TrackLine::Invalid;
            }
            h.x1 = box[0];
            h.y1 = box[1];
            h.x2 = box[2];
            h.y2 = box[3];
            double s;
            if (hv.get_number("score", &s)) h.score = static_cast<float>(s);
            float kp[2 * kNumKeypoints];
            if (const json::Value *k = hv.get("kp")) {
                if (!read_numbers(k, kp, 2 * kNumKeypoints)) {
                    if (err) *err = "\"kp\" must have 42 numbers";
                    return TrackLine::Invalid;
                }
                h.has_keypoints = true;
                for (int i = 0; i < kNumKeypoints; i++) h.kp[i] = {kp[2 * i], kp[2 * i + 1]};
            }
            f.hands.push_back(h);
        }
    }
    bool face;
    if (v.get_bool("face", &face)) {
        f.face_ran = true;
        f.face_seen = face;
    }
    *frame = std::move(f);
    return TrackLine::Frame;
}

}  // namespace av
