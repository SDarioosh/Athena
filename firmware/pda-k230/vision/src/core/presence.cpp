#include "presence.h"

#include <cmath>

namespace av {

namespace {
constexpr double kEps = 1e-9;
}

std::optional<bool> Presence::tick(double t) {
    if (!std::isfinite(t)) return std::nullopt;
    if (streak_ && t - last_seen_ > cfg_.seen_gap_s + kEps) streak_ = false;
    if (present_ && t - last_seen_ >= cfg_.leave_s - kEps) {
        present_ = false;
        return false;
    }
    return std::nullopt;
}

std::optional<bool> Presence::update(double t, bool seen) {
    if (!std::isfinite(t)) return std::nullopt;
    if (t < last_seen_ - kEps) {
        // clock went backwards: count from here
        streak_ = false;
        last_seen_ = t;
    }
    if (!seen) return tick(t);
    if (!streak_ || t - last_seen_ > cfg_.seen_gap_s + kEps) {
        streak_ = true;
        streak_start_ = t;
    }
    last_seen_ = t;
    if (!present_ && t - streak_start_ >= cfg_.appear_s - kEps) {
        present_ = true;
        return true;
    }
    return std::nullopt;
}

}  // namespace av
