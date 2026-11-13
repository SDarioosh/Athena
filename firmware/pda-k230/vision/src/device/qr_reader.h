// QR codes (the hub's pairing code) from one camera plane, scaled down to a small grey
// image in memory. Backend chosen at build time: zxing-cpp (default, package zxing-cpp
// of the SDK), OpenCV's QRCodeDetector (needs opencv4 objdetect), or none.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace av::dev {

class QrReader {
public:
    static bool available();
    static const char *backend();
    // plane: one channel of the frame, width x height bytes. True if a code was read.
    bool decode(const uint8_t *plane, int width, int height, int max_width, std::string *text);

private:
    std::vector<uint8_t> grey_;
};

}  // namespace av::dev
