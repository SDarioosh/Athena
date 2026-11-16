// The engine end to end with synthetic camera-frame results: mode handling, state,
// orientation, gestures, presence, QR, disconnects, garbage.
#include <algorithm>
#include <random>

#include "engine.h"
#include "json.h"
#include "synth.h"
#include "testlib.h"

using av::json::Value;

namespace {

struct Msg {
    std::string t, g, shape, camera, text;
    bool present = false;
    double fps = -1;
};

std::vector<Msg> parse_all(const std::vector<std::string> &lines) {
    std::vector<Msg> out;
    for (const std::string &l : lines) {
        CHECK(l.size() <= av::kMaxLine && !l.empty() && l.back() == '\n');
        Value v;
        CHECK(av::json::parse(l.substr(0, l.size() - 1), &v));
        Msg m;
        v.get_string("t", &m.t);
        v.get_string("g", &m.g);
        v.get_string("shape", &m.shape);
        v.get_string("camera", &m.camera);
        v.get_string("text", &m.text);
        v.get_bool("present", &m.present);
        v.get_number("fps", &m.fps);
        out.push_back(m);
    }
    return out;
}

std::vector<std::string> gestures(const std::vector<Msg> &ms) {
    std::vector<std::string> g;
    for (auto &m : ms) {
        if (m.t == "gesture") g.push_back(m.g);
    }
    return g;
}

std::string join(const std::vector<std::string> &v) {
    std::string s;
    for (auto &x : v) s += x + " ";
    return s;
}

av::Engine connected(const av::Config &cfg, const char *mode_line) {
    av::Engine e(cfg);
    e.on_connect(0.0);
    e.on_line(mode_line, 0.0);
    return e;
}

std::vector<Msg> run_frames(av::Engine &e, const std::vector<av::FrameResult> &frames,
                            double tail = 1.0) {
    std::vector<std::string> lines;
    for (auto &f : frames) {
        e.on_frame(f);
        for (auto &l : e.take_output()) lines.push_back(l);
    }
    if (!frames.empty()) {
        e.tick(frames.back().t + tail);
        for (auto &l : e.take_output()) lines.push_back(l);
    }
    return parse_all(lines);
}

const char *kGestures = "{\"t\":\"mode\",\"gestures\":true,\"presence\":false,\"qr\":false}\n";

}  // namespace

TEST(connect_sends_state_and_mode_sets_plan) {
    av::Engine e{av::Config()};
    CHECK(!e.plan().camera_on);
    e.on_connect(1.0);
    auto ms = parse_all(e.take_output());
    CHECK_EQ(ms.size(), size_t(1));
    CHECK(ms[0].t == "state" && ms[0].camera == "off");
    e.on_line(kGestures, 1.0);
    CHECK(e.plan().camera_on && e.plan().hand && !e.plan().face);
    e.on_line("{\"t\":\"mode\",\"gestures\":false,\"presence\":false,\"qr\":false}", 2.0);
    CHECK(!e.plan().camera_on);
    e.on_line("garbage", 2.0);
    e.on_line("{\"t\":\"other\"}", 2.0);
    CHECK(!e.plan().camera_on);
    CHECK(e.take_output().empty());
}

TEST(state_on_change_and_every_five_seconds) {
    av::Engine e = connected(av::Config(), kGestures);
    e.take_output();
    e.on_camera(av::CameraState::On, "", 0.0);
    auto ms = parse_all(e.take_output());
    CHECK(ms.size() == 1 && ms[0].camera == "on");
    e.on_camera(av::CameraState::On, "", 0.5);  // no change: nothing
    CHECK(e.take_output().empty());
    int states = 0;
    double fps = 0;
    for (int i = 1; i <= 15 * 12; i++) {  // 12 s at 15 fps
        av::FrameResult f;
        f.t = i / 15.0;
        f.width = 1280;
        f.height = 720;
        f.hand_ran = true;
        e.on_frame(f);
        for (auto &m : parse_all(e.take_output())) {
            if (m.t == "state") {
                states++;
                fps = m.fps;
            }
        }
    }
    CHECK_EQ(states, 2);  // at 5 s and 10 s
    CHECK_NEAR(fps, 15.0, 0.5);
    e.on_camera(av::CameraState::Error, "camera gone", 13.0);
    ms = parse_all(e.take_output());
    CHECK(ms.size() == 1 && ms[0].camera == "error" && ms[0].fps == 0);
}

TEST(swipes_through_every_orientation) {
    for (int rot : {0, 90, 180, 270}) {
        for (bool mir : {false, true}) {
            av::Config cfg;
            cfg.orientation = {rot, mir};
            for (const char *d : {"left", "right", "up", "down"}) {
                av::Engine e = connected(cfg, kGestures);
                e.take_output();
                synth::TrackSpec spec;
                spec.fps = 15;
                auto frames = synth::make_frames(synth::swipe_segments(d), spec, 1280, 720, cfg.orientation);
                auto g = gestures(run_frames(e, frames));
                CHECK_MSG(g.size() == 1 && g[0] == std::string("swipe_") + d,
                          "rot " + std::to_string(rot) + " mirror " + std::to_string(mir) + " " + d +
                              ": " + join(g));
            }
        }
    }
}

