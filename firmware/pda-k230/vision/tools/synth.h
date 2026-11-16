// Synthetic, fictional hand data for tests and the synth_track tool: keypoints of a drawn
// hand in a given shape, and observation tracks (swipes, holds, drift) at any frame rate
// with jitter and dropped frames. Nothing here comes from a camera.
#pragma once

#include <cstdint>
#include <random>
#include <string>
#include <vector>

#include "../src/core/engine.h"
#include "../src/core/gesture_tracker.h"
#include "../src/core/types.h"

namespace synth {

using av::Keypoints;
using av::Pt;
using av::Shape;

// What to draw. kind: Open, Fist, Point, ThumbsUp; Other draws a "victory" sign.
struct HandSpec {
    Shape kind = Shape::Open;
    bool gun = false;              // Point with the thumb out
    bool thumbs_down = false;      // ThumbsUp turned upside down (should be "other")
    float palm_px = 80.0f;         // wrist to middle-finger knuckle
    float rotate_deg = 0.0f;       // extra rotation in the image plane (clockwise)
    bool mirror = false;           // a left hand instead of a right one
    Pt center{320.0f, 240.0f};     // where the palm is
    float noise = 0.0f;            // gaussian noise, as a fraction of palm_px
};

Keypoints make_hand(const HandSpec &spec, std::mt19937 &rng);

// Box around the keypoints with a margin, as a detector would give it.
void box_of(const Keypoints &kp, float *x1, float *y1, float *x2, float *y2);

// A timeline of normalised hand positions; each segment moves linearly.
struct Segment {
    double duration_s;
    float x0, y0, x1, y1;
    Shape shape = Shape::Open;
    bool hand = true;              // false: no hand during this segment
};

struct TrackSpec {
    double fps = 15.0;
    double jitter_s = 0.0;         // random timing jitter per frame (uniform +-)
    float pos_noise = 0.0f;        // position noise (normalised, gaussian sigma)
    double drop_prob = 0.0;        // probability a frame is missing
    uint32_t seed = 1;
    float conf = 0.9f;
    double t0 = 100.0;
};

std::vector<av::Observation> make_track(const std::vector<Segment> &segs, const TrackSpec &spec);

// Typical tracks, normalised coordinates. "dir": left, right, up, down.
std::vector<Segment> swipe_segments(const std::string &dir, double swipe_s = 0.3,
                                    float dist = 0.45f, bool with_return = false);

// Camera-frame results for a track (keypoints drawn in camera pixels): for the engine
// and the replay tool. Positions are mapped back through the given orientation so the
// engine sees them in display coordinates as written in the segments.
std::vector<av::FrameResult> make_frames(const std::vector<Segment> &segs, const TrackSpec &spec,
                                         int cam_w, int cam_h, const av::Orientation &o,
                                         float palm_px = 90.0f);

}  // namespace synth
