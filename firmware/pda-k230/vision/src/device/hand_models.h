// Hand detection (hand_det.kmodel, a yolov5 palm detector, 512 x 512) and hand keypoints
// (handkp_det.kmodel, 21 points, 256 x 256) on the KPU: the two models of Canaan's
// sq_handkp_class demo, used the same way (letterbox for the detector, a square crop
// around each hand for the keypoints).
#pragma once

#include <string>
#include <vector>

#include "engine.h"
#include "kmodel.h"

namespace av::dev {

class HandDetector {
public:
    bool init(const std::string &model, FrameTensor &frame, float obj_thresh, float nms_thresh,
              std::string *err);
    // Boxes in frame pixels, best first, at most max_hands.
    bool detect(FrameTensor &frame, std::vector<HandDetection> &out, size_t max_hands,
                std::string *err);

private:
    KModel model_;
    Letterbox lb_;
    float obj_ = 0.3f, nms_ = 0.4f;
};

class HandKeypointer {
public:
    bool init(const std::string &model, std::string *err);
    // Fills h.kp (frame pixels) from the region around h's box.
    bool run(FrameTensor &frame, HandDetection &h, std::string *err);

private:
    KModel model_;
};

}  // namespace av::dev