TEST(holds_through_the_engine) {
    struct C { av::Shape s; const char *g; };
    for (C c : {C{av::Shape::Open, "palm"}, C{av::Shape::Fist, "fist"},
                C{av::Shape::ThumbsUp, "thumbs_up"}, C{av::Shape::Point, "point"}}) {
        for (av::Classifier cl : {av::Classifier::Geometry, av::Classifier::Canaan}) {
            av::Config cfg;
            cfg.classifier = cl;
            av::Engine e = connected(cfg, kGestures);
            e.take_output();
            synth::TrackSpec spec;
            spec.fps = 15;
            std::vector<synth::Segment> segs = {{1.5, 0.5f, 0.5f, 0.5f, 0.5f, c.s}};
            auto ms = run_frames(e, synth::make_frames(segs, spec, 1280, 720, cfg.orientation));
            auto g = gestures(ms);
            CHECK_MSG(g.size() == 1 && g[0] == c.g,
                      std::string(c.g) + (cl == av::Classifier::Canaan ? " canaan: " : ": ") + join(g));
            bool shape_seen = false;
            for (auto &m : ms) {
                if (m.t == "hand" && m.shape == av::shape_name(c.s)) shape_seen = true;
            }
            CHECK(shape_seen);
            CHECK(ms.back().t == "hand" && ms.back().shape == "none");
        }
    }
}

TEST(presence_from_faces_and_hands) {
    av::Config cfg;
    av::Engine e = connected(cfg, "{\"t\":\"mode\",\"presence\":true}");
    e.take_output();
    std::vector<Msg> ms;
    for (int i = 0; i < 9; i++) {  // 3 fps for 3 s: face seen
        av::FrameResult f;
        f.t = 10 + i / 3.0;
        f.width = 1280;
        f.height = 720;
        f.face_ran = true;
        f.face_seen = true;
        e.on_frame(f);
        for (auto &m : parse_all(e.take_output())) ms.push_back(m);
    }
    CHECK(ms.size() == 1 && ms[0].t == "presence" && ms[0].present);
    e.tick(40.0);
    ms = parse_all(e.take_output());
    CHECK(ms.size() == 1 && ms[0].t == "presence" && !ms[0].present);

    // with gestures too, a hand counts as a person even when no face is found
    e.on_line("{\"t\":\"mode\",\"gestures\":true,\"presence\":true}", 50);
    synth::TrackSpec spec;
    spec.fps = 15;
    spec.t0 = 50;
    auto frames = synth::make_frames({{3.0, 0.5f, 0.5f, 0.5f, 0.5f, av::Shape::Other}}, spec, 1280, 720,
                                     cfg.orientation);
    bool present = false;
    for (auto &m : run_frames(e, frames, 0.1)) present |= m.t == "presence" && m.present;
    CHECK(present);
}

TEST(presence_off_sends_nothing) {
    av::Engine e = connected(av::Config(), kGestures);
    e.take_output();
    for (int i = 0; i < 100; i++) {
        av::FrameResult f;
        f.t = i * 0.1;
        f.width = 640;
        f.height = 480;
        f.face_ran = true;
        f.face_seen = true;
        e.on_frame(f);
    }
    for (auto &m : parse_all(e.take_output())) CHECK(m.t != "presence");
}

TEST(qr_once_while_in_view) {
    av::Engine e = connected(av::Config(), "{\"t\":\"mode\",\"qr\":true}");
    e.take_output();
    std::vector<Msg> ms;
    auto frame = [&](double t, const std::string &text) {
        av::FrameResult f;
        f.t = t;
        f.width = 1280;
        f.height = 720;
        f.qr_ran = true;
        f.qr_text = text;
        e.on_frame(f);
        for (auto &m : parse_all(e.take_output())) ms.push_back(m);
    };
    for (int i = 0; i < 50; i++) frame(i * 0.1, "odysseus-pair:v1?url=https://hub.example&code=1");
    CHECK_EQ(ms.size(), size_t(1));
    frame(5.1, "another code");
    CHECK_EQ(ms.size(), size_t(2));
    frame(5.2, "");
    frame(20.0, "another code");  // out of view for longer than qr_repeat_s: again
    CHECK_EQ(ms.size(), size_t(3));
    CHECK(ms[0].t == "qr" && ms[2].text == "another code");
    // qr off: decoded texts are not sent
    e.on_line("{\"t\":\"mode\",\"gestures\":true}", 21);
    frame(22.0, "x");
    CHECK_EQ(ms.size(), size_t(3));
}

