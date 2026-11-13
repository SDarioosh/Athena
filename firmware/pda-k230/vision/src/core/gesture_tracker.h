// Turns per-frame hand observations into the debounced `gesture` events and the throttled
// `hand` cursor stream of Contract 2 (docs/k230-pda-plan.md).
//
// Time-driven: every decision uses the observation timestamps, never a frame count, so it
// behaves the same at 5 or 30 frames per second and when frames are dropped. Positions are
// normalised display coordinates (0..1, already rotated and mirrored: x grows to the right
// of the screen, y downwards).
//
// Swipes: the hand's centre moved at least swipe_min_dist along one axis within
// swipe_window_s, at swipe_min_speed or faster, and swipe_axis_ratio times more along that
// axis than across it. One event per swipe: the motion history is cleared when it fires,
// no swipe fires for swipe_refractory_s, and the opposite direction (the return stroke) is
// blocked for swipe_return_block_s.
//
// Holds: the debounced shape (a new shape counts after shape_switch_s) kept with the hand
// still (within hold_max_move of where the hold began) for the shape's hold time fires
// once: palm (open), fist, thumbs_up, point. A hold fires again only after the shape
// changed or the hand left; point also after the hand moved and settled again. No hold
// fires within hold_after_swipe_s of a swipe.
//
// Cursor: an exponentially smoothed centre (cursor_smooth_tau_s), sent at most every
// cursor_min_interval_s while a hand is seen, when it moved by cursor_min_move, its shape
// changed or cursor_keepalive_s passed; `none` once when the hand is gone for hand_lost_s.
#pragma once

#include <deque>
#include <optional>
#include <string>
#include <vector>

#include "types.h"

namespace av {

struct TrackerConfig {
    // cursor
    double cursor_min_interval_s = 0.1;
    double cursor_smooth_tau_s = 0.07;
    float cursor_min_move = 0.004f;
    double cursor_keepalive_s = 1.0;
    // hand presence
    double hand_lost_s = 0.5;
    double max_gap_s = 0.5;
    // swipes
    double swipe_window_s = 0.6;
    float swipe_min_dist_x = 0.22f;
    float swipe_min_dist_y = 0.20f;
    float swipe_min_speed = 0.8f;       // normalised units per second
    float swipe_axis_ratio = 1.8f;
    double swipe_refractory_s = 0.6;
    double swipe_return_block_s = 1.0;
    // shapes and holds
    double shape_switch_s = 0.15;
    float shape_min_conf = 0.55f;
    double palm_hold_s = 0.8;
    double fist_hold_s = 0.5;
    double thumbs_up_hold_s = 0.3;
    double point_hold_s = 0.3;
    float hold_max_move = 0.06f;
    double hold_after_swipe_s = 0.6;
};

struct Observation {
    double t = 0.0;            // seconds, monotonic
    bool hand = false;         // a hand is seen in this frame
    float x = 0.0f, y = 0.0f;  // centre, normalised display coordinates
    Shape shape = Shape::Other;
    float conf = 0.0f;         // shape confidence 0..1
};

struct TrackerEvent {
    enum class Kind { Gesture, Hand } kind = Kind::Hand;
    std::string gesture;       // Gesture: swipe_left ... point
    float conf = 0.0f;         // Gesture
    float x = 0.0f, y = 0.0f;  // Hand
    Shape shape = Shape::None; // Hand
};

class GestureTracker {
public:
    explicit GestureTracker(const TrackerConfig &cfg = TrackerConfig()) : cfg_(cfg) {}

    // One frame. Events are appended to out.
    void update(const Observation &o, std::vector<TrackerEvent> &out);
    // Time passing without frames (camera stalled): sends `none` once the hand is lost.
    void tick(double t, std::vector<TrackerEvent> &out);
    // Forget everything (mode change, reconnect). If the cursor was shown, sends `none`.
    void reset(std::vector<TrackerEvent> *out = nullptr);

    bool hand_visible() const { return visible_; }
    const TrackerConfig &config() const { return cfg_; }

private:
    struct Sample { double t; float x, y; };
    enum Dir { kNone = -1, kLeft, kRight, kUp, kDown };

    void lose_hand(std::vector<TrackerEvent> &out);
    void clear_motion();
    void update_shape(const Observation &o);
    void check_swipe(const Observation &o, std::vector<TrackerEvent> &out);
    void check_hold(const Observation &o, std::vector<TrackerEvent> &out);
    void update_cursor(const Observation &o, std::vector<TrackerEvent> &out);
    double hold_time(Shape s) const;

    TrackerConfig cfg_;

    bool visible_ = false;      // a hand is being tracked
    double last_seen_t_ = 0.0;
    double last_obs_t_ = -1e9;
    std::deque<Sample> hist_;

    // swipe state
    double last_swipe_t_ = -1e9;
    Dir last_swipe_dir_ = kNone;

    // shape debounce
    Shape stable_ = Shape::Other;
    Shape cand_ = Shape::Other;
    bool has_cand_ = false;
    double cand_since_ = 0.0;
    float conf_sum_ = 0.0f;
    int conf_n_ = 0;

    // hold
    bool hold_active_ = false;
    double hold_since_ = 0.0;
    float hold_x_ = 0.0f, hold_y_ = 0.0f;
    bool hold_fired_ = false;

    // cursor
    bool cursor_init_ = false;
    float sx_ = 0.0f, sy_ = 0.0f;
    double cursor_t_ = 0.0;
    bool cursor_shown_ = false;
    double last_emit_t_ = -1e9;
    float ex_ = 0.0f, ey_ = 0.0f;
    Shape eshape_ = Shape::None;
};

}  // namespace av
