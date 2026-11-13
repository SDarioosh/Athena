// athena-vision-replay: feeds a keypoint track (track.h) through the vision core and prints
// the Contract 2 lines it would send. Used by the tests, and by the owner to tune
// thresholds on tracks recorded on the board (`athena-vision --record-keypoints FILE`).
//
//   athena-vision-replay [options] TRACK.jsonl     (or - for stdin)
//     --mode LIST        what runs: gestures,presence,qr (default gestures,presence)
//     --raw              print only the JSON lines, without the time
//     --hands            also print the shape seen in every frame (for tuning)
//     --tail S           let S seconds pass after the last frame (default 1)
//     --config FILE      a vision.conf; any config key works as --key=value too
//                        (--rotate/--mirror override the track header)
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <memory>
#include <string>
#include <vector>

#include "config.h"
#include "engine.h"
#include "track.h"

namespace {

void usage() {
    std::fprintf(stderr,
                 "usage: athena-vision-replay [--mode gestures,presence,qr] [--raw] [--hands]\n"
                 "                            [--tail S] [--config FILE] [--KEY=VALUE ...] TRACK|-\n");
}

bool parse_mode(const std::string &s, av::Mode *m) {
    *m = av::Mode();
    size_t i = 0;
    while (i <= s.size()) {
        size_t j = s.find(',', i);
        if (j == std::string::npos) j = s.size();
        std::string w = s.substr(i, j - i);
        if (w == "gestures") m->gestures = true;
        else if (w == "presence") m->presence = true;
        else if (w == "qr") m->qr = true;
        else if (!w.empty() && w != "none") return false;
        i = j + 1;
    }
    return true;
}

}  // namespace

int main(int argc, char **argv) {
    av::Mode mode;
    mode.gestures = true;
    mode.presence = true;
    bool raw = false, hands = false, orient_given = false;
    double tail = 1.0;
    std::string path;
    std::vector<const char *> rest{argv[0]};
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        if (a == "--mode" && i + 1 < argc) {
            if (!parse_mode(argv[++i], &mode)) {
                std::fprintf(stderr, "bad --mode\n");
                return 2;
            }
        } else if (a.rfind("--mode=", 0) == 0) {
            if (!parse_mode(a.substr(7), &mode)) {
                std::fprintf(stderr, "bad --mode\n");
                return 2;
            }
        } else if (a == "--raw") {
            raw = true;
        } else if (a == "--hands") {
            hands = true;
        } else if (a == "--tail" && i + 1 < argc) {
            tail = std::atof(argv[++i]);
        } else if (a == "--help" || a == "-h") {
            usage();
            return 0;
        } else if (a == "-" || a.rfind("--", 0) != 0) {
            path = a;
        } else {
            if (a.rfind("--rotate", 0) == 0 || a.rfind("--mirror", 0) == 0) orient_given = true;
            rest.push_back(argv[i]);
            // a "--key value" pair: keep the value with it
            if (a.find('=') == std::string::npos && i + 1 < argc &&
                std::strncmp(argv[i + 1], "--", 2) != 0) {
                std::string next = argv[i + 1];
                bool boolword = next == "true" || next == "false" || next == "1" || next == "0" ||
                                next == "yes" || next == "no" || next == "on" || next == "off";
                if (a != "--mirror" || boolword) rest.push_back(argv[++i]);
            }
        }
    }
    if (path.empty()) {
        usage();
        return 2;
    }
    av::Config cfg;
    av::CliResult cli = av::config_parse_cli(cfg, static_cast<int>(rest.size()), rest.data(), "");
    for (const std::string &e : cli.errors) std::fprintf(stderr, "%s\n", e.c_str());
    if (!cli.ok) return 2;

    std::ifstream file;
    std::istream *in = &std::cin;
    if (path != "-") {
        file.open(path);
        if (!file) {
            std::fprintf(stderr, "can't open %s\n", path.c_str());
            return 2;
        }
        in = &file;
    }

    av::TrackHeader header;
    std::unique_ptr<av::Engine> engine;
    double last_t = 0.0;
    int lineno = 0, frames = 0, bad = 0;
    auto start = [&](double t) {
        if (header.has_orientation && !orient_given) cfg.orientation = header.orientation;
        engine = std::make_unique<av::Engine>(cfg);
        engine->on_connect(t);
        engine->take_output();  // the initial `state`
        engine->set_mode(mode, t);
    };
    auto print = [&](double t) {
        for (const std::string &l : engine->take_output()) {
            if (raw) std::printf("%s", l.c_str());
            else std::printf("t=%.3f %s", t, l.c_str());
        }
    };
    std::string line;
    while (std::getline(*in, line)) {
        lineno++;
        av::FrameResult f;
        std::string err;
        av::TrackLine k = av::decode_track_line(line, &header, &f, &err);
        if (k == av::TrackLine::Invalid) {
            std::fprintf(stderr, "line %d: %s\n", lineno, err.c_str());
            bad++;
            continue;
        }
        if (k != av::TrackLine::Frame) continue;
        if (f.width <= 0 || f.height <= 0) {
            std::fprintf(stderr, "line %d: frame size unknown (no header, no w/h)\n", lineno);
            bad++;
            continue;
        }
        if (!engine) start(f.t);
        engine->on_frame(f);
        if (hands) {
            const av::HandView &h = engine->last_hand();
            if (h.in_frame) {
                std::printf("t=%.3f # hand %.3f %.3f %s %.2f\n", f.t, h.x, h.y,
                            av::shape_name(h.shape), h.conf);
            }
        }
        print(f.t);
        last_t = f.t;
        frames++;
    }
    if (engine && tail > 0) {
        double t = last_t + tail;
        engine->tick(t);
        print(t);
    }
    std::fprintf(stderr, "%d frames, %d bad lines\n", frames, bad);
    return bad ? 1 : 0;
}
