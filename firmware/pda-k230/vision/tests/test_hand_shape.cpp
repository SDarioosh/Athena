// Hand shapes from synthetic keypoints: every shape, scaled, rotated, mirrored, noisy.
#include <random>

#include "canaan_gesture.h"
#include "hand_shape.h"
#include "synth.h"
#include "testlib.h"

using av::Shape;

namespace {

struct Tally {
    int ok = 0, total = 0;
};

Tally sweep(Shape kind, float noise, float rot_min, float rot_max, float rot_step,
            bool gun = false, Shape expect = Shape::None) {
    if (expect == Shape::None) expect = kind;
    av::HandShapeConfig cfg;
    Tally t;
    std::mt19937 rng(1234);
    for (float palm : {25.0f, 60.0f, 150.0f, 400.0f}) {
        for (float rot = rot_min; rot <= rot_max + 1e-3f; rot += rot_step) {
            for (bool mirror : {false, true}) {
                for (int rep = 0; rep < (noise > 0 ? 4 : 1); rep++) {
                    synth::HandSpec s;
                    s.kind = kind;
                    s.gun = gun;
                    s.palm_px = palm;
                    s.rotate_deg = rot;
                    s.mirror = mirror;
                    s.center = {600.0f, 500.0f};
                    s.noise = noise;
                    av::Keypoints kp = synth::make_hand(s, rng);
                    av::ShapeResult r = av::classify_hand(kp, cfg);
                    t.total++;
                    if (r.shape == expect) t.ok++;
                }
            }
        }
    }
    return t;
}

}  // namespace

TEST(clean_shapes_all_rotations) {
    for (Shape s : {Shape::Open, Shape::Fist, Shape::Point}) {
        Tally t = sweep(s, 0.0f, 0.0f, 345.0f, 15.0f);
        CHECK_MSG(t.ok == t.total, std::string(av::shape_name(s)) + " " + std::to_string(t.ok) +
                                       "/" + std::to_string(t.total));
    }
}

TEST(gun_is_point) {
    Tally t = sweep(Shape::Point, 0.0f, -60.0f, 60.0f, 15.0f, true);
    CHECK_MSG(t.ok == t.total, std::to_string(t.ok) + "/" + std::to_string(t.total));
}

TEST(victory_is_other) {
    Tally t = sweep(Shape::Other, 0.0f, 0.0f, 345.0f, 15.0f);
    CHECK_MSG(t.ok == t.total, std::to_string(t.ok) + "/" + std::to_string(t.total));
}

TEST(thumbs_up_within_tilt) {
    Tally t = sweep(Shape::ThumbsUp, 0.0f, -40.0f, 40.0f, 10.0f);
    CHECK_MSG(t.ok == t.total, std::to_string(t.ok) + "/" + std::to_string(t.total));
}

TEST(thumbs_up_turned_too_far_is_other) {
    av::HandShapeConfig cfg;
    std::mt19937 rng(1);
    for (float rot : {80.0f, 120.0f, 180.0f, -100.0f}) {
        synth::HandSpec s;
        s.kind = Shape::ThumbsUp;
        s.rotate_deg = rot;
        av::ShapeResult r = av::classify_hand(synth::make_hand(s, rng), cfg);
        CHECK(r.shape == Shape::Other);
    }
    synth::HandSpec down;
    down.kind = Shape::ThumbsUp;
    down.thumbs_down = true;
    CHECK(av::classify_hand(synth::make_hand(down, rng), cfg).shape == Shape::Other);
}

TEST(noisy_shapes_mostly_right) {
    // 3 % of the palm length is about what the keypoint model jitters by.
    for (Shape s : {Shape::Open, Shape::Fist, Shape::Point}) {
        Tally t = sweep(s, 0.03f, 0.0f, 330.0f, 30.0f);
        CHECK_MSG(t.ok * 100 >= t.total * 97, std::string(av::shape_name(s)) + " " +
                                                  std::to_string(t.ok) + "/" + std::to_string(t.total));
    }
    Tally t = sweep(Shape::ThumbsUp, 0.03f, -30.0f, 30.0f, 10.0f);
    CHECK_MSG(t.ok * 100 >= t.total * 95, std::to_string(t.ok) + "/" + std::to_string(t.total));
}

