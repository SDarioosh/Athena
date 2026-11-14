// Checks on what the camera driver hands over, before a byte of a frame is read: the
// negotiated format (the driver may change the size or the format VIDIOC_S_FMT asked for)
// and each dequeued buffer's length. Pure; tests/test_frame_check.cpp.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace av {

// The bytes of a planar 3-channel frame (3 x height x width); 0 for a size out of range.
size_t planar3_bytes(int width, int height);

// Why the format the driver settled on (VIDIOC_G_FMT) can't be used, or "" when it is the
// one asked for and its image size holds a whole frame.
std::string check_format(uint32_t fourcc, uint32_t want_fourcc, uint32_t width, uint32_t height,
                         uint32_t want_width, uint32_t want_height, uint32_t sizeimage);

// A dequeued buffer holds a whole frame: its length (the mapped buffer) and, when the
// driver reports it (non-zero), its bytesused are at least planar3_bytes.
bool frame_fits(size_t length, size_t bytesused, int width, int height);

}  // namespace av
