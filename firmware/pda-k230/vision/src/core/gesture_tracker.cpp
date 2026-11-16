#include "gesture_tracker.h"

#include <algorithm>
#include <cmath>

namespace av {

namespace {

constexpr double kEps = 1e-9;

float clamp01(float v) {
    if (!(v >= 0.0f)) return 0.0f;  // also NaN
    return v > 1.0f ? 1.0f : v;
}

const char *dir_name(int d) {
    switch (d) {
    case 0: return "swipe_left";
    case 1: return "swipe_right";
    case 2: return "swipe_up";
    case 3: return "swipe_down";
    }
    return "";
}

int opposite(int d) {
    switch (d) {
    case 0: return 1;
    case 1: return 0;
    case 2: return 3;
    case 3: return 2;
    }
    return -1;
}

const char *hold_name(Shape s) {
    switch (s) {
    case Shape::Open: return "palm";
    case Shape::Fist: return "fist";
    case Shape::ThumbsUp: return "thumbs_up";
    case Shape::Point: return "point";
    default: return nullptr;
    }
}

}  // namespace

double GestureTracker::hold_time(Shape s) const {
    switch (s) {
    case Shape::Open: return cfg_.palm_hold_s;
    case Shape::Fist: return cfg_.fist_hold_s;
    case Shape::ThumbsUp: return cfg_.thumbs_up_hold_s;
    case Shape::Point: return cfg_.point_hold_s;
    default: return -1.0;
    }
}

void GestureTracker::clear_motion() { hist_.clear(); }

void GestureTracker::reset(std::vector<TrackerEvent> *out) {
    if (out && cursor_shown_) {
        TrackerEvent e;
        e.kind = TrackerEvent::Kind::Hand;
        e.x = ex_;
        e.y = ey_;
        e.shape = Shape::None;
        out->push_back(e);
    }
    *this = GestureTracker(cfg_);
}

void GestureTracker::lose_hand(std::vector<TrackerEvent> &out) {
    if (cursor_shown_) {
        TrackerEvent e;
        e.kind = TrackerEvent::Kind::Hand;
        e.x = ex_;
        e.y = ey_;
        e.shape = Shape::None;
        out.push_back(e);
    }
    visible_ = false;
    clear_motion();
    hold_active_ = false;
    hold_fired_ = false;
    has_cand_ = false;
    cursor_init_ = false;
    cursor_shown_ = false;
    eshape_ = Shape::None;
    // last_swipe_t_ / last_swipe_dir_ survive: a hand that swiped out of view and comes
    // back the other way is the return stroke.
}

void GestureTracker::tick(double t, std::vector<TrackerEvent> &out) {
    if (visible_ && t - last_seen_t_ >= cfg_.hand_lost_s - kEps) lose_hand(out);
}

void GestureTracker::update(const Observation &o, std::vector<TrackerEvent> &out) {
    if (!std::isfinite(o.t)) return;
    if (o.t < last_obs_t_ - kEps) {
        // The clock went backwards: start over, but take the cursor down first.
        lose_hand(out);
        last_obs_t_ = o.t;
    }
    if (visible_ && o.t - last_obs_t_ > cfg_.max_gap_s + kEps) {
        clear_motion();
        hold_active_ = false;
    }
    last_obs_t_ = o.t;

    if (visible_ && o.t - last_seen_t_ >= cfg_.hand_lost_s - kEps) lose_hand(out);
    if (!o.hand || !std::isfinite(o.x) || !std::isfinite(o.y)) return;

    Observation ob = o;
    ob.x = clamp01(o.x);
    ob.y = clamp01(o.y);
    if (!std::isfinite(ob.conf)) ob.conf = 0.0f;

    if (!visible_) {
        visible_ = true;
        stable_ = ob.conf >= cfg_.shape_min_conf ? ob.shape : Shape::Other;
        if (stable_ == Shape::None) stable_ = Shape::Other;
        has_cand_ = false;
        conf_sum_ = 0.0f;
        conf_n_ = 0;
        hold_active_ = false;
        hold_fired_ = false;
        clear_motion();
    }
    last_seen_t_ = ob.t;

    update_shape(ob);
    hist_.push_back({ob.t, ob.x, ob.y});
    while (!hist_.empty() && ob.t - hist_.front().t > cfg_.swipe_window_s + kEps) {
        hist_.pop_front();
    }
    check_swipe(ob, out);
    check_hold(ob, out);
    update_cursor(ob, out);
}

void GestureTracker::update_shape(const Observation &o) {
    Shape s = o.conf >= cfg_.shape_min_conf ? o.shape : Shape::Other;
    if (s == Shape::None) s = Shape::Other;
    if (s == stable_) {
        has_cand_ = false;
        conf_sum_ += o.conf;
        conf_n_++;
        return;
    }
    if (has_cand_ && cand_ == s) {
        if (o.t - cand_since_ >= cfg_.shape_switch_s - kEps) {
            stable_ = s;
            has_cand_ = false;
            hold_active_ = false;
            hold_fired_ = false;
            conf_sum_ = o.conf;
            conf_n_ = 1;
        }
        return;
    }
    cand_ = s;
    cand_since_ = o.t;
    has_cand_ = true;
}

void GestureTracker::check_swipe(const Observation &o, std::vector<TrackerEvent> &out) {
    if (hist_.size() < 2) return;
    const Sample &now = hist_.back();
    int best_dir = kNone;
    float best_score = 0.0f;
    for (size_t i = 0; i + 1 < hist_.size(); i++) {
        const Sample &s = hist_[i];
        double dt = now.t - s.t;
        if (dt <= kEps) continue;
        float dx = now.x - s.x, dy = now.y - s.y;
        float ax = std::fabs(dx), ay = std::fabs(dy);
        if (ax >= cfg_.swipe_min_dist_x && ax >= cfg_.swipe_axis_ratio * ay &&
            ax / dt >= cfg_.swipe_min_speed) {
            float score = ax / cfg_.swipe_min_dist_x;
            if (score > best_score) {
                best_score = score;
                best_dir = dx < 0 ? kLeft : kRight;
            }
        }
        if (ay >= cfg_.swipe_min_dist_y && ay >= cfg_.swipe_axis_ratio * ax &&
            ay / dt >= cfg_.swipe_min_speed) {
            float score = ay / cfg_.swipe_min_dist_y;
            if (score > best_score) {
                best_score = score;
                best_dir = dy < 0 ? kUp : kDown;
            }
        }
    }
    if (best_dir == kNone) return;

    Sample keep = now;
    hist_.clear();
    hist_.push_back(keep);  // the motion so far is used up, fired or not

    double since = o.t - last_swipe_t_;
    if (since < cfg_.swipe_refractory_s - kEps) return;
    if (best_dir == opposite(last_swipe_dir_) && since < cfg_.swipe_return_block_s - kEps) {
        return;
    }
    TrackerEvent e;
    e.kind = TrackerEvent::Kind::Gesture;
    e.gesture = dir_name(best_dir);
    e.conf = clamp01(0.6f + 0.4f * (best_score - 1.0f));
    out.push_back(e);
    last_swipe_t_ = o.t;
    last_swipe_dir_ = static_cast<Dir>(best_dir);
    hold_active_ = false;
}

void GestureTracker::check_hold(const Observation &o, std::vector<TrackerEvent> &out) {
    const char *name = hold_name(stable_);
    if (!name) {
        hold_active_ = false;
        return;
    }
    if (o.t - last_swipe_t_ < cfg_.hold_after_swipe_s - kEps) {
        hold_active_ = false;
        return;
    }
    if (!hold_active_) {
        hold_active_ = true;
        hold_since_ = o.t;
        hold_x_ = o.x;
        hold_y_ = o.y;
    } else if (std::hypot(o.x - hold_x_, o.y - hold_y_) > cfg_.hold_max_move) {
        hold_since_ = o.t;
        hold_x_ = o.x;
        hold_y_ = o.y;
        if (stable_ == Shape::Point) hold_fired_ = false;
    }
    if (!hold_fired_ && o.t - hold_since_ >= hold_time(stable_) - kEps) {
        TrackerEvent e;
        e.kind = TrackerEvent::Kind::Gesture;
        e.gesture = name;
        e.conf = clamp01(conf_n_ > 0 ? conf_sum_ / conf_n_ : o.conf);
        out.push_back(e);
        hold_fired_ = true;
    }
}

void GestureTracker::update_cursor(const Observation &o, std::vector<TrackerEvent> &out) {
    if (!cursor_init_) {
        sx_ = o.x;
        sy_ = o.y;
        cursor_init_ = true;
        cursor_t_ = o.t;
    } else {
        double dt = std::max(0.0, o.t - cursor_t_);
        float a = cfg_.cursor_smooth_tau_s > 0.0
                      ? static_cast<float>(1.0 - std::exp(-dt / cfg_.cursor_smooth_tau_s))
                      : 1.0f;
        sx_ += a * (o.x - sx_);
        sy_ += a * (o.y - sy_);
        cursor_t_ = o.t;
    }
    double since = o.t - last_emit_t_;
    if (since < cfg_.cursor_min_interval_s - kEps) return;
    bool moved = std::hypot(sx_ - ex_, sy_ - ey_) >= cfg_.cursor_min_move;
    if (cursor_shown_ && !moved && stable_ == eshape_ && since < cfg_.cursor_keepalive_s - kEps) {
        return;
    }
    TrackerEvent e;
    e.kind = TrackerEvent::Kind::Hand;
    e.x = clamp01(sx_);
    e.y = clamp01(sy_);
    e.shape = stable_;
    out.push_back(e);
    cursor_shown_ = true;
    last_emit_t_ = o.t;
    ex_ = e.x;
    ey_ = e.y;
    eshape_ = stable_;
}

}  // namespace av
