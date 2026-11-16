#include "hand_shape.h"

#include <algorithm>
#include <cmath>
#include <cstring>

namespace av {

const char *shape_name(Shape s) {
    switch (s) {
    case Shape::None: return "none";
    case Shape::Open: return "open";
    case Shape::Fist: return "fist";
    case Shape::Point: return "point";
    case Shape::ThumbsUp: return "thumbs_up";
    case Shape::Other: return "other";
    }
    return "other";
}

bool shape_from_name(std::string_view name, Shape *out) {
    static const Shape all[] = {Shape::None, Shape::Open, Shape::Fist,
                                Shape::Point, Shape::ThumbsUp, Shape::Other};
    for (Shape s : all) {
        if (name == shape_name(s)) {
            *out = s;
            return true;
        }
    }
    return false;
}

namespace {

constexpr float kPi = 3.14159265358979f;

float dist(Pt a, Pt b) { return std::hypot(a.x - b.x, a.y - b.y); }

bool finite(Pt p) { return std::isfinite(p.x) && std::isfinite(p.y); }

// Angle between two vectors in degrees, or -1 if one is too short to tell.
float angle_deg(Pt a0, Pt a1, Pt b0, Pt b1, float min_len) {
    float ax = a1.x - a0.x, ay = a1.y - a0.y;
    float bx = b1.x - b0.x, by = b1.y - b0.y;
    float la = std::hypot(ax, ay), lb = std::hypot(bx, by);
    if (la < min_len || lb < min_len) return -1.0f;
    float c = (ax * bx + ay * by) / (la * lb);
    c = std::max(-1.0f, std::min(1.0f, c));
    return std::acos(c) * 180.0f / kPi;
}

float clamp01(float v) { return std::max(0.0f, std::min(1.0f, v)); }

struct DigitInfo {
    Digit state = Digit::Unsure;
    float margin = 0.0f;  // 0..1, how clearly it is in its state
};

DigitInfo finger_state(const Keypoints &k, int mcp, float palm, const HandShapeConfig &c) {
    Pt w = k[kp::kWrist], m = k[mcp], p = k[mcp + 1], d = k[mcp + 2], t = k[mcp + 3];
    float base = dist(m, w);
    DigitInfo out;
    if (base < 0.25f * palm) return out;  // collapsed keypoints: can't tell
    float reach = dist(t, w) / base;
    float bend = angle_deg(m, p, d, t, 0.04f * palm);
    if (bend < 0.0f) {
        // A segment too short to see (finger pointing at the camera, or folded flat):
        // judge by reach alone, never as extended.
        if (reach <= c.finger_curled_reach) {
            out.state = Digit::Curled;
            out.margin = clamp01((c.finger_curled_reach - reach) / 0.3f);
        }
        return out;
    }
    if (reach >= c.finger_extended_reach && bend <= c.finger_extended_max_bend) {
        out.state = Digit::Extended;
        out.margin = std::min(clamp01((reach - c.finger_extended_reach) / 0.35f),
                              clamp01((c.finger_extended_max_bend - bend) / 35.0f));
        return out;
    }
    if (reach <= c.finger_curled_reach || bend >= c.finger_curled_min_bend) {
        out.state = Digit::Curled;
        out.margin = std::max(clamp01((c.finger_curled_reach - reach) / 0.3f),
                              clamp01((bend - c.finger_curled_min_bend) / 50.0f));
        return out;
    }
    return out;
}

DigitInfo thumb_state(const Keypoints &k, float palm, const HandShapeConfig &c) {
    DigitInfo out;
    float spread = dist(k[kp::kThumbTip], k[kp::kMiddleMcp]) / palm;
    float bend = angle_deg(k[kp::kThumbMcp], k[kp::kThumbIp], k[kp::kThumbIp],
                           k[kp::kThumbTip], 0.04f * palm);
    if (bend >= 0.0f && spread >= c.thumb_extended_spread && bend <= c.thumb_max_bend) {
        out.state = Digit::Extended;
        out.margin = std::min(clamp01((spread - c.thumb_extended_spread) / 0.3f),
                              clamp01((c.thumb_max_bend - bend) / 30.0f));
        return out;
    }
    if (spread <= c.thumb_curled_spread || bend >= c.thumb_curled_min_bend) {
        out.state = Digit::Curled;
        float m1 = clamp01((c.thumb_curled_spread - spread) / 0.25f);
        float m2 = bend >= 0.0f ? clamp01((bend - c.thumb_curled_min_bend) / 40.0f) : 0.0f;
        out.margin = std::max(m1, m2);
        return out;
    }
    return out;
}

}  // namespace

bool thumb_points_up(const Keypoints &kp, float max_tilt_deg) {
    Pt a = kp[kp::kThumbMcp], b = kp[kp::kThumbTip];
    float dx = b.x - a.x, dy = b.y - a.y;  // screen y grows downwards
    float len = std::hypot(dx, dy);
    if (!(len > 0.0f)) return false;
    float cos_up = -dy / len;
    return cos_up >= std::cos(max_tilt_deg * kPi / 180.0f);
}

ShapeResult classify_hand(const Keypoints &k, const HandShapeConfig &c) {
    ShapeResult r;
    r.digits.fill(Digit::Unsure);
    for (const Pt &p : k) {
        if (!finite(p)) return r;
    }
    float palm = dist(k[kp::kWrist], k[kp::kMiddleMcp]);
    if (!(palm >= c.min_palm_px)) return r;

    DigitInfo d[5];
    d[0] = thumb_state(k, palm, c);
    d[1] = finger_state(k, kp::kIndexMcp, palm, c);
    d[2] = finger_state(k, kp::kMiddleMcp, palm, c);
    d[3] = finger_state(k, kp::kRingMcp, palm, c);
    d[4] = finger_state(k, kp::kPinkyMcp, palm, c);
    for (int i = 0; i < 5; i++) r.digits[i] = d[i].state;

    int ext = 0, curl = 0;
    float ext_m = 0, curl_m = 0;
    for (int i = 1; i < 5; i++) {
        if (d[i].state == Digit::Extended) { ext++; ext_m += d[i].margin; }
        if (d[i].state == Digit::Curled) { curl++; curl_m += d[i].margin; }
    }
    auto conf = [](float mean_margin, int unsure) {
        return clamp01(0.55f + 0.45f * mean_margin - 0.1f * unsure);
    };

    if (curl == 0 && ext >= 3 && d[0].state != Digit::Curled) {
        r.shape = Shape::Open;
        r.conf = conf(ext_m / ext, 4 - ext);
        return r;
    }
    if (ext == 0 && curl >= 3) {
        if (d[0].state == Digit::Extended) {
            if (thumb_points_up(k, c.thumbs_up_max_tilt)) {
                r.shape = Shape::ThumbsUp;
                r.conf = conf((curl_m + d[0].margin) / (curl + 1), 4 - curl);
            }
            return r;  // a thumb out sideways or down is "other"
        }
        r.shape = Shape::Fist;
        r.conf = conf(curl_m / curl, 4 - curl);
        return r;
    }
    if (d[1].state == Digit::Extended && d[2].state != Digit::Extended &&
        d[3].state != Digit::Extended && d[4].state != Digit::Extended) {
        int c3 = (d[2].state == Digit::Curled) + (d[3].state == Digit::Curled) +
                 (d[4].state == Digit::Curled);
        if (c3 >= 2) {
            float m = d[1].margin;
            for (int i = 2; i < 5; i++) m += d[i].state == Digit::Curled ? d[i].margin : 0.0f;
            r.shape = Shape::Point;
            r.conf = conf(m / (1 + c3), 3 - c3);
            return r;
        }
    }
    return r;
}

}  // namespace av
