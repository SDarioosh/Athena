// All settings of athena-vision in one place: the thresholds of the pure core and the
// device settings (camera, models, socket). Read from a `key = value` file
// (default /etc/athena-pda/vision.conf, see vision.conf.example) and then from the command
// line (`--key=value` or `--key value`); later values win. `athena-vision --list-keys`
// prints every key with its current value.
#pragma once

#include <string>
#include <vector>

#include "gesture_tracker.h"
#include "hand_shape.h"
#include "mode.h"
#include "orientation.h"
#include "presence.h"

namespace av {

enum class Classifier { Geometry, Canaan };

struct DeviceConfig {
    std::string socket_path;            // empty: $ATHENA_PDA_RUN/vision.sock, else
                                        // /run/athena-pda/vision.sock
    int video_device = -1;              // /dev/videoN; -1: the vvcam AI channel (auto)
    int width = 1280;                   // AI channel frame size (planar RGB)
    int height = 720;
    std::string model_dir = "/usr/share/athena-vision/models";
    std::string hand_det_model = "hand_det.kmodel";        // relative to model_dir
    std::string hand_kp_model = "handkp_det.kmodel";
    std::string face_model = "/root/app/face_detect/face_detection_320.kmodel";
    float hand_obj_thresh = 0.3f;
    float hand_nms_thresh = 0.4f;
    float face_thresh = 0.6f;
    int qr_max_width = 640;             // the grey frame is scaled down to this for QR
    std::string record_keypoints;       // a file: write keypoint tracks (never pixels)
    int verbose = 0;
};

struct Config {
    HandShapeConfig shape;
    Classifier classifier = Classifier::Geometry;
    TrackerConfig tracker;
    PresenceConfig presence;
    PlanConfig plan;
    Orientation orientation{90, true};
    double state_interval_s = 5.0;      // `state` repeat while the camera is on
    double qr_repeat_s = 3.0;           // the same QR text again only after this
    float hand_switch_dist = 0.15f;     // keep following the same hand within this
    DeviceConfig device;
};

// Sets one key. False (with a reason) for an unknown key or a bad value.
bool config_set(Config &c, const std::string &key, const std::string &value,
                std::string *err);

// Reads a `key = value` file: '#' starts a comment, blank lines are skipped. Returns
// false if the file can't be opened; bad lines are reported in errors and skipped.
bool config_load_file(Config &c, const std::string &path, std::vector<std::string> *errors);

// Applies `key = value` text (the file's contents).
void config_load_text(Config &c, const std::string &text, std::vector<std::string> *errors);

// Every key with its current value, one `key = value` per line.
std::string config_dump(const Config &c);

// The command line. Recognises --config FILE (read before the other flags, wherever it is),
// --help, --version, --list-keys and --key=value / --key value for every config key.
struct CliResult {
    bool ok = true;
    bool help = false, version = false, list_keys = false;
    std::string config_path;            // as given, or empty
    std::vector<std::string> errors;
};
CliResult config_parse_cli(Config &c, int argc, const char *const *argv,
                           const std::string &default_config_path);

}  // namespace av
