// The vision core: Contract 2 messages in, per-frame detector results in, Contract 2 lines
// out. No camera, model or socket code; athena-vision (device) and the replay tool both
// drive it, and the tests drive it with synthetic frames.
#pragma once

#include <deque>
#include <string>
#include <string_view>
#include <vector>

#include "config.h"
#include "gesture_tracker.h"
#include "mode.h"
#include "presence.h"
#include "protocol.h"

namespace av {

struct HandDetection {
    float x1 = 0, y1 = 0, x2 = 0, y2 = 0;  // box, camera pixels
    float score = 0;
    bool has_keypoints = false;
    Keypoints kp{};                        // camera pixels
};

// What the detectors found in one camera frame. Never holds pixels.
struct FrameResult {
    double t = 0.0;                 // seconds, monotonic
    int width = 0, height = 0;      // camera frame size
    bool hand_ran = false;
    std::vector<HandDetection> hands;
    bool face_ran = false;
    bool face_seen = false;
    bool qr_ran = false;
    std::string qr_text;            // empty: nothing decoded
};

// The hand the engine follows in a frame, in display orientation (for logs and tests).
struct HandView {
    bool present = false;
    float x = 0, y = 0;             // normalised display coordinates
    Shape shape = Shape::Other;
    float conf = 0;
};

class Engine {
public:
    explicit Engine(const Config &cfg);

    void on_connect(double t);      // sends the current `state`
    void on_disconnect(double t);   // mode becomes all false: the camera must stop
    void on_line(std::string_view line, double t);
    void set_mode(const Mode &m, double t);
    // The device reports the camera; a change sends `state`.
    void on_camera(CameraState s, const std::string &msg, double t);
    void on_frame(const FrameResult &f);
    void tick(double t);

    const Mode &mode() const { return mode_; }
    Plan plan() const { return plan_for(mode_, cfg_.plan); }
    double fps() const { return fps_; }
    const HandView &last_hand() const { return last_hand_; }

    // Lines to send, in order (each ends with '\n').
    std::vector<std::string> take_output();

    // Shape of one hand already in display pixels, with the configured classifier.
    static void classify(const Config &cfg, const Keypoints &kp_display, Shape *shape, float *conf);

private:
    void emit(std::string line);
    void send_state(double t);
    void emit_tracker(const std::vector<TrackerEvent> &ev);
    const HandDetection *pick_hand(const FrameResult &f, float *nx, float *ny) const;

    Config cfg_;
    Mode mode_;
    GestureTracker tracker_;
    Presence presence_;
    bool connected_ = false;
    CameraState camera_ = CameraState::Off;
    std::string camera_msg_;
    double last_state_t_ = -1e9;
    std::deque<double> frame_times_;
    double fps_ = 0.0;
    std::string last_qr_;
    double last_qr_seen_ = -1e9;
    HandView last_hand_;
    std::vector<std::string> out_;
};

}  // namespace av
