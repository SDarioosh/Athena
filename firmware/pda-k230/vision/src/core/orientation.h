// From camera pixels to display orientation.
//
// rotate (0, 90, 180, 270, clockwise) turns the camera image the way it must turn to look
// upright on the screen; mirror then flips it left-right, as a user-facing camera needs
// (moving the hand to the right moves right on screen). Which values the board needs is
// not confirmed yet (docs/k230-pda-plan.md, open question 1): both are config settings.
#pragma once

#include "types.h"

namespace av {

struct Orientation {
    int rotate = 0;      // 0, 90, 180, 270
    bool mirror = true;
};

bool valid_rotation(int deg);

// Size of the frame after rotation.
void display_size(const Orientation &o, int cam_w, int cam_h, int *disp_w, int *disp_h);

// A camera pixel position to a display pixel position.
Pt to_display_px(const Orientation &o, Pt cam, int cam_w, int cam_h);
// The inverse: a display pixel position back to camera pixels.
Pt from_display_px(const Orientation &o, Pt disp, int cam_w, int cam_h);

}  // namespace av
