// What the camera driver hands over is checked before a frame is read.
#include "frame_check.h"
#include "testlib.h"

namespace {
constexpr uint32_t BG3P = 'B' | ('G' << 8) | ('3' << 16) | (static_cast<uint32_t>('P') << 24);
constexpr uint32_t NV12 = 'N' | ('V' << 8) | ('1' << 16) | (static_cast<uint32_t>('2') << 24);
}  // namespace

TEST(planar3_bytes) {
    CHECK(av::planar3_bytes(320, 320) == 320u * 320u * 3u);
    CHECK(av::planar3_bytes(0, 320) == 0);
    CHECK(av::planar3_bytes(320, -1) == 0);
    CHECK(av::planar3_bytes(5000, 10) == 0);
}

TEST(format_as_asked) {
    CHECK(av::check_format(BG3P, BG3P, 320, 320, 320, 320, 320 * 320 * 3).empty());
    CHECK(av::check_format(BG3P, BG3P, 320, 320, 320, 320, 0).empty());  // not reported
}

TEST(format_changed_by_the_driver) {
    CHECK(!av::check_format(NV12, BG3P, 320, 320, 320, 320, 320 * 320 * 3).empty());
    CHECK(!av::check_format(BG3P, BG3P, 640, 320, 320, 320, 640 * 320 * 3).empty());
    CHECK(!av::check_format(BG3P, BG3P, 320, 240, 320, 320, 320 * 240 * 3).empty());
    CHECK(!av::check_format(BG3P, BG3P, 320, 320, 320, 320, 320 * 320 * 3 - 1).empty());
    CHECK(!av::check_format(BG3P, BG3P, 0, 0, 0, 0, 0).empty());
}

TEST(buffer_too_small) {
    const size_t need = 320 * 320 * 3;
    CHECK(av::frame_fits(need, need, 320, 320));
    CHECK(av::frame_fits(need + 4096, 0, 320, 320));  // bytesused not reported
    CHECK(!av::frame_fits(need - 1, 0, 320, 320));
    CHECK(!av::frame_fits(need, need - 1, 320, 320));
    CHECK(!av::frame_fits(0, 0, 320, 320));
    CHECK(!av::frame_fits(need, need, 0, 320));
}

TEST_MAIN()
