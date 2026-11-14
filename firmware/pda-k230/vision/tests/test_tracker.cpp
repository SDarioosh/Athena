// The gesture tracker on synthetic observation tracks: swipes at 8, 15 and 30 fps with
// jitter and dropped frames, one event per swipe, return strokes, drift, holds, cursor.
#include <map>

#include "gesture_tracker.h"
#include "synth.h"
#include "testlib.h"

using av::Shape;

namespace {

struct Run {
    std::vector<av::TrackerEvent> ev;
    std::vector<std::string> gestures() const {
        std::vector<std::string> g;
        for (auto &e : ev) {
            if (e.kind == av::TrackerEvent::Kind::Gesture) g.push_back(e.gesture);
        }
        return g;
    }
    std::vector<double> hand_times;  // filled by run()
};

Run run(const std::vector<av::Observation> &obs, const av::TrackerConfig &cfg = av::TrackerConfig(),
        double tail_s = 1.0) {
    av::GestureTracker tr(cfg);
    Run r;
    for (const auto &o : obs) {
        size_t before = r.ev.size();
        tr.update(o, r.ev);
        for (size_t i = before; i < r.ev.size(); i++) {
            if (r.ev[i].kind == av::TrackerEvent::Kind::Hand) r.hand_times.push_back(o.t);
        }
    }
    if (!obs.empty()) tr.tick(obs.back().t + tail_s, r.ev);
    return r;
}

std::string join(const std::vector<std::string> &v) {
    std::string s;
    for (auto &x : v) s += x + " ";
    return s;
}

}  // namespace

TEST(swipes_every_direction_fps_jitter_drops) {
    const char *dirs[] = {"left", "right", "up", "down"};
    int runs = 0;
    for (double fps : {8.0, 15.0, 30.0}) {
        for (const char *d : dirs) {
            for (uint32_t seed = 1; seed <= 12; seed++) {
                synth::TrackSpec spec;
                spec.fps = fps;
                spec.jitter_s = 0.2 / fps;
                spec.pos_noise = 0.004f;
                spec.drop_prob = seed % 3 == 0 ? 0.15 : 0.0;
                spec.seed = seed;
                auto obs = synth::make_track(synth::swipe_segments(d, 0.3, 0.45f), spec);
                auto g = run(obs).gestures();
                std::string want = std::string("swipe_") + d;
                CHECK_MSG(g.size() == 1 && g[0] == want,
                          "fps " + std::to_string(fps) + " seed " + std::to_string(seed) + " " +
                              want + ": got " + join(g));
                runs++;
            }
        }
    }
    CHECK_EQ(runs, 3 * 4 * 12);
}

TEST(slow_swipe_at_5fps) {
    // 5 fps: a 0.4 s swipe is two or three frames.
    synth::TrackSpec spec;
    spec.fps = 5.0;
    auto obs = synth::make_track(synth::swipe_segments("right", 0.4, 0.5f), spec);
    auto g = run(obs).gestures();
    CHECK_MSG(g.size() == 1 && g[0] == "swipe_right", join(g));
}

TEST(return_stroke_is_ignored) {
    for (double fps : {8.0, 15.0, 30.0}) {
        for (const char *d : {"left", "right", "up", "down"}) {
            synth::TrackSpec spec;
            spec.fps = fps;
            spec.jitter_s = 0.1 / fps;
            auto obs = synth::make_track(synth::swipe_segments(d, 0.3, 0.45f, true), spec);
            auto g = run(obs).gestures();
            CHECK_MSG(g.size() == 1 && g[0] == std::string("swipe_") + d,
                      std::string(d) + " fps " + std::to_string(fps) + ": " + join(g));
        }
    }
}

