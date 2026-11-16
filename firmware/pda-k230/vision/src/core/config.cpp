#include "config.h"

#include <cerrno>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>

#include "json.h"

namespace av {

namespace {

enum class T { Float, Double, Int, Bool, Str, Classifier, Rotation };

struct Key {
    const char *name;
    T type;
    void *(*ptr)(Config &);
};

#define K(name, type, expr) {name, type, [](Config &c) -> void * { return &(expr); }}

const Key kKeys[] = {
    // device
    K("socket", T::Str, c.device.socket_path),
    K("video_device", T::Int, c.device.video_device),
    K("width", T::Int, c.device.width),
    K("height", T::Int, c.device.height),
    K("model_dir", T::Str, c.device.model_dir),
    K("hand_det_model", T::Str, c.device.hand_det_model),
    K("hand_kp_model", T::Str, c.device.hand_kp_model),
    K("face_model", T::Str, c.device.face_model),
    K("hand_obj_thresh", T::Float, c.device.hand_obj_thresh),
    K("hand_nms_thresh", T::Float, c.device.hand_nms_thresh),
    K("face_thresh", T::Float, c.device.face_thresh),
    K("qr_max_width", T::Int, c.device.qr_max_width),
    K("record_keypoints", T::Str, c.device.record_keypoints),
    K("verbose", T::Int, c.device.verbose),
    // orientation, classifier, engine
    K("rotate", T::Rotation, c.orientation.rotate),
    K("mirror", T::Bool, c.orientation.mirror),
    K("classifier", T::Classifier, c.classifier),
    K("state_interval_s", T::Double, c.state_interval_s),
    K("qr_repeat_s", T::Double, c.qr_repeat_s),
    K("hand_switch_dist", T::Float, c.hand_switch_dist),
    // plan
    K("gestures_max_fps", T::Double, c.plan.gestures_max_fps),
    K("presence_fps", T::Double, c.plan.presence_fps),
    K("qr_fps", T::Double, c.plan.qr_fps),
    K("face_interval_s", T::Double, c.plan.face_interval_s),
    K("qr_interval_s", T::Double, c.plan.qr_interval_s),
    // hand shape
    K("finger_extended_reach", T::Float, c.shape.finger_extended_reach),
    K("finger_extended_max_bend", T::Float, c.shape.finger_extended_max_bend),
    K("finger_curled_reach", T::Float, c.shape.finger_curled_reach),
    K("finger_curled_min_bend", T::Float, c.shape.finger_curled_min_bend),
    K("thumb_extended_spread", T::Float, c.shape.thumb_extended_spread),
    K("thumb_curled_spread", T::Float, c.shape.thumb_curled_spread),
    K("thumb_max_bend", T::Float, c.shape.thumb_max_bend),
    K("thumb_curled_min_bend", T::Float, c.shape.thumb_curled_min_bend),
    K("thumbs_up_max_tilt", T::Float, c.shape.thumbs_up_max_tilt),
    K("min_palm_px", T::Float, c.shape.min_palm_px),
    // tracker
    K("cursor_min_interval_s", T::Double, c.tracker.cursor_min_interval_s),
    K("cursor_smooth_tau_s", T::Double, c.tracker.cursor_smooth_tau_s),
    K("cursor_min_move", T::Float, c.tracker.cursor_min_move),
    K("cursor_keepalive_s", T::Double, c.tracker.cursor_keepalive_s),
    K("hand_lost_s", T::Double, c.tracker.hand_lost_s),
    K("max_gap_s", T::Double, c.tracker.max_gap_s),
    K("swipe_window_s", T::Double, c.tracker.swipe_window_s),
    K("swipe_min_dist_x", T::Float, c.tracker.swipe_min_dist_x),
    K("swipe_min_dist_y", T::Float, c.tracker.swipe_min_dist_y),
    K("swipe_min_speed", T::Float, c.tracker.swipe_min_speed),
    K("swipe_axis_ratio", T::Float, c.tracker.swipe_axis_ratio),
    K("swipe_refractory_s", T::Double, c.tracker.swipe_refractory_s),
    K("swipe_return_block_s", T::Double, c.tracker.swipe_return_block_s),
    K("shape_switch_s", T::Double, c.tracker.shape_switch_s),
    K("shape_min_conf", T::Float, c.tracker.shape_min_conf),
    K("palm_hold_s", T::Double, c.tracker.palm_hold_s),
    K("fist_hold_s", T::Double, c.tracker.fist_hold_s),
    K("thumbs_up_hold_s", T::Double, c.tracker.thumbs_up_hold_s),
    K("point_hold_s", T::Double, c.tracker.point_hold_s),
    K("hold_max_move", T::Float, c.tracker.hold_max_move),
    K("hold_after_swipe_s", T::Double, c.tracker.hold_after_swipe_s),
    // presence
    K("presence_appear_s", T::Double, c.presence.appear_s),
    K("presence_leave_s", T::Double, c.presence.leave_s),
    K("presence_seen_gap_s", T::Double, c.presence.seen_gap_s),
};

#undef K

std::string trim(const std::string &s) {
    size_t a = 0, b = s.size();
    while (a < b && (s[a] == ' ' || s[a] == '\t' || s[a] == '\r' || s[a] == '\n')) a++;
    while (b > a && (s[b - 1] == ' ' || s[b - 1] == '\t' || s[b - 1] == '\r' || s[b - 1] == '\n')) b--;
    return s.substr(a, b - a);
}

bool parse_double(const std::string &v, double *out) {
    if (v.empty()) return false;
    errno = 0;
    char *end = nullptr;
    double d = std::strtod(v.c_str(), &end);
    if (errno || end != v.c_str() + v.size() || !std::isfinite(d)) return false;
    *out = d;
    return true;
}

bool parse_int(const std::string &v, int *out) {
    if (v.empty()) return false;
    errno = 0;
    char *end = nullptr;
    long l = std::strtol(v.c_str(), &end, 10);
    if (errno || end != v.c_str() + v.size() || l < -1000000 || l > 1000000) return false;
    *out = static_cast<int>(l);
    return true;
}

bool parse_bool(const std::string &v, bool *out) {
    if (v == "1" || v == "true" || v == "yes" || v == "on") { *out = true; return true; }
    if (v == "0" || v == "false" || v == "no" || v == "off") { *out = false; return true; }
    return false;
}

const Key *find_key(const std::string &name) {
    for (const Key &k : kKeys) {
        if (name == k.name) return &k;
    }
    return nullptr;
}

std::string value_text(const Key &k, Config &c) {
    void *p = k.ptr(c);
    std::string out;
    switch (k.type) {
    case T::Float: json::append_number(out, *static_cast<float *>(p), 4); break;
    case T::Double: json::append_number(out, *static_cast<double *>(p), 4); break;
    case T::Int:
    case T::Rotation: out = std::to_string(*static_cast<int *>(p)); break;
    case T::Bool: out = *static_cast<bool *>(p) ? "true" : "false"; break;
    case T::Str: out = *static_cast<std::string *>(p); break;
    case T::Classifier:
        out = *static_cast<Classifier *>(p) == Classifier::Canaan ? "canaan" : "geometry";
        break;
    }
    return out;
}

}  // namespace

bool config_set(Config &c, const std::string &key_in, const std::string &value_in,
                std::string *err) {
    std::string key = trim(key_in), value = trim(value_in);
    for (char &ch : key) {
        if (ch == '-') ch = '_';
    }
    const Key *k = find_key(key);
    if (!k) {
        if (err) *err = "unknown key '" + key + "'";
        return false;
    }
    void *p = k->ptr(c);
    auto bad = [&](const char *what) {
        if (err) *err = "bad value for " + key + " (" + what + "): '" + value + "'";
        return false;
    };
    switch (k->type) {
    case T::Float: {
        double d;
        if (!parse_double(value, &d)) return bad("number");
        *static_cast<float *>(p) = static_cast<float>(d);
        return true;
    }
    case T::Double: {
        double d;
        if (!parse_double(value, &d)) return bad("number");
        *static_cast<double *>(p) = d;
        return true;
    }
    case T::Int: {
        int i;
        if (!parse_int(value, &i)) return bad("integer");
        *static_cast<int *>(p) = i;
        return true;
    }
    case T::Rotation: {
        int i;
        if (!parse_int(value, &i) || !valid_rotation(i)) return bad("0, 90, 180 or 270");
        *static_cast<int *>(p) = i;
        return true;
    }
    case T::Bool: {
        bool b;
        if (!parse_bool(value, &b)) return bad("true or false");
        *static_cast<bool *>(p) = b;
        return true;
    }
    case T::Str:
        *static_cast<std::string *>(p) = value;
        return true;
    case T::Classifier:
        if (value == "geometry") *static_cast<Classifier *>(p) = Classifier::Geometry;
        else if (value == "canaan") *static_cast<Classifier *>(p) = Classifier::Canaan;
        else return bad("geometry or canaan");
        return true;
    }
    return false;
}

void config_load_text(Config &c, const std::string &text, std::vector<std::string> *errors) {
    std::istringstream in(text);
    std::string ln;
    int n = 0;
    while (std::getline(in, ln)) {
        n++;
        size_t hash = ln.find('#');
        if (hash != std::string::npos) ln = ln.substr(0, hash);
        ln = trim(ln);
        if (ln.empty()) continue;
        size_t eq = ln.find('=');
        std::string err;
        if (eq == std::string::npos) {
            err = "expected key = value";
        } else if (config_set(c, ln.substr(0, eq), ln.substr(eq + 1), &err)) {
            continue;
        }
        if (errors) errors->push_back("line " + std::to_string(n) + ": " + err);
    }
}

bool config_load_file(Config &c, const std::string &path, std::vector<std::string> *errors) {
    std::ifstream f(path);
    if (!f) return false;
    std::ostringstream ss;
    ss << f.rdbuf();
    std::vector<std::string> errs;
    config_load_text(c, ss.str(), &errs);
    if (errors) {
        for (auto &e : errs) errors->push_back(path + ": " + e);
    }
    return true;
}

std::string config_dump(const Config &cin) {
    Config c = cin;
    std::string out;
    for (const Key &k : kKeys) {
        out += k.name;
        out += " = ";
        out += value_text(k, c);
        out += '\n';
    }
    return out;
}

CliResult config_parse_cli(Config &c, int argc, const char *const *argv,
                           const std::string &default_config_path) {
    CliResult r;
    // First pass: --config
    std::string path = default_config_path;
    bool explicit_path = false;
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        if (a == "--config" && i + 1 < argc) {
            path = argv[++i];
            explicit_path = true;
        } else if (a.rfind("--config=", 0) == 0) {
            path = a.substr(9);
            explicit_path = true;
        }
    }
    r.config_path = explicit_path ? path : std::string();
    if (!path.empty()) {
        if (!config_load_file(c, path, &r.errors) && explicit_path) {
            r.errors.push_back("can't read config file " + path);
        }
    }
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        if (a == "--config") {
            i++;
            continue;
        }
        if (a.rfind("--config=", 0) == 0) continue;
        if (a == "--help" || a == "-h") { r.help = true; continue; }
        if (a == "--version") { r.version = true; continue; }
        if (a == "--list-keys") { r.list_keys = true; continue; }
        if (a.rfind("--", 0) != 0 || a.size() < 3) {
            r.errors.push_back("unexpected argument '" + a + "'");
            continue;
        }
        std::string key, value;
        size_t eq = a.find('=');
        if (eq != std::string::npos) {
            key = a.substr(2, eq - 2);
            value = a.substr(eq + 1);
        } else {
            key = a.substr(2);
            std::string norm = key;
            for (char &ch : norm) {
                if (ch == '-') ch = '_';
            }
            const Key *k = find_key(norm);
            if (k && k->type == T::Bool && (i + 1 >= argc || std::strncmp(argv[i + 1], "--", 2) == 0)) {
                value = "true";  // a bare boolean flag
            } else if (i + 1 < argc) {
                value = argv[++i];
            } else {
                r.errors.push_back("missing value for --" + key);
                continue;
            }
        }
        std::string err;
        if (!config_set(c, key, value, &err)) r.errors.push_back(err);
    }
    r.ok = r.errors.empty();
    return r;
}

}  // namespace av
