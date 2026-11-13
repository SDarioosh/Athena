// Canaan's static hand gesture rule (the sq_handkp_class demo of k230_linux_sdk) and the
// mapping of its labels onto our shapes. Used when the config says `classifier = canaan`;
// the default is our own classifier (hand_shape.h). See canaan_gesture.cpp for the notice.
#pragma once

#include <string>

#include "types.h"

namespace av {

// One of "fist", "five", "gun", "love", "one", "six", "three", "thumbUp", "yeah", "other".
std::string canaan_label(const Keypoints &kp);

// five -> open, fist -> fist, one and gun -> point, thumbUp -> thumbs_up (only if the
// thumb points up on the screen within max_tilt_deg; Canaan's rule doesn't look), every
// other label -> other. Unknown labels -> other.
Shape shape_from_canaan_label(const std::string &label, const Keypoints &kp,
                              float thumbs_up_max_tilt_deg);

}  // namespace av
