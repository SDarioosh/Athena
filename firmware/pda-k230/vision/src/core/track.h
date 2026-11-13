// Keypoint tracks: what the detectors found, frame by frame, as JSON lines. Recorded on the
// board with `athena-vision --record-keypoints FILE` and fed back through the core with the
// replay tool to tune thresholds. A track holds hand boxes and keypoints, a face-seen
// boolean and the frame size: never pixels, never QR texts.
//
//   {"track":"athena-vision","v":1,"w":1280,"h":720,"rotate":90,"mirror":true}
//   {"t":12.345,"hands":[{"box":[x1,y1,x2,y2],"score":0.82,"kp":[x0,y0,...,x20,y20]}]}
//   {"t":12.412,"hands":[],"face":true}
//   {"t":12.480}                                         (hand detection didn't run)
//
// The header line is optional (replay then needs --width/--height); "w"/"h" in a frame
// line override it. Coordinates are camera pixels, before rotation and mirroring.
#pragma once

#include <string>
#include <string_view>

#include "engine.h"
#include "orientation.h"

namespace av {

struct TrackHeader {
    int width = 0, height = 0;
    bool has_orientation = false;
    Orientation orientation;
};

std::string encode_track_header(const TrackHeader &h);
// One frame (qr_text is never written).
std::string encode_track_frame(const FrameResult &f, bool write_size = false);

enum class TrackLine { Header, Frame, Blank, Invalid };

// Reads one line. Frame lines get width/height from the header unless they carry their own.
TrackLine decode_track_line(std::string_view line, TrackHeader *header, FrameResult *frame,
                            std::string *err = nullptr);

}  // namespace av
