// Keypoint track lines: round trip, header handling, malformed lines, no pixels or QR.
#include <random>

#include "synth.h"
#include "testlib.h"
#include "track.h"

TEST(header_round_trip) {
    av::TrackHeader h;
    h.width = 1280;
    h.height = 720;
    h.has_orientation = true;
    h.orientation = {270, false};
    av::TrackHeader back;
    av::FrameResult f;
    CHECK(av::decode_track_line(av::encode_track_header(h), &back, &f) == av::TrackLine::Header);
    CHECK(back.width == 1280 && back.height == 720 && back.has_orientation);
    CHECK(back.orientation.rotate == 270 && !back.orientation.mirror);
}

TEST(frame_round_trip_without_qr) {
    std::mt19937 rng(2);
    av::FrameResult f;
    f.t = 12.3456;
    f.width = 1280;
    f.height = 720;
    f.hand_ran = true;
    av::HandDetection h;
    h.kp = synth::make_hand(synth::HandSpec(), rng);
    h.has_keypoints = true;
    h.x1 = 10.04f; h.y1 = 20; h.x2 = 300; h.y2 = 400.5f;
    h.score = 0.8765f;
    f.hands.push_back(h);
    av::HandDetection nokp;
    nokp.x2 = nokp.y2 = 5;
    f.hands.push_back(nokp);
    f.face_ran = true;
    f.face_seen = true;
    f.qr_ran = true;
    f.qr_text = "secret pairing code";
    std::string line = av::encode_track_frame(f);
    CHECK(line.find("secret") == std::string::npos);
    CHECK(line.find("qr") == std::string::npos);
    av::TrackHeader hd;
    hd.width = 1280;
    hd.height = 720;
    av::FrameResult b;
    CHECK(av::decode_track_line(line, &hd, &b) == av::TrackLine::Frame);
    CHECK_NEAR(b.t, 12.346, 1e-9);
    CHECK(b.width == 1280 && b.height == 720 && b.hand_ran && b.face_ran && b.face_seen);
    CHECK(!b.qr_ran && b.qr_text.empty());
    CHECK_EQ(b.hands.size(), size_t(2));
    CHECK(b.hands[0].has_keypoints && !b.hands[1].has_keypoints);
    CHECK_NEAR(b.hands[0].score, 0.877, 1e-6);
    for (int i = 0; i < av::kNumKeypoints; i++) {
        CHECK_NEAR(b.hands[0].kp[i].x, h.kp[i].x, 0.051);
        CHECK_NEAR(b.hands[0].kp[i].y, h.kp[i].y, 0.051);
    }
}

TEST(frame_variants) {
    av::TrackHeader hd;
    av::FrameResult f;
    CHECK(av::decode_track_line("{\"t\":1.5}", &hd, &f) == av::TrackLine::Frame);
    CHECK(!f.hand_ran && !f.face_ran && f.width == 0);
    CHECK(av::decode_track_line("{\"t\":1.5,\"w\":640,\"h\":480,\"hands\":[]}", &hd, &f) == av::TrackLine::Frame);
    CHECK(f.hand_ran && f.hands.empty() && f.width == 640);
    CHECK(av::decode_track_line("   ", &hd, &f) == av::TrackLine::Blank);
    CHECK(av::decode_track_line("# comment", &hd, &f) == av::TrackLine::Blank);
}

TEST(malformed_lines) {
    const char *bad[] = {
        "{", "[]", "{\"hands\":[]}", "{\"t\":\"x\"}", "{\"t\":1,\"hands\":{}}",
        "{\"t\":1,\"hands\":[{\"box\":[1,2,3]}]}", "{\"t\":1,\"hands\":[{\"box\":[1,2,3,\"4\"]}]}",
        "{\"t\":1,\"hands\":[{\"box\":[1,2,3,4],\"kp\":[1,2]}]}", "{\"t\":1,\"hands\":[5]}",
    };
    for (const char *b : bad) {
        av::TrackHeader hd;
        av::FrameResult f;
        std::string err;
        CHECK_MSG(av::decode_track_line(b, &hd, &f, &err) == av::TrackLine::Invalid, b);
        CHECK(!err.empty());
    }
    std::mt19937 rng(5);
    std::uniform_int_distribution<int> byte(32, 126);
    std::string good = "{\"t\":1,\"hands\":[{\"box\":[1,2,3,4],\"score\":0.5}],\"face\":true}";
    for (int i = 0; i < 20000; i++) {
        std::string s = good;
        s[static_cast<size_t>(i) % s.size()] = static_cast<char>(byte(rng));
        av::TrackHeader hd;
        av::FrameResult f;
        av::decode_track_line(s, &hd, &f);
    }
}

TEST_MAIN()