TEST(two_swipes_same_direction_both_fire) {
    // left, back right slowly (not a swipe), then left again after the refractory period
    std::vector<synth::Segment> s = {
        {0.3, 0.75f, 0.5f, 0.75f, 0.5f, Shape::Other},
        {0.3, 0.75f, 0.5f, 0.3f, 0.5f, Shape::Other},
        {1.2, 0.3f, 0.5f, 0.75f, 0.5f, Shape::Other},  // slow return: 0.375 u/s
        {0.3, 0.75f, 0.5f, 0.3f, 0.5f, Shape::Other},
        {0.5, 0.3f, 0.5f, 0.3f, 0.5f, Shape::Other, false},
    };
    for (double fps : {8.0, 15.0, 30.0}) {
        synth::TrackSpec spec;
        spec.fps = fps;
        auto g = run(synth::make_track(s, spec)).gestures();
        CHECK_MSG(g.size() == 2 && g[0] == "swipe_left" && g[1] == "swipe_left", join(g));
    }
}

TEST(no_double_fire_on_long_fast_swipe) {
    // one long sweep over the whole frame in 0.6 s
    std::vector<synth::Segment> s = {
        {0.3, 0.05f, 0.5f, 0.05f, 0.5f, Shape::Other},
        {0.6, 0.05f, 0.5f, 0.95f, 0.5f, Shape::Other},
        {0.5, 0.95f, 0.5f, 0.95f, 0.5f, Shape::Other, false},
    };
    for (double fps : {8.0, 15.0, 30.0}) {
        synth::TrackSpec spec;
        spec.fps = fps;
        auto g = run(synth::make_track(s, spec)).gestures();
        CHECK_MSG(g.size() == 1 && g[0] == "swipe_right", join(g));
    }
}

TEST(slow_drift_is_not_a_swipe) {
    std::vector<synth::Segment> s = {
        {3.0, 0.2f, 0.4f, 0.7f, 0.6f, Shape::Other},  // 0.17 u/s
        {2.0, 0.7f, 0.6f, 0.4f, 0.3f, Shape::Other},
        {0.5, 0.4f, 0.3f, 0.4f, 0.3f, Shape::Other, false},
    };
    for (double fps : {5.0, 8.0, 15.0, 30.0}) {
        synth::TrackSpec spec;
        spec.fps = fps;
        spec.pos_noise = 0.006f;
        spec.jitter_s = 0.2 / fps;
        auto g = run(synth::make_track(s, spec)).gestures();
        CHECK_MSG(g.empty(), join(g));
    }
}

TEST(short_flick_is_not_a_swipe) {
    std::vector<synth::Segment> s = {
        {0.3, 0.5f, 0.5f, 0.5f, 0.5f, Shape::Other},
        {0.1, 0.5f, 0.5f, 0.62f, 0.5f, Shape::Other},  // fast but only 0.12
        {0.6, 0.62f, 0.5f, 0.62f, 0.5f, Shape::Other},
    };
    synth::TrackSpec spec;
    spec.fps = 30;
    CHECK(run(synth::make_track(s, spec)).gestures().empty());
}

TEST(diagonal_is_not_a_swipe) {
    std::vector<synth::Segment> s = {
        {0.3, 0.3f, 0.3f, 0.3f, 0.3f, Shape::Other},
        {0.3, 0.3f, 0.3f, 0.7f, 0.7f, Shape::Other},
        {0.3, 0.7f, 0.7f, 0.7f, 0.7f, Shape::Other},
    };
    synth::TrackSpec spec;
    spec.fps = 30;
    CHECK(run(synth::make_track(s, spec)).gestures().empty());
}

TEST(hand_appearing_elsewhere_is_not_a_swipe) {
    // the hand leaves on the left and shows up on the right: no motion was seen
    std::vector<synth::Segment> s = {
        {0.5, 0.2f, 0.5f, 0.2f, 0.5f, Shape::Other},
        {0.6, 0.2f, 0.5f, 0.2f, 0.5f, Shape::Other, false},
        {0.5, 0.8f, 0.5f, 0.8f, 0.5f, Shape::Other},
    };
    synth::TrackSpec spec;
    spec.fps = 15;
    CHECK(run(synth::make_track(s, spec)).gestures().empty());
}

