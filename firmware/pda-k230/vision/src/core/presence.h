// Debounced presence from per-frame "a person is seen" booleans (Contract 2 `presence`).
//
// Appear: seen for appear_s without a gap longer than seen_gap_s (so a missed detection
// now and then doesn't restart the count). Leave: not seen for leave_s. Starts absent;
// only changes are reported.
#pragma once

#include <optional>

namespace av {

struct PresenceConfig {
    double appear_s = 2.0;
    double leave_s = 20.0;
    double seen_gap_s = 1.0;
};

class Presence {
public:
    explicit Presence(const PresenceConfig &cfg = PresenceConfig()) : cfg_(cfg) {}

    // One frame where the detector ran. Returns the new state when it changes.
    std::optional<bool> update(double t, bool seen);
    // Time passing without frames (camera off or stalled).
    std::optional<bool> tick(double t);
    void reset() { *this = Presence(cfg_); }

    bool present() const { return present_; }

private:
    PresenceConfig cfg_;
    bool present_ = false;
    bool streak_ = false;
    double streak_start_ = 0.0;
    double last_seen_ = -1e9;
};

}  // namespace av
