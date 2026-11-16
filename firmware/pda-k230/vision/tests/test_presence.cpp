// Presence debounce: 2 s to appear, 20 s to leave, missed detections tolerated.
#include "presence.h"
#include "testlib.h"

namespace {

// Feeds a detector result every dt from t0 to t1; returns the changes with their times.
std::vector<std::pair<double, bool>> feed(av::Presence &p, double t0, double t1, double dt,
                                          bool seen, int miss_every = 0) {
    std::vector<std::pair<double, bool>> ch;
    int i = 0;
    for (double t = t0; t < t1 - 1e-9; t += dt, i++) {
        bool s = seen && !(miss_every && i % miss_every == miss_every - 1);
        if (auto c = p.update(t, s)) ch.push_back({t, *c});
    }
    return ch;
}

}  // namespace

TEST(appears_after_two_seconds) {
    for (double dt : {1.0 / 3, 0.2, 1.0 / 30}) {
        av::Presence p;
        auto ch = feed(p, 0.0, 5.0, dt, true);
        CHECK_EQ(ch.size(), size_t(1));
        CHECK(ch[0].second);
        CHECK(ch[0].first >= 2.0 - 1e-6 && ch[0].first <= 2.0 + dt + 1e-6);
        CHECK(p.present());
    }
}

TEST(brief_sighting_is_not_presence) {
    av::Presence p;
    auto ch = feed(p, 0.0, 1.5, 0.2, true);
    CHECK(ch.empty());
    ch = feed(p, 1.5, 10.0, 0.2, false);
    CHECK(ch.empty());
    CHECK(!p.present());
}

TEST(missed_detections_dont_restart_the_count) {
    av::Presence p;
    auto ch = feed(p, 0.0, 3.0, 0.2, true, 3);  // every third frame missed
    CHECK_EQ(ch.size(), size_t(1));
    CHECK(ch[0].first <= 2.3);
}

TEST(a_long_gap_restarts_the_count) {
    av::Presence p;
    feed(p, 0.0, 1.5, 0.2, true);
    feed(p, 1.5, 3.0, 0.2, false);       // 1.5 s gap > seen_gap_s
    auto ch = feed(p, 3.0, 6.0, 0.2, true);
    CHECK_EQ(ch.size(), size_t(1));
    CHECK(ch[0].first >= 5.0 - 1e-6);    // 2 s after the new streak began
}

TEST(leaves_after_twenty_seconds) {
    av::Presence p;
    feed(p, 0.0, 3.0, 0.2, true);
    CHECK(p.present());
    auto ch = feed(p, 3.0, 40.0, 0.2, false);
    CHECK_EQ(ch.size(), size_t(1));
    CHECK(!ch[0].second);
    // last seen at 2.8: leave at 22.8
    CHECK(ch[0].first >= 22.8 - 1e-6 && ch[0].first <= 23.0 + 1e-6);
}

TEST(short_absence_keeps_presence) {
    av::Presence p;
    feed(p, 0.0, 3.0, 0.2, true);
    auto ch = feed(p, 3.0, 15.0, 0.2, false);
    CHECK(ch.empty());
    ch = feed(p, 15.0, 20.0, 0.2, true);
    CHECK(ch.empty());
    CHECK(p.present());
}

TEST(tick_without_frames_leaves) {
    av::Presence p;
    feed(p, 0.0, 3.0, 0.5, true);
    CHECK(p.present());
    CHECK(!p.tick(10.0).has_value());
    auto c = p.tick(22.6);
    CHECK(c.has_value() && !*c);
    CHECK(!p.tick(30.0).has_value());
}

TEST(reset_and_bad_times) {
    av::Presence p;
    feed(p, 0.0, 3.0, 0.5, true);
    p.reset();
    CHECK(!p.present());
    CHECK(!p.update(NAN, true).has_value());
    CHECK(!p.tick(INFINITY).has_value());
    // clock going backwards doesn't flip presence by itself
    feed(p, 100.0, 103.0, 0.5, true);
    CHECK(p.present());
    CHECK(!p.update(50.0, true).has_value());
    CHECK(p.present());
}

TEST_MAIN()