TEST(very_noisy_never_confuses_open_and_fist) {
    av::HandShapeConfig cfg;
    std::mt19937 rng(99);
    int bad = 0;
    for (int i = 0; i < 400; i++) {
        synth::HandSpec s;
        s.kind = i % 2 ? Shape::Open : Shape::Fist;
        s.rotate_deg = static_cast<float>(i * 7 % 360);
        s.noise = 0.08f;
        Shape r = av::classify_hand(synth::make_hand(s, rng), cfg).shape;
        if ((s.kind == Shape::Open && r == Shape::Fist) || (s.kind == Shape::Fist && r == Shape::Open)) {
            bad++;
        }
    }
    CHECK_EQ(bad, 0);
}

TEST(confidence_range) {
    av::HandShapeConfig cfg;
    std::mt19937 rng(5);
    synth::HandSpec s;
    s.kind = Shape::Open;
    av::ShapeResult r = av::classify_hand(synth::make_hand(s, rng), cfg);
    CHECK(r.conf >= 0.55f && r.conf <= 1.0f);
}

TEST(tiny_or_degenerate_hands_are_other) {
    av::HandShapeConfig cfg;
    std::mt19937 rng(5);
    synth::HandSpec s;
    s.kind = Shape::Open;
    s.palm_px = 5.0f;
    CHECK(av::classify_hand(synth::make_hand(s, rng), cfg).shape == Shape::Other);
    av::Keypoints zero{};
    CHECK(av::classify_hand(zero, cfg).shape == Shape::Other);
    av::Keypoints nan_kp = synth::make_hand(synth::HandSpec(), rng);
    nan_kp[7].x = std::nanf("");
    CHECK(av::classify_hand(nan_kp, cfg).shape == Shape::Other);
    av::Keypoints inf_kp = synth::make_hand(synth::HandSpec(), rng);
    inf_kp[0].y = INFINITY;
    CHECK(av::classify_hand(inf_kp, cfg).shape == Shape::Other);
}

TEST(canaan_labels_and_mapping) {
    std::mt19937 rng(3);
    synth::HandSpec s;
    s.kind = Shape::Open;
    av::Keypoints open = synth::make_hand(s, rng);
    CHECK_EQ(av::canaan_label(open), std::string("five"));
    CHECK(av::shape_from_canaan_label("five", open, 50) == Shape::Open);
    s.kind = Shape::Fist;
    av::Keypoints fist = synth::make_hand(s, rng);
    CHECK_EQ(av::canaan_label(fist), std::string("fist"));
    s.kind = Shape::Point;
    CHECK_EQ(av::canaan_label(synth::make_hand(s, rng)), std::string("one"));
    s.gun = true;
    CHECK_EQ(av::canaan_label(synth::make_hand(s, rng)), std::string("gun"));
    CHECK(av::shape_from_canaan_label("gun", open, 50) == Shape::Point);
    CHECK(av::shape_from_canaan_label("one", open, 50) == Shape::Point);
    CHECK(av::shape_from_canaan_label("yeah", open, 50) == Shape::Other);
    CHECK(av::shape_from_canaan_label("love", open, 50) == Shape::Other);
    CHECK(av::shape_from_canaan_label("", open, 50) == Shape::Other);
    CHECK(av::shape_from_canaan_label("garbage", open, 50) == Shape::Other);
    // thumbUp only counts when the thumb points up on the screen
    synth::HandSpec up;
    up.kind = Shape::ThumbsUp;
    av::Keypoints tu = synth::make_hand(up, rng);
    CHECK(av::shape_from_canaan_label("thumbUp", tu, 50) == Shape::ThumbsUp);
    up.thumbs_down = true;
    av::Keypoints td = synth::make_hand(up, rng);
    CHECK(av::shape_from_canaan_label("thumbUp", td, 50) == Shape::Other);
    av::Keypoints zero{};
    CHECK_EQ(av::canaan_label(zero), std::string("other"));
}

TEST(shape_names_round_trip) {
    for (Shape s : {Shape::None, Shape::Open, Shape::Fist, Shape::Point, Shape::ThumbsUp, Shape::Other}) {
        Shape back = Shape::Other;
        CHECK(av::shape_from_name(av::shape_name(s), &back));
        CHECK(back == s);
    }
    Shape x;
    CHECK(!av::shape_from_name("thumbsup", &x));
}

TEST_MAIN()
