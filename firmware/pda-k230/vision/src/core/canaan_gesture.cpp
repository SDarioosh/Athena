/* The angle features and the rule table in canaan_label() follow hand_angle() and
 * h_gesture() of buildroot-overlay/package/ai_demo/sq_handkp_class/hand_keypoint.cc in
 * https://github.com/kendryte/k230_linux_sdk (rewritten here; same thresholds and rules),
 * which carries this notice:
 *
 * Copyright (c) 2025, Canaan Bright Sight Co., Ltd
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are met:
 * 1. Redistributions of source code must retain the above copyright
 * notice, this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright
 * notice, this list of conditions and the following disclaimer in the
 * documentation and/or other materials provided with the distribution.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND
 * CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
 * INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
 * MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
 * DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
 * CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
 * SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
 * BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
 * SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
 * INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
 * WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
 * NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 * OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */
#include "canaan_gesture.h"

#include <cmath>

#include "hand_shape.h"

namespace av {

namespace {

// Angle between two vectors in degrees; NaN (Canaan: 65535) if one has zero length.
double angle2d(double ax, double ay, double bx, double by) {
    double na = std::sqrt(ax * ax + ay * ay), nb = std::sqrt(bx * bx + by * by);
    if (!(na > 0.0) || !(nb > 0.0)) return std::nan("");
    double c = (ax * bx + ay * by) / (na * nb);
    if (c > 1.0) c = 1.0;
    if (c < -1.0) c = -1.0;
    return std::acos(c) * 180.0 / 3.14159265358979323846;
}

// Per digit: the angle between (wrist - second joint) and (third joint - tip).
double digit_angle(const Keypoints &k, int base) {
    // thumb: joints 2, 3, 4; fingers: PIP, DIP, tip = base + 1..3
    int a = base == 1 ? 2 : base + 1;
    int b = a + 1, t = a + 2;
    return angle2d(k[0].x - k[a].x, k[0].y - k[a].y, k[b].x - k[t].x, k[b].y - k[t].y);
}

}  // namespace

std::string canaan_label(const Keypoints &k) {
    double a[5] = {digit_angle(k, 1), digit_angle(k, 5), digit_angle(k, 9),
                   digit_angle(k, 13), digit_angle(k, 17)};
    for (double v : a) {
        if (std::isnan(v)) return "other";
    }
    const double big = 65.0, thumb_big = 53.0, small = 49.0;
    auto B = [&](int i) { return a[i] > big; };
    auto S = [&](int i) { return a[i] < small; };
    if (a[0] > thumb_big && B(1) && B(2) && B(3) && B(4)) return "fist";
    if (S(0) && S(1) && S(2) && S(3) && S(4)) return "five";
    if (S(0) && S(1) && B(2) && B(3) && B(4)) return "gun";
    if (S(0) && S(1) && B(2) && B(3) && S(4)) return "love";
    if (a[0] > 5.0 && S(1) && B(2) && B(3) && B(4)) return "one";
    if (S(0) && B(1) && B(2) && B(3) && S(4)) return "six";
    if (a[0] > thumb_big && S(1) && S(2) && S(3) && B(4)) return "three";
    if (S(0) && B(1) && B(2) && B(3) && B(4)) return "thumbUp";
    if (a[0] > thumb_big && S(1) && S(2) && B(3) && B(4)) return "yeah";
    return "other";
}

Shape shape_from_canaan_label(const std::string &label, const Keypoints &kp,
                              float thumbs_up_max_tilt_deg) {
    if (label == "five") return Shape::Open;
    if (label == "fist") return Shape::Fist;
    if (label == "one" || label == "gun") return Shape::Point;
    if (label == "thumbUp") {
        return thumb_points_up(kp, thumbs_up_max_tilt_deg) ? Shape::ThumbsUp : Shape::Other;
    }
    return Shape::Other;
}

}  // namespace av
