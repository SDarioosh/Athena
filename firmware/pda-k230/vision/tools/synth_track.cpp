// synth_track: writes a synthetic, fictional keypoint track (track.h format) to stdout,
// for the replay tests and as an example of the format.
//
//   synth_track KIND [--fps N] [--jitter S] [--drop P] [--seed N] [--rotate R]
//               [--mirror true|false] [--width W] [--height H]
//   KIND: swipe_left swipe_right swipe_up swipe_down return_left palm fist thumbs_up
//         point drift
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#include "synth.h"
#include "track.h"

int main(int argc, char **argv) {
    if (argc < 2) {
        std::fprintf(stderr, "usage: synth_track KIND [--fps N] [--jitter S] [--drop P] [--seed N]\n"
                             "                   [--rotate R] [--mirror true|false] [--width W] [--height H]\n");
        return 2;
    }
    std::string kind = argv[1];
    synth::TrackSpec spec;
    av::Orientation o{90, true};
    int w = 1280, h = 720;
    for (int i = 2; i + 1 < argc; i += 2) {
        std::string k = argv[i];
        const char *v = argv[i + 1];
        if (k == "--fps") spec.fps = std::atof(v);
        else if (k == "--jitter") spec.jitter_s = std::atof(v);
        else if (k == "--drop") spec.drop_prob = std::atof(v);
        else if (k == "--seed") spec.seed = static_cast<uint32_t>(std::atoi(v));
        else if (k == "--rotate") o.rotate = std::atoi(v);
        else if (k == "--mirror") o.mirror = std::strcmp(v, "true") == 0;
        else if (k == "--width") w = std::atoi(v);
        else if (k == "--height") h = std::atoi(v);
        else {
            std::fprintf(stderr, "unknown option %s\n", k.c_str());
            return 2;
        }
    }
    if (spec.fps <= 0 || w <= 0 || h <= 0 || !av::valid_rotation(o.rotate)) {
        std::fprintf(stderr, "bad option value\n");
        return 2;
    }
    std::vector<synth::Segment> segs;
    using av::Shape;
    auto hold = [&](Shape s) {
        segs = {{1.6, 0.5f, 0.5f, 0.5f, 0.5f, s, true}, {0.8, 0.5f, 0.5f, 0.5f, 0.5f, s, false}};
    };
    if (kind.rfind("swipe_", 0) == 0) segs = synth::swipe_segments(kind.substr(6));
    else if (kind == "return_left") segs = synth::swipe_segments("left", 0.3, 0.45f, true);
    else if (kind == "palm") hold(Shape::Open);
    else if (kind == "fist") hold(Shape::Fist);
    else if (kind == "thumbs_up") hold(Shape::ThumbsUp);
    else if (kind == "point") hold(Shape::Point);
    else if (kind == "drift") {
        segs = {{3.0, 0.3f, 0.5f, 0.6f, 0.55f, Shape::Other, true},
                {0.8, 0.6f, 0.55f, 0.6f, 0.55f, Shape::Other, false}};
    } else {
        std::fprintf(stderr, "unknown kind %s\n", kind.c_str());
        return 2;
    }
    av::TrackHeader hd;
    hd.width = w;
    hd.height = h;
    hd.has_orientation = true;
    hd.orientation = o;
    std::fputs(av::encode_track_header(hd).c_str(), stdout);
    for (const av::FrameResult &f : synth::make_frames(segs, spec, w, h, o)) {
        std::fputs(av::encode_track_frame(f).c_str(), stdout);
    }
    return 0;
}
