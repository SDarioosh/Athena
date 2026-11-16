#include "qr_reader.h"

#include <algorithm>

#if defined(VISION_QR_ZXING)
#include <ZXing/ReadBarcode.h>
#elif defined(VISION_QR_OPENCV)
#include <opencv2/core.hpp>
#include <opencv2/objdetect.hpp>
#endif

namespace av::dev {

bool QrReader::available() {
#if defined(VISION_QR_ZXING) || defined(VISION_QR_OPENCV)
    return true;
#else
    return false;
#endif
}

const char *QrReader::backend() {
#if defined(VISION_QR_ZXING)
    return "zxing";
#elif defined(VISION_QR_OPENCV)
    return "opencv";
#else
    return "none";
#endif
}

bool QrReader::decode(const uint8_t *plane, int width, int height, int max_width,
                      std::string *text) {
    if (!plane || width <= 0 || height <= 0) return false;
    // Box-average down to at most max_width wide.
    int step = std::max(1, (width + std::max(1, max_width) - 1) / std::max(1, max_width));
    int dw = width / step, dh = height / step;
    if (dw < 21 || dh < 21) return false;  // smaller than the smallest QR code
    grey_.resize(static_cast<size_t>(dw) * static_cast<size_t>(dh));
    for (int y = 0; y < dh; y++) {
        for (int x = 0; x < dw; x++) {
            unsigned sum = 0;
            for (int j = 0; j < step; j++) {
                const uint8_t *row = plane + static_cast<size_t>(y * step + j) * width + x * step;
                for (int i = 0; i < step; i++) sum += row[i];
            }
            grey_[static_cast<size_t>(y) * dw + x] = static_cast<uint8_t>(sum / (step * step));
        }
    }
#if defined(VISION_QR_ZXING)
    ZXing::ImageView img(grey_.data(), dw, dh, ZXing::ImageFormat::Lum);
    ZXing::ReaderOptions opts;
    opts.setFormats(ZXing::BarcodeFormat::QRCode);
    opts.setTryHarder(false);
    opts.setMaxNumberOfSymbols(1);
    ZXing::Barcode b = ZXing::ReadBarcode(img, opts);
    if (!b.isValid() || b.text().empty()) return false;
    *text = b.text();
    return true;
#elif defined(VISION_QR_OPENCV)
    cv::Mat m(dh, dw, CV_8UC1, grey_.data());
    cv::QRCodeDetector det;
    std::string s = det.detectAndDecode(m);
    if (s.empty()) return false;
    *text = s;
    return true;
#else
    (void)text;
    return false;
#endif
}

}  // namespace av::dev
