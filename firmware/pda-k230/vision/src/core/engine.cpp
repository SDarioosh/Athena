#include "engine.h"

#include <cmath>

#include "canaan_gesture.h"
#include "hand_shape.h"
#include "orientation.h"

namespace av {

Engine::Engine(const Config &cfg) : cfg_(cfg), tracker_(cfg.tracker), presence_(cfg.presence) {}

void Engine::emit(std::string line) {
    if (connected_ && !line.empty()) out_.push_back(std::move(line));
}

std::vector<std::string> Engine::take_output() {
    std::vector<std::string> o;
    o.swap(out_);
    return o;
}

void Engine::send_state(double t) {
    emit(encode_state(camera_, camera_ == CameraState::On ? fps_ : 0.0, camera_msg_));
    last_state_t_ = t;
}

void Engine::on_connect(double t) {
    connected_ = true;
    out_.clear();
    send_state(t);
}

void Engine::on_disconnect(double t) {
    set_mode(Mode(), t);
    connected_ = false;
    out_.clear();
}

void Engine::set_mode(const Mode &m, double t) {
    std::vector<TrackerEvent> ev;
    if (mode_.gestures && !m.gestures) tracker_.reset(&ev);
    if (mode_.presence && !m.presence) presence_.reset();
    if (mode_.qr && !m.qr) {
        last_qr_.clear();
        last_qr_seen_ = -1e9;
    }
    mode_ = m;
    emit_tracker(ev);
    if (!m.gestures) last_hand_ = HandView();
    (void)t;
}

void Engine::on_line(std::string_view line, double t) {
    UiMessage msg = decode_ui_line(line);
    if (msg.kind == UiMessage::Kind::Mode) set_mode(msg.mode, t);
    // unknown and invalid lines are ignored (Contract 2: unknown types are ignored)
}

void Engine::on_camera(CameraState s, const std::string &msg, double t) {
    if (s != CameraState::On) {
        frame_times_.clear();
        fps_ = 0.0;
    }
    if (s == camera_ && msg == camera_msg_) return;
    camera_ = s;
    camera_msg_ = msg;
    send_state(t);
}

void Engine::emit_tracker(const std::vector<TrackerEvent> &ev) {
    for (const TrackerEvent &e : ev) {
        if (e.kind == TrackerEvent::Kind::Gesture) {
            emit(encode_gesture(e.gesture, e.conf));
        } else {
            emit(encode_hand(e.x, e.y, e.shape));
        }
    }
}

void Engine::classify(const Config &cfg, const Keypoints &kp, Shape *shape, float *conf) {
    if (cfg.classifier == Classifier::Canaan) {
        Shape s = shape_from_canaan_label(canaan_label(kp), kp, cfg.shape.thumbs_up_max_tilt);
        *shape = s;
        *conf = s == Shape::Other ? 0.0f : 0.8f;
        return;
    }
    ShapeResult r = classify_hand(kp, cfg.shape);
    *shape = r.shape;
    *conf = r.conf;
}

const HandDetection *Engine::pick_hand(const FrameResult &f, float *nx, float *ny) const {
    int dw, dh;
    display_size(cfg_.orientation, f.width, f.height, &dw, &dh);
    if (dw <= 0 || dh <= 0) return nullptr;
    const HandDetection *best = nullptr, *nearest = nullptr;
    float best_area = -1.0f, near_d = 1e9f;
    float bx = 0, by = 0, nnx = 0, nny = 0;
    for (const HandDetection &h : f.hands) {
        float area = (h.x2 - h.x1) * (h.y2 - h.y1);
        if (!std::isfinite(area) || area <= 0.0f) continue;
        Pt c = to_display_px(cfg_.orientation, {(h.x1 + h.x2) * 0.5f, (h.y1 + h.y2) * 0.5f},
                             f.width, f.height);
        float x = c.x / dw, y = c.y / dh;
        if (area > best_area) {
            best_area = area;
            best = &h;
            bx = x;
            by = y;
        }
        if (last_hand_.present) {
            float d = std::hypot(x - last_hand_.x, y - last_hand_.y);
            if (d < near_d) {
                near_d = d;
                nearest = &h;
                nnx = x;
                nny = y;
            }
        }
    }
    if (nearest && near_d <= cfg_.hand_switch_dist) {
        *nx = nnx;
        *ny = nny;
        return nearest;
    }
    *nx = bx;
    *ny = by;
    return best;
}

void Engine::on_frame(const FrameResult &f) {
    const double t = f.t;
    if (!std::isfinite(t)) return;
    // frame rate over the last 2 s
    frame_times_.push_back(t);
    while (frame_times_.size() > 2 && t - frame_times_.front() > 2.0) frame_times_.pop_front();
    if (frame_times_.size() >= 2 && t > frame_times_.front()) {
        fps_ = (frame_times_.size() - 1) / (t - frame_times_.front());
    }

    bool hand_seen = false;
    if (mode_.gestures && f.hand_ran && f.width > 0 && f.height > 0) {
        Observation ob;
        ob.t = t;
        float nx = 0, ny = 0;
        const HandDetection *h = pick_hand(f, &nx, &ny);
        if (h) {
            hand_seen = true;
            ob.hand = true;
            ob.x = nx;
            ob.y = ny;
            ob.shape = Shape::Other;
            ob.conf = 0.0f;
            if (h->has_keypoints) {
                Keypoints kd;
                for (int i = 0; i < kNumKeypoints; i++) {
                    kd[i] = to_display_px(cfg_.orientation, h->kp[i], f.width, f.height);
                }
                classify(cfg_, kd, &ob.shape, &ob.conf);
            }
            last_hand_ = {true, ob.x, ob.y, ob.shape, ob.conf};
        }
        std::vector<TrackerEvent> ev;
        tracker_.update(ob, ev);
        emit_tracker(ev);
        // While the tracker still follows the hand through a missed frame, keep its last
        // position for picking the same hand next time.
        if (!h) last_hand_.present = tracker_.hand_visible();
    }

    if (mode_.presence && (f.face_ran || hand_seen)) {
        auto changed = presence_.update(t, (f.face_ran && f.face_seen) || hand_seen);
        if (changed) emit(encode_presence(*changed));
    }

    if (mode_.qr && f.qr_ran && !f.qr_text.empty()) {
        if (f.qr_text != last_qr_ || t - last_qr_seen_ > cfg_.qr_repeat_s) {
            emit(encode_qr(f.qr_text));
            last_qr_ = f.qr_text;
        }
        last_qr_seen_ = t;
    }
    tick(t);
}

void Engine::tick(double t) {
    if (!std::isfinite(t)) return;
    if (mode_.gestures) {
        std::vector<TrackerEvent> ev;
        tracker_.tick(t, ev);
        emit_tracker(ev);
    }
    if (mode_.presence) {
        auto changed = presence_.tick(t);
        if (changed) emit(encode_presence(*changed));
    }
    if (camera_ == CameraState::On && t - last_state_t_ >= cfg_.state_interval_s) send_state(t);
}

}  // namespace av
