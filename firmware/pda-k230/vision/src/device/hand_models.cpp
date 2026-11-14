// Written against the nncase runtime API. Facts about the models taken from Canaan's
// k230_linux_sdk sq_handkp_class demo (BSD-2-Clause, Copyright (c) 2025 Canaan Bright
// Sight Co., Ltd; see README.md "Third-party"): the detector's anchors and its output layout
// (per stride: grid x grid cells x 3 anchors x [x, y, w, h, objectness, class], already
// sigmoid-activated, yolov5 box decoding), the letterbox pad value 114, the keypoint
// model's output (42 values, x/y relative to the crop) and the crop of 1.26 x half the
// box's longer side around its centre.
#include "hand_models.h"

#include <algorithm>
#include <cmath>

namespace av::dev {

namespace {

// anchors (w, h in model input pixels) for strides 8, 16, 32
const float kAnchors[3][3][2] = {
    {{26, 27}, {53, 52}, {75, 71}},
    {{80, 99}, {106, 82}, {99, 134}},
    {{140, 113}, {161, 172}, {245, 276}},
};

float iou(const HandDetection &a, const HandDetection &b) {
    float x1 = std::max(a.x1, b.x1), y1 = std::max(a.y1, b.y1);
    float x2 = std::min(a.x2, b.x2), y2 = std::min(a.y2, b.y2);
    float inter = std::max(0.0f, x2 - x1) * std::max(0.0f, y2 - y1);
    float ua = (a.x2 - a.x1) * (a.y2 - a.y1) + (b.x2 - b.x1) * (b.y2 - b.y1) - inter;
    return ua > 0 ? inter / ua : 0.0f;
}

}  // namespace

bool HandDetector::init(const std::string &model, FrameTensor &frame, float obj_thresh,
                        float nms_thresh, std::string *err) {
    obj_ = obj_thresh;
    nms_ = nms_thresh;
    if (!model_.load(model, err)) return false;
    if (model_.input_shape().size() != 4 || model_.output_count() < 3) {
        if (err) *err = model + ": unexpected model shape";
        return false;
    }
    const int pad[3] = {114, 114, 114};
    return lb_.init(frame, model_.input(), pad, true, err);
}

bool HandDetector::detect(FrameTensor &frame, std::vector<HandDetection> &out, size_t max_hands,
                          std::string *err) {
    out.clear();
    if (!lb_.run(frame, model_.input(), err) || !model_.run(err)) return false;
    const float in_size = static_cast<float>(model_.input_shape()[2]);
    const float fw = static_cast<float>(frame.width()), fh = static_cast<float>(frame.height());
    std::vector<HandDetection> cand;
    for (size_t o = 0; o < model_.output_count() && o < 3; o++) {
        const float *d = model_.output(o);
        size_t n = model_.output_elems(o);
        if (!d || n % 18 != 0) continue;
        int grid = static_cast<int>(std::lround(std::sqrt(static_cast<double>(n / 18))));
        if (grid <= 0 || static_cast<size_t>(grid) * grid * 18 != n) continue;
        float stride = in_size / static_cast<float>(grid);
        int si = stride < 12 ? 0 : (stride < 24 ? 1 : 2);
        for (int gy = 0; gy < grid; gy++) {
            for (int gx = 0; gx < grid; gx++) {
                for (int a = 0; a < 3; a++) {
                    const float *r = d + ((gy * grid + gx) * 3 + a) * 6;
                    float score = r[4] * r[5];
                    if (!(score > obj_)) continue;
                    float cx = (r[0] * 2.0f - 0.5f + static_cast<float>(gx)) * stride;
                    float cy = (r[1] * 2.0f - 0.5f + static_cast<float>(gy)) * stride;
                    float w = std::pow(r[2] * 2.0f, 2.0f) * kAnchors[si][a][0];
                    float h = std::pow(r[3] * 2.0f, 2.0f) * kAnchors[si][a][1];
                    cx = (cx - lb_.off_x) / lb_.scale;
                    cy = (cy - lb_.off_y) / lb_.scale;
                    w /= lb_.scale;
                    h /= lb_.scale;
                    // a model output of NaN or inf is no box (std::clamp keeps a NaN, and
                    // the keypoint crop casts these to int)
                    if (!std::isfinite(cx) || !std::isfinite(cy) || !std::isfinite(w) ||
                        !std::isfinite(h)) {
                        continue;
                    }
                    HandDetection b;
                    b.x1 = std::clamp(cx - w / 2, 0.0f, fw);
                    b.y1 = std::clamp(cy - h / 2, 0.0f, fh);
                    b.x2 = std::clamp(cx + w / 2, 0.0f, fw);
                    b.y2 = std::clamp(cy + h / 2, 0.0f, fh);
                    b.score = score;
                    // slivers at the frame edge are not hands
                    if (b.x2 - b.x1 < 0.03f * fw || b.y2 - b.y1 < 0.03f * fh) continue;
                    cand.push_back(b);
                }
            }
        }
    }
    std::sort(cand.begin(), cand.end(),
              [](const HandDetection &x, const HandDetection &y) { return x.score > y.score; });
    for (const HandDetection &c : cand) {
        bool keep = true;
        for (const HandDetection &k : out) {
            if (iou(c, k) >= nms_) {
                keep = false;
                break;
            }
        }
        if (keep) out.push_back(c);
        if (out.size() >= max_hands) break;
    }
    return true;
}

bool HandKeypointer::init(const std::string &model, std::string *err) {
    if (!model_.load(model, err)) return false;
    if (model_.input_shape().size() != 4 || model_.output_count() < 1 ||
        model_.output_elems(0) < 42) {
        if (err) *err = model + ": unexpected model shape";
        return false;
    }
    return true;
}

bool HandKeypointer::run(FrameTensor &frame, HandDetection &h, std::string *err) {
    const int fw = frame.width(), fh = frame.height();
    float w = h.x2 - h.x1, ht = h.y2 - h.y1;
    float half = 1.26f * std::max(w, ht) / 2.0f;
    float cx = (h.x1 + h.x2) / 2.0f, cy = (h.y1 + h.y2) / 2.0f;
    int x1 = std::max(0, static_cast<int>(cx - half));
    int y1 = std::max(0, static_cast<int>(cy - half));
    int x2 = std::min(fw - 1, static_cast<int>(cx + half));
    int y2 = std::min(fh - 1, static_cast<int>(cy + half));
    int cw = x2 - x1 + 1, ch = y2 - y1 + 1;
    if (cw <= 0 || ch <= 0) {
        if (err) *err = "keypoint crop empty";
        return false;
    }
    if (!crop_resize(frame, model_.input(), x1, y1, cw, ch, err) || !model_.run(err)) return false;
    const float *p = model_.output(0);
    if (!p) {
        if (err) *err = "keypoint output missing";
        return false;
    }
    for (int i = 0; i < 2 * kNumKeypoints; i++) {
        if (!std::isfinite(p[i])) return true;  // no keypoints this frame (has_keypoints false)
    }
    for (int i = 0; i < kNumKeypoints; i++) {
        h.kp[i].x = p[2 * i] * static_cast<float>(cw) + static_cast<float>(x1);
        h.kp[i].y = p[2 * i + 1] * static_cast<float>(ch) + static_cast<float>(y1);
    }
    h.has_keypoints = true;
    return true;
}

}  // namespace av::dev