TEST(gestures_off_hides_the_cursor) {
    av::Config cfg;
    av::Engine e = connected(cfg, kGestures);
    e.take_output();
    synth::TrackSpec spec;
    spec.fps = 15;
    auto frames = synth::make_frames({{1.0, 0.5f, 0.5f, 0.5f, 0.5f, av::Shape::Other}}, spec, 1280, 720,
                                     cfg.orientation);
    for (auto &f : frames) e.on_frame(f);
    e.take_output();
    e.on_line("{\"t\":\"mode\",\"presence\":true}", frames.back().t);
    auto ms = parse_all(e.take_output());
    CHECK(ms.size() == 1 && ms[0].t == "hand" && ms[0].shape == "none");
    for (auto &f : frames) e.on_frame(f);  // hand frames while gestures are off
    for (auto &m : parse_all(e.take_output())) CHECK(m.t != "hand" && m.t != "gesture");
}

TEST(disconnect_stops_the_camera_and_output) {
    av::Engine e = connected(av::Config(), kGestures);
    CHECK(e.plan().camera_on);
    e.on_disconnect(5);
    CHECK(!e.plan().camera_on);
    av::FrameResult f;
    f.t = 6;
    f.width = 100;
    f.height = 100;
    f.hand_ran = true;
    e.on_frame(f);
    e.on_camera(av::CameraState::Off, "", 6);
    CHECK(e.take_output().empty());
    e.on_connect(7);
    auto ms = parse_all(e.take_output());
    CHECK(ms.size() == 1 && ms[0].t == "state");
    CHECK(!e.plan().camera_on);  // until the UI sends a mode
}

TEST(follows_the_same_hand) {
    // A small hand is seen first and then moves right fast; a bigger one appears on the
    // left and stays. The engine keeps following the first: a swipe right. (Jumping to
    // the bigger hand would look like a swipe left.)
    av::Config cfg;
    cfg.orientation = {0, false};
    av::Engine e = connected(cfg, kGestures);
    e.take_output();
    std::vector<Msg> ms;
    std::mt19937 rng(1);
    for (int i = 0; i < 30; i++) {
        av::FrameResult f;
        f.t = i / 15.0;
        f.width = 1000;
        f.height = 1000;
        f.hand_ran = true;
        auto add = [&](float cx, float cy, float palm) {
            synth::HandSpec s;
            s.kind = av::Shape::Other;
            s.center = {cx, cy};
            s.palm_px = palm;
            av::HandDetection h;
            h.kp = synth::make_hand(s, rng);
            h.has_keypoints = true;
            float x1, y1, x2, y2;
            synth::box_of(h.kp, &x1, &y1, &x2, &y2);
            float hw = (x2 - x1) / 2, hh = (y2 - y1) / 2;
            h.x1 = cx - hw; h.x2 = cx + hw; h.y1 = cy - hh; h.y2 = cy + hh;
            f.hands.push_back(h);
        };
        float x = 400.0f + 60.0f * static_cast<float>(std::min(std::max(i - 10, 0), 9));
        add(x, 500, 50);                    // the first hand, small
        if (i >= 10) add(150, 500, 120);    // a bigger one on the left
        e.on_frame(f);
        for (auto &m : parse_all(e.take_output())) ms.push_back(m);
    }
    auto g = gestures(ms);
    CHECK_MSG(g.size() == 1 && g[0] == "swipe_right", join(g));
}

TEST(garbage_frames_never_crash) {
    std::mt19937 rng(77);
    std::uniform_real_distribution<float> u(-5000.0f, 5000.0f);
    av::Config cfg;
    av::Engine e = connected(cfg, "{\"t\":\"mode\",\"gestures\":true,\"presence\":true,\"qr\":true}");
    for (int i = 0; i < 5000; i++) {
        av::FrameResult f;
        f.t = i % 97 == 0 ? NAN : i * 0.03;
        f.width = i % 13 == 0 ? 0 : 1280;
        f.height = i % 17 == 0 ? -5 : 720;
        f.hand_ran = i % 2;
        f.face_ran = i % 3 == 0;
        f.face_seen = i % 5 == 0;
        f.qr_ran = true;
        if (i % 11 == 0) f.qr_text = std::string(static_cast<size_t>(i % 1500), 'q');
        for (int k = 0; k < i % 4; k++) {
            av::HandDetection h;
            h.x1 = u(rng); h.y1 = u(rng); h.x2 = u(rng); h.y2 = u(rng);
            if (k == 2) h.x2 = NAN;
            h.has_keypoints = k != 1;
            for (auto &p : h.kp) p = {u(rng), u(rng)};
            if (k == 3) h.kp[4].x = INFINITY;
            f.hands.push_back(h);
        }
        e.on_frame(f);
        parse_all(e.take_output());  // every line valid and within the limit
    }
}

TEST_MAIN()
