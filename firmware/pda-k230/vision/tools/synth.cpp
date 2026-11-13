#include "synth.h"

#include <algorithm>
#include <cmath>

#include "../src/core/orientation.h"

namespace synth {

namespace {

constexpr float kPi = 3.14159265358979f;

Pt add(Pt a, Pt b) { return {a.x + b.x, a.y + b.y}; }
Pt mul(Pt a, float s) { return {a.x * s, a.y * s}; }
Pt norm(Pt a) {
    float l = std::hypot(a.x, a.y);
    return l > 0 ? Pt{a.x / l, a.y / l} : Pt{0, -1};
}
Pt perp(Pt a) { return {-a.y, a.x}; }

struct FingerDef {
    Pt mcp;
    float seg[3];
};

// A right hand, palm to the camera, fingers up; wrist at the origin, palm length 1.
const FingerDef kFingers[4] = {
    {{-0.32f, -0.95f}, {0.45f, 0.27f, 0.22f}},  // index
    {{-0.08f, -1.00f}, {0.50f, 0.30f, 0.23f}},  // middle
    {{0.15f, -0.95f}, {0.47f, 0.28f, 0.22f}},   // ring
    {{0.36f, -0.85f}, {0.36f, 0.22f, 0.20f}},   // pinky
};

void finger(Keypoints &k, int f, bool extended) {
    const FingerDef &d = kFingers[f];
    int base = 5 + 4 * f;
    Pt dir = norm(d.mcp);
    k[base] = d.mcp;
    if (extended) {
        k[base + 1] = add(d.mcp, mul(dir, d.seg[0]));
        k[base + 2] = add(k[base + 1], mul(dir, d.seg[1]));
        k[base + 3] = add(k[base + 2], mul(dir, d.seg[2]));
    } else {
        // Folded over the palm: the first bone points at the camera (seen short), the
        // others back towards the wrist.
        k[base + 1] = add(d.mcp, mul(dir, 0.15f));
        k[base + 2] = add(k[base + 1], add(mul(dir, -0.25f), mul(perp(dir), 0.03f)));
        k[base + 3] = add(k[base + 2], mul(dir, -0.15f));
    }
}

enum class ThumbPose { Out, Folded, Side };

void thumb(Keypoints &k, ThumbPose p) {
    k[1] = {-0.25f, -0.25f};
    switch (p) {
    case ThumbPose::Out:
        k[2] = {-0.45f, -0.42f};
        k[3] = {-0.68f, -0.60f};
        k[4] = {-0.90f, -0.75f};
        break;
    case ThumbPose::Folded:
        k[2] = {-0.40f, -0.40f};
        k[3] = {-0.30f, -0.65f};
        k[4] = {-0.10f, -0.70f};
        break;
    case ThumbPose::Side:  // straight out sideways from a fist
        k[2] = {-0.40f, -0.45f};
        k[3] = {-0.70f, -0.50f};
        k[4] = {-1.00f, -0.55f};
        break;
    }
}

}  // namespace

Keypoints make_hand(const HandSpec &s, std::mt19937 &rng) {
    Keypoints k{};
    k[0] = {0, 0};
    float base_rot = 0.0f;
    switch (s.kind) {
    case Shape::Open:
        thumb(k, ThumbPose::Out);
        for (int f = 0; f < 4; f++) finger(k, f, true);
        break;
    case Shape::Fist:
        thumb(k, ThumbPose::Folded);
        for (int f = 0; f < 4; f++) finger(k, f, false);
        break;
    case Shape::Point:
        thumb(k, s.gun ? ThumbPose::Out : ThumbPose::Folded);
        finger(k, 0, true);
        for (int f = 1; f < 4; f++) finger(k, f, false);
        break;
    case Shape::ThumbsUp:
        thumb(k, ThumbPose::Side);
        for (int f = 0; f < 4; f++) finger(k, f, false);
        base_rot = s.thumbs_down ? -90.0f : 90.0f;  // turn the thumb to point up (or down)
        break;
    default:  // victory sign
        thumb(k, ThumbPose::Folded);
        finger(k, 0, true);
        finger(k, 1, true);
        finger(k, 2, false);
        finger(k, 3, false);
        break;
    }
    // Centre the palm, mirror, rotate, scale, move, add noise.
    Pt palm_c = mul(add(k[0], k[9]), 0.5f);
    if (s.mirror) base_rot = -base_rot;  // a left hand turns the other way to thumb up
    float a = (base_rot + s.rotate_deg) * kPi / 180.0f;
    float ca = std::cos(a), sa = std::sin(a);
    std::normal_distribution<float> n(0.0f, 1.0f);
    for (Pt &p : k) {
        Pt q = {p.x - palm_c.x, p.y - palm_c.y};
        if (s.mirror) q.x = -q.x;
        Pt r = {q.x * ca - q.y * sa, q.x * sa + q.y * ca};
        p = {s.center.x + r.x * s.palm_px, s.center.y + r.y * s.palm_px};
        if (s.noise > 0) {
            p.x += n(rng) * s.noise * s.palm_px;
            p.y += n(rng) * s.noise * s.palm_px;
        }
    }
    return k;
}

void box_of(const Keypoints &kp, float *x1, float *y1, float *x2, float *y2) {
    float a = 1e9f, b = 1e9f, c = -1e9f, d = -1e9f;
    for (const Pt &p : kp) {
        a = std::min(a, p.x);
        b = std::min(b, p.y);
        c = std::max(c, p.x);
        d = std::max(d, p.y);
    }
    float m = 0.1f * std::max(c - a, d - b);
    *x1 = a - m;
    *y1 = b - m;
    *x2 = c + m;
    *y2 = d + m;
}

std::vector<av::Observation> make_track(const std::vector<Segment> &segs, const TrackSpec &spec) {
    std::vector<av::Observation> out;
    std::mt19937 rng(spec.seed);
    std::uniform_real_distribution<double> jit(-spec.jitter_s, spec.jitter_s);
    std::uniform_real_distribution<double> u01(0.0, 1.0);
    std::normal_distribution<float> pn(0.0f, 1.0f);
    double total = 0;
    for (const Segment &s : segs) total += s.duration_s;
    double dt = 1.0 / spec.fps;
    for (int i = 0;; i++) {
        double tl = i * dt;
        if (tl > total + 1e-9) break;
        double t = tl + (spec.jitter_s > 0 ? jit(rng) : 0.0);
        if (t < 0) t = 0;
        bool drop = spec.drop_prob > 0 && u01(rng) < spec.drop_prob;
        // find segment at tl
        double acc = 0;
        const Segment *seg = &segs.back();
        double f = 1.0;
        for (const Segment &s : segs) {
            if (tl <= acc + s.duration_s + 1e-12) {
                seg = &s;
                f = s.duration_s > 0 ? (tl - acc) / s.duration_s : 1.0;
                break;
            }
            acc += s.duration_s;
        }
        if (drop) continue;
        av::Observation o;
        o.t = spec.t0 + t;
        o.hand = seg->hand;
        if (o.hand) {
            o.x = static_cast<float>(seg->x0 + (seg->x1 - seg->x0) * f);
            o.y = static_cast<float>(seg->y0 + (seg->y1 - seg->y0) * f);
            if (spec.pos_noise > 0) {
                o.x += pn(rng) * spec.pos_noise;
                o.y += pn(rng) * spec.pos_noise;
            }
            o.shape = seg->shape;
            o.conf = spec.conf;
        }
        out.push_back(o);
    }
    std::sort(out.begin(), out.end(),
              [](const av::Observation &a, const av::Observation &b) { return a.t < b.t; });
    return out;
}

std::vector<Segment> swipe_segments(const std::string &dir, double swipe_s, float dist,
                                    bool with_return) {
    float x0 = 0.5f, y0 = 0.5f, x1 = 0.5f, y1 = 0.5f;
    float h = dist / 2;
    if (dir == "left") { x0 = 0.5f + h; x1 = 0.5f - h; }
    else if (dir == "right") { x0 = 0.5f - h; x1 = 0.5f + h; }
    else if (dir == "up") { y0 = 0.5f + h; y1 = 0.5f - h; }
    else { y0 = 0.5f - h; y1 = 0.5f + h; }
    std::vector<Segment> s;
    s.push_back({0.4, x0, y0, x0, y0, Shape::Other, true});     // hand appears, still
    s.push_back({swipe_s, x0, y0, x1, y1, Shape::Other, true}); // the swipe
    s.push_back({0.1, x1, y1, x1, y1, Shape::Other, true});
    if (with_return) {
        s.push_back({swipe_s * 1.3, x1, y1, x0, y0, Shape::Other, true});  // back again
        s.push_back({0.2, x0, y0, x0, y0, Shape::Other, true});
    }
    s.push_back({0.8, x0, y0, x0, y0, Shape::Other, false});    // hand gone
    return s;
}

std::vector<av::FrameResult> make_frames(const std::vector<Segment> &segs, const TrackSpec &spec,
                                         int cam_w, int cam_h, const av::Orientation &o,
                                         float palm_px) {
    std::vector<av::Observation> obs = make_track(segs, spec);
    std::vector<av::FrameResult> out;
    std::mt19937 rng(spec.seed + 7);
    int dw, dh;
    av::display_size(o, cam_w, cam_h, &dw, &dh);
    for (const av::Observation &ob : obs) {
        av::FrameResult f;
        f.t = ob.t;
        f.width = cam_w;
        f.height = cam_h;
        f.hand_ran = true;
        if (ob.hand) {
            HandSpec hs;
            hs.kind = ob.shape;
            hs.palm_px = palm_px;
            hs.center = {ob.x * dw, ob.y * dh};
            hs.noise = 0.01f;
            Keypoints disp = make_hand(hs, rng);
            av::HandDetection h;
            h.has_keypoints = true;
            h.score = 0.8f;
            for (int i = 0; i < av::kNumKeypoints; i++) {
                h.kp[i] = av::from_display_px(o, disp[i], cam_w, cam_h);
            }
            // The box is centred on the palm, as the engine uses the box centre.
            float bx1, by1, bx2, by2;
            box_of(h.kp, &bx1, &by1, &bx2, &by2);
            av::Pt c = av::from_display_px(o, hs.center, cam_w, cam_h);
            float hw = (bx2 - bx1) / 2, hh = (by2 - by1) / 2;
            h.x1 = c.x - hw;
            h.y1 = c.y - hh;
            h.x2 = c.x + hw;
            h.y2 = c.y + hh;
            f.hands.push_back(h);
        }
        out.push_back(f);
    }
    return out;
}

}  // namespace synth
