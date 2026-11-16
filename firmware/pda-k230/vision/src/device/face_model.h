// "Is there a face?" for presence, with the face detector the image already has
// (face_detection_320.kmodel of the face_detect package, a RetinaFace-style detector,
// 320 x 320). Only the per-anchor face scores are read: no boxes, no landmarks, nothing
// about who it is.
#pragma once

#include <string>

#include "kmodel.h"

namespace av::dev {

class FaceDetector {
public:
    bool init(const std::string &model, FrameTensor &frame, float thresh, std::string *err);
    // Highest face score in the frame (0..1); *seen = score >= thresh.
    bool run(FrameTensor &frame, bool *seen, float *best, std::string *err);

private:
    KModel model_;
    Letterbox lb_;
    float thresh_ = 0.6f;
};

}  // namespace av::dev
