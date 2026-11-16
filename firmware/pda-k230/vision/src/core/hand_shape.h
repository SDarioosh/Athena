// Hand shape from the 21 hand keypoints: open, fist, point, thumbs_up or other.
//
// Geometry only, so it doesn't care about the hand's size, where it is, how it is turned
// in the image, or whether it is a left or a right hand. The one exception is thumbs_up,
// which needs the thumb to point up on the screen: give the keypoints in display
// orientation (rotation and mirror already applied; see orientation.h).
//
// Per digit:
//   finger (index, middle, ring, pinky) with MCP m, PIP p, DIP d, tip t, wrist w:
//     reach = |t - w| / |m - w|        (about 1.8-2.1 straight, 0.8-1.2 curled)
//     bend  = angle between (p - m) and (t - d) in degrees (0 straight, ~150+ curled)
//     extended if reach >= finger_extended_reach and bend <= finger_extended_max_bend
//     curled   if reach <= finger_curled_reach   or  bend >= finger_curled_min_bend
//   thumb, with palm = |middle MCP - wrist|:
//     spread = |thumb tip - middle MCP| / palm, bend = angle (IP - MCP) vs (tip - IP)
//     extended if spread >= thumb_extended_spread and bend <= thumb_max_bend
//     curled   if spread <= thumb_curled_spread   or  bend >= thumb_curled_min_bend
//   anything between is "unsure".
// Shapes:
//   open      no finger curled, at least 3 of 4 extended, thumb not curled
//   point     index extended, ring and pinky not extended, at least 2 of middle/ring/pinky
//             curled, middle not extended (thumb free: "gun" counts as point)
//   fist      no finger extended, at least 3 of 4 curled, thumb not extended
//   thumbs_up the fist rule, thumb extended and pointing up within thumbs_up_max_tilt
//   other     everything else, and hands too small to judge
#pragma once

#include <array>

#include "types.h"

namespace av {

struct HandShapeConfig {
    float finger_extended_reach = 1.45f;
    float finger_extended_max_bend = 55.0f;   // degrees
    float finger_curled_reach = 1.25f;
    float finger_curled_min_bend = 95.0f;     // degrees
    float thumb_extended_spread = 0.75f;
    float thumb_curled_spread = 0.55f;
    float thumb_max_bend = 45.0f;             // degrees
    float thumb_curled_min_bend = 75.0f;      // degrees
    float thumbs_up_max_tilt = 50.0f;         // degrees from straight up on the screen
    float min_palm_px = 12.0f;                // smaller hands are "other"
};

enum class Digit { Extended, Curled, Unsure };

struct ShapeResult {
    Shape shape = Shape::Other;
    float conf = 0.0f;                 // 0..1
    std::array<Digit, 5> digits{};     // thumb, index, middle, ring, pinky
};

ShapeResult classify_hand(const Keypoints &kp, const HandShapeConfig &cfg);

// True if the thumb (MCP -> tip) points up on the screen within max_tilt degrees.
bool thumb_points_up(const Keypoints &kp, float max_tilt_deg);

}  // namespace av
