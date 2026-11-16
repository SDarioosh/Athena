// Contract 2 (`vision.sock`, docs/k230-pda-plan.md): one JSON object per line, UTF-8,
// '\n'-terminated, at most kMaxLine bytes including the '\n'.
//
// Every encode_* returns one complete line (with '\n') that is never longer than kMaxLine,
// or an empty string when the message can't be sent within it (only a QR text longer than
// about 1000 bytes: a cut QR text would be wrong, so it isn't sent).
#pragma once

#include <cstddef>
#include <string>
#include <string_view>

#include "types.h"

namespace av {

constexpr size_t kMaxLine = 1024;

struct Mode {
    bool gestures = false;
    bool presence = false;
    bool qr = false;
    bool any() const { return gestures || presence || qr; }
    bool operator==(const Mode &o) const {
        return gestures == o.gestures && presence == o.presence && qr == o.qr;
    }
    bool operator!=(const Mode &o) const { return !(*this == o); }
};

enum class CameraState { Off, On, Error };
const char *camera_state_name(CameraState s);

std::string encode_state(CameraState camera, double fps, std::string_view msg);
std::string encode_gesture(std::string_view g, float conf);
std::string encode_hand(float x, float y, Shape shape);
std::string encode_presence(bool present);
std::string encode_qr(std::string_view text);

struct UiMessage {
    enum class Kind { Mode, Unknown, Invalid } kind = Kind::Invalid;
    Mode mode;           // Kind::Mode; a missing or non-boolean field is false
    std::string type;    // the "t" of an unknown message
};

// One line from the UI (with or without its '\n' / "\r\n").
UiMessage decode_ui_line(std::string_view line);

}  // namespace av
