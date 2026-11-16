// What runs for a `mode` message (Contract 2).
//
//   gestures  -> hand detection + keypoints on every frame, up to gestures_max_fps
//   presence  -> face detection, presence_fps on its own, every face_interval_s alongside
//                gestures (a hand seen also counts as a person)
//   qr        -> QR decoding every qr_interval_s, at qr_fps on its own
//   all false -> the camera stream is stopped (not just ignored)
#pragma once

#include "protocol.h"

namespace av {

struct PlanConfig {
    double gestures_max_fps = 30.0;
    double presence_fps = 3.0;
    double qr_fps = 10.0;
    double face_interval_s = 0.5;  // while gestures run too
    double qr_interval_s = 0.2;    // while gestures run too
};

struct Plan {
    bool camera_on = false;
    bool hand = false;
    bool face = false;
    bool qr = false;
    double frame_interval_s = 0.0;  // process at most one frame per interval
    double face_interval_s = 0.0;   // run face detection at most this often
    double qr_interval_s = 0.0;     // run the QR decoder at most this often
};

Plan plan_for(const Mode &mode, const PlanConfig &cfg);

}  // namespace av
