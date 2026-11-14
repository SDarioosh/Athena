#include "frame_check.h"

namespace av {

size_t planar3_bytes(int width, int height) {
    if (width <= 0 || height <= 0 || width > 4096 || height > 4096) return 0;
    return static_cast<size_t>(width) * static_cast<size_t>(height) * 3;
}

std::string check_format(uint32_t fourcc, uint32_t want_fourcc, uint32_t width, uint32_t height,
                         uint32_t want_width, uint32_t want_height, uint32_t sizeimage) {
    if (fourcc != want_fourcc) return "camera format is not the planar BGR asked for";
    if (width != want_width || height != want_height) {
        return "camera gives " + std::to_string(width) + "x" + std::to_string(height) +
               ", not the " + std::to_string(want_width) + "x" + std::to_string(want_height) +
               " asked for";
    }
    const size_t need = planar3_bytes(static_cast<int>(width), static_cast<int>(height));
    if (need == 0) return "camera frame size out of range";
    // sizeimage 0: the driver doesn't say; each buffer's length is checked anyway
    if (sizeimage != 0 && sizeimage < need) return "camera image size is smaller than a frame";
    return "";
}

bool frame_fits(size_t length, size_t bytesused, int width, int height) {
    const size_t need = planar3_bytes(width, height);
    return need != 0 && length >= need && (bytesused == 0 || bytesused >= need);
}

}  // namespace av
