#include "mode.h"

#include <algorithm>

namespace av {

namespace {
double interval(double fps) { return fps > 0.0 ? 1.0 / fps : 1.0; }
}

Plan plan_for(const Mode &mode, const PlanConfig &cfg) {
    Plan p;
    if (!mode.any()) return p;
    p.camera_on = true;
    p.hand = mode.gestures;
    p.face = mode.presence;
    p.qr = mode.qr;
    if (mode.gestures) {
        p.frame_interval_s = interval(cfg.gestures_max_fps);
    } else if (mode.qr) {
        p.frame_interval_s = interval(cfg.qr_fps);
    } else {
        p.frame_interval_s = interval(cfg.presence_fps);
    }
    // Alone, face and QR run on every processed frame; beside gestures, at their interval.
    p.face_interval_s = mode.presence ? std::max(p.frame_interval_s,
                                                 mode.gestures || mode.qr ? cfg.face_interval_s
                                                                          : 0.0)
                                      : 0.0;
    p.qr_interval_s = mode.qr ? std::max(p.frame_interval_s,
                                         mode.gestures ? cfg.qr_interval_s : 0.0)
                              : 0.0;
    return p;
}

}  // namespace av
