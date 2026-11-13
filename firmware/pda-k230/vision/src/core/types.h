// Shared value types of the vision core. Pure C++17, no device dependencies.
#pragma once

#include <array>
#include <string_view>

namespace av {

// Hand shape as sent in the `hand` message of Contract 2 (docs/k230-pda-plan.md).
enum class Shape { None, Open, Fist, Point, ThumbsUp, Other };

const char *shape_name(Shape s);                     // "none", "open", ...
bool shape_from_name(std::string_view name, Shape *out);

struct Pt {
    float x = 0.0f;
    float y = 0.0f;
};

// The 21 hand keypoints in the usual order (as the handkp_det model outputs them):
// 0 wrist; thumb 1 CMC, 2 MCP, 3 IP, 4 tip; index 5 MCP, 6 PIP, 7 DIP, 8 tip;
// middle 9-12; ring 13-16; pinky 17-20.
constexpr int kNumKeypoints = 21;
using Keypoints = std::array<Pt, kNumKeypoints>;

namespace kp {
constexpr int kWrist = 0;
constexpr int kThumbCmc = 1, kThumbMcp = 2, kThumbIp = 3, kThumbTip = 4;
constexpr int kIndexMcp = 5, kMiddleMcp = 9, kRingMcp = 13, kPinkyMcp = 17;
}  // namespace kp

}  // namespace av