TEST(holds_fire_once_after_their_time) {
    struct Case { Shape s; const char *g; double hold; };
    av::TrackerConfig cfg;
    const Case cases[] = {
        {Shape::Open, "palm", cfg.palm_hold_s},
        {Shape::Fist, "fist", cfg.fist_hold_s},
        {Shape::ThumbsUp, "thumbs_up", cfg.thumbs_up_hold_s},
        {Shape::Point, "point", cfg.point_hold_s},
    };
    for (const Case &c : cases) {
        for (double fps : {5.0, 8.0, 15.0, 30.0}) {
            synth::TrackSpec spec;
            spec.fps = fps;
            spec.pos_noise = 0.003f;
            spec.jitter_s = 0.2 / fps;
            std::vector<synth::Segment> segs = {{2.5, 0.5f, 0.5f, 0.5f, 0.5f, c.s}};
            auto obs = synth::make_track(segs, spec);
            av::GestureTracker tr(cfg);
            std::vector<av::TrackerEvent> ev;
            double first = obs.front().t, fired_at = -1;
            int n = 0;
            for (auto &o : obs) {
                size_t b = ev.size();
                tr.update(o, ev);
                for (size_t i = b; i < ev.size(); i++) {
                    if (ev[i].kind == av::TrackerEvent::Kind::Gesture) {
                        n++;
                        fired_at = o.t;
                        CHECK(ev[i].gesture == c.g);
                        CHECK(ev[i].conf > 0.5f && ev[i].conf <= 1.0f);
                    }
                }
            }
            CHECK_MSG(n == 1, std::string(c.g) + " fired " + std::to_string(n) + " times at " +
                                  std::to_string(fps));
            double delay = fired_at - first;
            // not before its time, and within one frame (plus jitter) after it
            CHECK_MSG(delay >= c.hold - 0.03 && delay <= c.hold + 1.5 / fps + 0.05,
                      std::string(c.g) + " delay " + std::to_string(delay) + " at " + std::to_string(fps));
        }
    }
}

TEST(hold_needs_stillness) {
    // an open hand moving steadily (not fast enough for a swipe) never makes a palm
    std::vector<synth::Segment> s = {{2.0, 0.2f, 0.5f, 0.8f, 0.5f, Shape::Open}};
    synth::TrackSpec spec;
    spec.fps = 15;
    CHECK(run(synth::make_track(s, spec)).gestures().empty());
}

TEST(short_shape_glitches_dont_break_a_hold) {
    // a palm held 1.2 s with a one-frame "other" every 0.3 s still fires palm once
    synth::TrackSpec spec;
    spec.fps = 15;
    auto obs = synth::make_track({{1.2, 0.5f, 0.5f, 0.5f, 0.5f, Shape::Open}}, spec);
    for (size_t i = 3; i < obs.size(); i += 5) obs[i].shape = Shape::Other;
    auto g = run(obs).gestures();
    CHECK_MSG(g.size() == 1 && g[0] == "palm", join(g));
}

TEST(low_confidence_shape_is_other) {
    synth::TrackSpec spec;
    spec.fps = 15;
    spec.conf = 0.3f;
    auto obs = synth::make_track({{2.0, 0.5f, 0.5f, 0.5f, 0.5f, Shape::Open}}, spec);
    CHECK(run(obs).gestures().empty());
}

TEST(hold_rearms_after_shape_change) {
    std::vector<synth::Segment> s = {
        {1.0, 0.5f, 0.5f, 0.5f, 0.5f, Shape::Fist},
        {0.5, 0.5f, 0.5f, 0.5f, 0.5f, Shape::Other},
        {1.0, 0.5f, 0.5f, 0.5f, 0.5f, Shape::Fist},
    };
    synth::TrackSpec spec;
    spec.fps = 15;
    auto g = run(synth::make_track(s, spec)).gestures();
    CHECK_MSG(g.size() == 2 && g[0] == "fist" && g[1] == "fist", join(g));
}

