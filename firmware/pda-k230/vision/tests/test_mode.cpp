// What runs for which mode, and the camera-to-display orientation.
#include "mode.h"
#include "orientation.h"
#include "testlib.h"

TEST(all_false_stops_the_camera) {
    av::Plan p = av::plan_for(av::Mode(), av::PlanConfig());
    CHECK(!p.camera_on && !p.hand && !p.face && !p.qr);
}

TEST(each_mode_runs_its_detectors) {
    av::PlanConfig c;
    for (int bits = 1; bits < 8; bits++) {
        av::Mode m;
        m.gestures = bits & 1;
        m.presence = bits & 2;
        m.qr = bits & 4;
        av::Plan p = av::plan_for(m, c);
        CHECK(p.camera_on);
        CHECK(p.hand == m.gestures);
        CHECK(p.face == m.presence);
        CHECK(p.qr == m.qr);
        CHECK(p.frame_interval_s > 0);
        if (m.presence) CHECK(p.face_interval_s >= p.frame_interval_s);
        if (m.qr) CHECK(p.qr_interval_s >= p.frame_interval_s);
    }
}

TEST(rates) {
    av::PlanConfig c;
    av::Mode g;
    g.gestures = true;
    CHECK_NEAR(av::plan_for(g, c).frame_interval_s, 1.0 / 30, 1e-9);
    av::Mode pr;
    pr.presence = true;
    av::Plan p = av::plan_for(pr, c);
    CHECK_NEAR(p.frame_interval_s, 1.0 / 3, 1e-9);
    CHECK_NEAR(p.face_interval_s, 1.0 / 3, 1e-9);  // alone: every processed frame
    av::Mode gp = g;
    gp.presence = true;
    gp.qr = true;
    p = av::plan_for(gp, c);
    CHECK_NEAR(p.frame_interval_s, 1.0 / 30, 1e-9);
    CHECK_NEAR(p.face_interval_s, 0.5, 1e-9);
    CHECK_NEAR(p.qr_interval_s, 0.2, 1e-9);
    av::Mode q;
    q.qr = true;
    p = av::plan_for(q, c);
    CHECK_NEAR(p.frame_interval_s, 0.1, 1e-9);
    CHECK_NEAR(p.qr_interval_s, 0.1, 1e-9);
}

TEST(orientation_round_trip_and_corners) {
    const int W = 1280, H = 720;
    for (int rot : {0, 90, 180, 270}) {
        for (bool mir : {false, true}) {
            av::Orientation o{rot, mir};
            int dw, dh;
            av::display_size(o, W, H, &dw, &dh);
            CHECK(dw * dh == W * H);
            for (av::Pt p : {av::Pt{0, 0}, av::Pt{100, 50}, av::Pt{1279, 719}, av::Pt{640, 360}}) {
                av::Pt d = av::to_display_px(o, p, W, H);
                CHECK(d.x >= 0 && d.x <= dw && d.y >= 0 && d.y <= dh);
                av::Pt back = av::from_display_px(o, d, W, H);
                CHECK_NEAR(back.x, p.x, 1e-3);
                CHECK_NEAR(back.y, p.y, 1e-3);
            }
        }
    }
    // 90 clockwise: the camera's left edge is the display's top
    av::Orientation o{90, false};
    av::Pt d = av::to_display_px(o, {0, 360}, W, H);
    CHECK_NEAR(d.y, 0, 1e-3);
    // mirror: moving right in the camera image moves left on the display
    av::Orientation m{0, true};
    CHECK(av::to_display_px(m, {100, 0}, W, H).x > av::to_display_px(m, {200, 0}, W, H).x);
    CHECK(av::valid_rotation(270) && !av::valid_rotation(45));
}

TEST_MAIN()
