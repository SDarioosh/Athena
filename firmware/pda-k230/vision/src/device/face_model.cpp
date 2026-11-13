// Written against the nncase runtime API. Facts about the model taken from Canaan's
// k230_linux_sdk face_detect package (BSD-2-Clause, Copyright (c) 2024-2025 Canaan Bright
// Sight Co., Ltd; see README.md "Third-party"): letterbox to the top left with pad
// (104, 117, 123), and the score outputs (2 anchors x 2 classes per cell, laid out
// [anchor * 2 + class][cell], softmax, class 1 = face).
#include "face_model.h"

#include <algorithm>
#include <cmath>

namespace av::dev {

bool FaceDetector::init(const std::string &model, FrameTensor &frame, float thresh,
                        std::string *err) {
    thresh_ = thresh;
    if (!model_.load(model, err)) return false;
    if (model_.input_shape().size() != 4) {
        if (err) *err = model + ": unexpected model shape";
        return false;
    }
    const int pad[3] = {104, 117, 123};
    return lb_.init(frame, model_.input(), pad, false, err);
}

bool FaceDetector::run(FrameTensor &frame, bool *seen, float *best, std::string *err) {
    *seen = false;
    *best = 0.0f;
    if (!lb_.run(frame, model_.input(), err) || !model_.run(err)) return false;
    // Score outputs have 4 channels (2 anchors x 2 classes); box outputs 8, landmarks 20.
    int used = 0;
    for (size_t o = 0; o < model_.output_count(); o++) {
        const auto &sh = model_.output_shape(o);
        if (sh.size() != 4 || sh[1] != 4) continue;
        const float *d = model_.output(o);
        if (!d) continue;
        size_t cells = sh[2] * sh[3];
        used++;
        for (size_t c = 0; c < cells; c++) {
            for (size_t a = 0; a < 2; a++) {
                float bg = d[(a * 2 + 0) * cells + c], fg = d[(a * 2 + 1) * cells + c];
                float p = 1.0f / (1.0f + std::exp(bg - fg));
                if (p > *best) *best = p;
            }
        }
    }
    if (used == 0 && model_.output_count() >= 6) {
        // not 4-D: fall back to the demo's order (outputs 3, 4, 5 are the scores)
        for (size_t o = 3; o < 6; o++) {
            const float *d = model_.output(o);
            size_t cells = model_.output_elems(o) / 4;
            if (!d || cells == 0) continue;
            used++;
            for (size_t c = 0; c < cells; c++) {
                for (size_t a = 0; a < 2; a++) {
                    float bg = d[(a * 2 + 0) * cells + c], fg = d[(a * 2 + 1) * cells + c];
                    float p = 1.0f / (1.0f + std::exp(bg - fg));
                    if (p > *best) *best = p;
                }
            }
        }
    }
    if (used == 0) {
        if (err) *err = "face model: no score outputs found";
        return false;
    }
    *seen = *best >= thresh_;
    return true;
}

}  // namespace av::dev