TEST(point_refires_after_moving_and_settling) {
    std::vector<synth::Segment> s = {
        {0.6, 0.3f, 0.3f, 0.3f, 0.3f, Shape::Point},
        {0.6, 0.3f, 0.3f, 0.5f, 0.45f, Shape::Point},  // move to another item (slowly)
        {0.6, 0.5f, 0.45f, 0.5f, 0.45f, Shape::Point},
    };
    synth::TrackSpec spec;
    spec.fps = 15;
    auto g = run(synth::make_track(s, spec)).gestures();
    CHECK_MSG(g.size() == 2 && g[0] == "point" && g[1] == "point", join(g));
}

TEST(palm_swipe_is_a_swipe_not_a_palm) {
    // an open hand swiped: only the swipe, even though the hand was open the whole time
    std::vector<synth::Segment> s = {
        {0.3, 0.75f, 0.5f, 0.75f, 0.5f, Shape::Open},
        {0.3, 0.75f, 0.5f, 0.3f, 0.5f, Shape::Open},
        {0.5, 0.3f, 0.5f, 0.3f, 0.5f, Shape::Open, false},
    };
    synth::TrackSpec spec;
    spec.fps = 30;
    auto g = run(synth::make_track(s, spec)).gestures();
    CHECK_MSG(g.size() == 1 && g[0] == "swipe_left", join(g));
}

TEST(cursor_throttled_and_none_once) {
    for (double fps : {8.0, 15.0, 30.0, 60.0}) {
        std::vector<synth::Segment> s = {
            {3.0, 0.2f, 0.2f, 0.8f, 0.7f, Shape::Open},   // moving all the time
            {1.0, 0.8f, 0.7f, 0.8f, 0.7f, Shape::Open, false},
        };
        synth::TrackSpec spec;
        spec.fps = fps;
        spec.jitter_s = 0.2 / fps;
        auto obs = synth::make_track(s, spec);
        Run r = run(obs);
        int none = 0, hands = 0;
        for (auto &e : r.ev) {
            if (e.kind != av::TrackerEvent::Kind::Hand) continue;
            if (e.shape == Shape::None) none++;
            else hands++;
            CHECK(e.x >= 0 && e.x <= 1 && e.y >= 0 && e.y <= 1);
        }
        CHECK_EQ(none, 1);
        // at most 10 per second: in any 1 s window, at most 10 + the none
        for (size_t i = 0; i < r.hand_times.size(); i++) {
            size_t j = i;
            while (j < r.hand_times.size() && r.hand_times[j] < r.hand_times[i] + 1.0) j++;
            CHECK_MSG(j - i <= 11, "window has " + std::to_string(j - i) + " at fps " + std::to_string(fps));
        }
        // The interval is strict, so the rate is fps / ceil(fps * 0.1): 8 at 8 fps,
        // 7.5 at 15, 10 at 30 and 60. Allow for jitter.
        double rate = fps / std::ceil(fps * 0.1 - 1e-9);
        CHECK_MSG(hands >= 0.8 * 3.0 * rate, std::to_string(hands) + " at fps " + std::to_string(fps));
        CHECK(r.ev.back().kind == av::TrackerEvent::Kind::Hand && r.ev.back().shape == Shape::None);
    }
}

TEST(cursor_is_smoothed_and_quiet_when_still) {
    synth::TrackSpec spec;
    spec.fps = 30;
    spec.pos_noise = 0.003f;
    auto obs = synth::make_track({{3.0, 0.5f, 0.5f, 0.5f, 0.5f, Shape::Other}}, spec);
    Run r = run(obs, av::TrackerConfig(), 0.0);
    int hands = 0;
    for (auto &e : r.ev) {
        if (e.kind == av::TrackerEvent::Kind::Hand) {
            hands++;
            CHECK_NEAR(e.x, 0.5, 0.01);
            CHECK_NEAR(e.y, 0.5, 0.01);
        }
    }
    // still hand: the first, a few while settling, keepalives once a second
    CHECK_MSG(hands <= 8, std::to_string(hands));
    CHECK(hands >= 3);
}

TEST(cursor_reports_shape_changes) {
    std::vector<synth::Segment> s = {
        {1.0, 0.5f, 0.5f, 0.5f, 0.5f, Shape::Open},
        {1.0, 0.5f, 0.5f, 0.5f, 0.5f, Shape::Fist},
    };
    synth::TrackSpec spec;
    spec.fps = 15;
    Run r = run(synth::make_track(s, spec), av::TrackerConfig(), 0.0);
    bool saw_open = false, saw_fist = false;
    for (auto &e : r.ev) {
        if (e.kind != av::TrackerEvent::Kind::Hand) continue;
        if (e.shape == Shape::Open) saw_open = true;
        if (e.shape == Shape::Fist) saw_fist = true;
    }
    CHECK(saw_open && saw_fist);
}

TEST(flicker_does_not_lose_the_hand) {
    // detections missing for 0.3 s now and then: no `none` until the hand really goes
    synth::TrackSpec spec;
    spec.fps = 15;
    auto obs = synth::make_track({{3.0, 0.5f, 0.5f, 0.5f, 0.5f, Shape::Other}}, spec);
    for (size_t i = 0; i < obs.size(); i++) {
        if (i % 15 >= 10 && i % 15 < 14) obs[i].hand = false;
    }
    av::GestureTracker tr;
    std::vector<av::TrackerEvent> ev;
    for (auto &o : obs) tr.update(o, ev);
    int none = 0;
    for (auto &e : ev) none += e.kind == av::TrackerEvent::Kind::Hand && e.shape == Shape::None;
    CHECK_EQ(none, 0);
    tr.tick(obs.back().t + 0.6, ev);
    CHECK(ev.back().shape == Shape::None);
}

TEST(stalled_camera_tick_sends_none) {
    av::GestureTracker tr;
    std::vector<av::TrackerEvent> ev;
    av::Observation o;
    o.t = 10.0;
    o.hand = true;
    o.x = o.y = 0.5f;
    tr.update(o, ev);
    CHECK_EQ(ev.size(), size_t(1));
    tr.tick(10.2, ev);
    CHECK_EQ(ev.size(), size_t(1));
    tr.tick(10.6, ev);
    CHECK_EQ(ev.size(), size_t(2));
    CHECK(ev[1].shape == Shape::None);
    tr.tick(12.0, ev);
    CHECK_EQ(ev.size(), size_t(2));  // only once
}

TEST(reset_sends_none_if_shown) {
    av::GestureTracker tr;
    std::vector<av::TrackerEvent> ev;
    tr.reset(&ev);
    CHECK(ev.empty());
    av::Observation o;
    o.t = 1.0;
    o.hand = true;
    o.x = o.y = 0.4f;
    tr.update(o, ev);
    tr.reset(&ev);
    CHECK_EQ(ev.size(), size_t(2));
    CHECK(ev[1].shape == Shape::None);
    CHECK(!tr.hand_visible());
}

TEST(garbage_observations_dont_crash) {
    av::GestureTracker tr;
    std::vector<av::TrackerEvent> ev;
    std::mt19937 rng(7);
    std::uniform_real_distribution<float> u(-2.0f, 3.0f);
    double t = 0;
    for (int i = 0; i < 20000; i++) {
        av::Observation o;
        int k = i % 17;
        t += k == 3 ? -0.5 : (k == 5 ? 10.0 : 0.033);
        o.t = k == 7 ? NAN : t;
        o.hand = (i % 3) != 0;
        o.x = k == 11 ? NAN : u(rng);
        o.y = k == 13 ? INFINITY : u(rng);
        o.shape = static_cast<Shape>(i % 6);
        o.conf = k == 2 ? NAN : u(rng);
        tr.update(o, ev);
        if (i % 50 == 0) tr.tick(t, ev);
    }
    for (auto &e : ev) {
        if (e.kind == av::TrackerEvent::Kind::Hand) {
            CHECK(e.x >= 0 && e.x <= 1 && e.y >= 0 && e.y <= 1);
        } else {
            CHECK(e.conf >= 0 && e.conf <= 1);
        }
    }
}

TEST_MAIN()
