// athena-vision: the camera AI of the K230 PDA (docs/k230-pda-plan.md, Contract 2).
//
// Connects to the UI's vision.sock, runs what the UI's `mode` asks for (hand gestures,
// presence, QR codes) on the camera with the KPU, and sends back only events. Frames
// never leave this process and are never stored: there is no option to dump them.
// The camera runs only while the UI asks for something; without a UI it is off.
#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <memory>
#include <poll.h>
#include <string>
#include <vector>

#include "camera.h"
#include "config.h"
#include "engine.h"
#include "face_model.h"
#include "hand_models.h"
#include "kmodel.h"
#include "line_client.h"
#include "qr_reader.h"
#include "track.h"

namespace {

volatile std::sig_atomic_t g_stop = 0;

void on_signal(int) { g_stop = 1; }

double now_s() {
    timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<double>(ts.tv_sec) + ts.tv_nsec * 1e-9;
}

int verbose = 0;

void log(const char *fmt, const std::string &a = std::string()) {
    std::fprintf(stderr, "athena-vision: ");
    std::fprintf(stderr, fmt, a.c_str());
    std::fputc('\n', stderr);
}

std::string model_path(const av::Config &c, const std::string &name) {
    if (name.empty() || name[0] == '/') return name;
    return c.device.model_dir + "/" + name;
}

std::string socket_path(const av::Config &c) {
    if (!c.device.socket_path.empty()) return c.device.socket_path;
    const char *run = std::getenv("ATHENA_PDA_RUN");
    std::string dir = run && *run ? run : "/run/athena-pda";
    return dir + "/vision.sock";
}

void usage() {
    std::printf(
        "usage: athena-vision [--config FILE] [--KEY=VALUE ...] [--list-keys] [--version]\n"
        "  Connects to $ATHENA_PDA_RUN/vision.sock (default /run/athena-pda/vision.sock) and\n"
        "  runs hand gestures, presence and QR decoding on the camera while the UI asks for\n"
        "  them. Config: /etc/athena-pda/vision.conf (key = value); --list-keys prints all.\n"
        "  --record-keypoints FILE appends hand keypoint tracks (no pixels) for tuning.\n");
}

// Everything that needs the camera and the KPU, set up lazily.
struct Pipeline {
    av::dev::Camera cam;
    av::dev::FrameTensor frame;
    bool frame_ready = false;
    av::dev::HandDetector det;
    av::dev::HandKeypointer kp;
    bool hand_ready = false;
    double hand_retry = 0;  // after a failed model load, try again from this time
    av::dev::FaceDetector face;
    bool face_ready = false;
    double face_retry = 0;
    av::dev::QrReader qr;
};

}  // namespace

int main(int argc, char **argv) {
    av::Config cfg;
    av::CliResult cli = av::config_parse_cli(cfg, argc, argv, "/etc/athena-pda/vision.conf");
    if (cli.help) {
        usage();
        return 0;
    }
    if (cli.version) {
        std::printf("athena-vision %s (qr: %s)\n", ATHENA_VISION_VERSION, av::dev::QrReader::backend());
        return 0;
    }
    if (cli.list_keys) {
        std::fputs(av::config_dump(cfg).c_str(), stdout);
        return cli.ok ? 0 : 2;
    }
    for (const std::string &e : cli.errors) log("%s", e);
    if (!cli.ok) return 2;
    verbose = cfg.device.verbose;

    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);
    std::signal(SIGPIPE, SIG_IGN);

    const std::string sock = socket_path(cfg);
    av::LineClient client(sock, av::kMaxLine);
    av::Engine engine(cfg);
    Pipeline pl;

    std::FILE *rec = nullptr;
    if (!cfg.device.record_keypoints.empty()) {
        rec = std::fopen(cfg.device.record_keypoints.c_str(), "a");
        if (!rec) log("can't open the keypoint record file %s", cfg.device.record_keypoints);
        else log("recording hand keypoints (no pixels) to %s", cfg.device.record_keypoints);
    }
    bool rec_header = false;

    log("waiting for the UI at %s", sock);
    double backoff = 0.5, next_connect = 0, next_cam_try = 0, last_proc = -1e9;
    double last_face = -1e9, last_qr = -1e9, last_frame = 0;
    int cam_errors = 0;
    bool cam_error_state = false;
    bool ui_connected = false;

    auto send_output = [&]() {
        for (const std::string &l : engine.take_output()) {
            if (client.connected()) client.send(l);
        }
        if (client.connected() && client.want_write()) client.flush();
    };

    while (!g_stop) {
        double now = now_s();

        if (!client.connected() && now >= next_connect) {
            std::string err;
            if (client.connect_once(&err)) {
                log("connected to the UI");
                engine.on_connect(now);
                ui_connected = true;
                backoff = 0.5;
            } else {
                if (verbose > 1) log("connect: %s", err);
                next_connect = now + backoff;
                backoff = std::min(backoff * 2, 5.0);
            }
        }

        av::Plan plan = engine.plan();
        if (plan.camera_on && !pl.cam.running() && now >= next_cam_try) {
            std::string err;
            bool ok = pl.cam.start(cfg.device.video_device, cfg.device.width, cfg.device.height, &err);
            if (ok && !pl.frame_ready) {
                ok = pl.frame.init(cfg.device.width, cfg.device.height, &err);
                pl.frame_ready = ok;
                if (!ok) pl.cam.stop();
            }
            if (ok) {
                log("camera on (/dev/video%s)", std::to_string(pl.cam.device()));
                engine.on_camera(av::CameraState::On, "", now);
                cam_errors = 0;
                cam_error_state = false;
                last_frame = now;
            } else {
                log("camera: %s", err);
                engine.on_camera(av::CameraState::Error, err, now);
                cam_error_state = true;
                next_cam_try = now + 5.0;
            }
        }
        if (!plan.camera_on && (pl.cam.running() || cam_error_state)) {
            if (pl.cam.running()) log("camera off");
            pl.cam.stop();
            engine.on_camera(av::CameraState::Off, "", now);
            cam_error_state = false;
            next_cam_try = 0;
        }

        send_output();  // camera changes now, not after the poll

        // wait for the socket or a frame
        pollfd fds[2];
        int n = 0, sock_i = -1, cam_i = -1;
        if (client.connected()) {
            sock_i = n;
            fds[n++] = {client.fd(), static_cast<short>(POLLIN | (client.want_write() ? POLLOUT : 0)), 0};
        }
        if (pl.cam.running()) {
            cam_i = n;
            fds[n++] = {pl.cam.fd(), POLLIN | POLLPRI, 0};
        }
        int timeout = pl.cam.running() ? 100 : 500;
        int pr = ::poll(fds, static_cast<nfds_t>(n), timeout);
        if (pr < 0 && errno != EINTR) {
            log("poll: %s", std::strerror(errno));
            break;
        }
        now = now_s();

        if (sock_i >= 0 && pr > 0 && (fds[sock_i].revents & (POLLIN | POLLHUP | POLLERR))) {
            std::vector<std::string> lines;
            bool alive = client.read_lines(lines);
            for (const std::string &l : lines) engine.on_line(l, now);
            if (!alive) {
                log("the UI went away");
                engine.on_disconnect(now);
                ui_connected = false;
                next_connect = now + 0.5;
            }
        }

        if (cam_i >= 0 && pr > 0 && (fds[cam_i].revents & (POLLIN | POLLPRI | POLLERR))) {
            av::dev::CameraFrame f;
            std::string err;
            av::dev::Camera::Grab g = pl.cam.grab(&f, 0, &err);
            if (g == av::dev::Camera::Grab::Frame) {
                last_frame = now;
                cam_errors = 0;
                plan = engine.plan();
                if (now - last_proc >= plan.frame_interval_s - 0.002) {
                    last_proc = now;
                    av::FrameResult r;
                    r.t = now;
                    r.width = f.width;
                    r.height = f.height;
                    bool want_face = plan.face && now - last_face >= plan.face_interval_s - 0.002;
                    bool want_qr = plan.qr && now - last_qr >= plan.qr_interval_s - 0.002;
                    bool need_kpu = plan.hand || want_face;
                    std::string merr;
                    if (need_kpu && !pl.frame.load(f.data, f.bytes, &merr)) {
                        log("frame: %s", merr);
                        need_kpu = false;
                    }
                    if (need_kpu && plan.hand) {
                        if (!pl.hand_ready && now >= pl.hand_retry) {
                            pl.hand_ready =
                                pl.det.init(model_path(cfg, cfg.device.hand_det_model), pl.frame,
                                            cfg.device.hand_obj_thresh, cfg.device.hand_nms_thresh, &merr) &&
                                pl.kp.init(model_path(cfg, cfg.device.hand_kp_model), &merr);
                            if (!pl.hand_ready) {
                                pl.hand_retry = now + 30.0;
                                log("hand models: %s", merr);
                                engine.on_camera(av::CameraState::Error, "hand models: " + merr, now);
                                cam_error_state = true;
                            }
                        }
                        if (pl.hand_ready) {
                            std::vector<av::HandDetection> hands;
                            if (pl.det.detect(pl.frame, hands, 2, &merr)) {
                                for (av::HandDetection &h : hands) {
                                    if (!pl.kp.run(pl.frame, h, &merr)) {
                                        if (verbose) log("keypoints: %s", merr);
                                    }
                                }
                                r.hand_ran = true;
                                r.hands = std::move(hands);
                            } else if (verbose) {
                                log("hand detection: %s", merr);
                            }
                        }
                    }
                    if (need_kpu && want_face) {
                        if (!pl.face_ready && now >= pl.face_retry) {
                            pl.face_ready = pl.face.init(model_path(cfg, cfg.device.face_model), pl.frame,
                                                         cfg.device.face_thresh, &merr);
                            if (!pl.face_ready) {
                                pl.face_retry = now + 30.0;
                                log("face model: %s", merr);
                                engine.on_camera(av::CameraState::Error, "face model: " + merr, now);
                                cam_error_state = true;
                            }
                        }
                        float best = 0;
                        if (pl.face_ready && pl.face.run(pl.frame, &r.face_seen, &best, &merr)) {
                            r.face_ran = true;
                            last_face = now;
                        }
                    }
                    if (want_qr && av::dev::QrReader::available()) {
                        // the middle plane (green): sharpest for black-and-white codes
                        const uint8_t *plane = f.data + static_cast<size_t>(f.width) * f.height;
                        r.qr_ran = true;
                        pl.qr.decode(plane, f.width, f.height, cfg.device.qr_max_width, &r.qr_text);
                        last_qr = now;
                    }
                    pl.cam.release();  // done with the pixels before anything else
                    engine.on_frame(r);
                    if (verbose > 1 && engine.last_hand().in_frame) {
                        const av::HandView &h = engine.last_hand();
                        std::fprintf(stderr, "athena-vision: hand %.3f %.3f %s %.2f\n", h.x, h.y,
                                     av::shape_name(h.shape), h.conf);
                    }
                    if (rec && (r.hand_ran || r.face_ran)) {
                        if (!rec_header) {
                            av::TrackHeader hd;
                            hd.width = r.width;
                            hd.height = r.height;
                            hd.has_orientation = true;
                            hd.orientation = cfg.orientation;
                            std::fputs(av::encode_track_header(hd).c_str(), rec);
                            rec_header = true;
                        }
                        std::fputs(av::encode_track_frame(r).c_str(), rec);
                        std::fflush(rec);
                    }
                }
                pl.cam.release();
            } else if (g == av::dev::Camera::Grab::Error) {
                if (++cam_errors >= 3) {
                    log("camera: %s", err);
                    pl.cam.stop();
                    engine.on_camera(av::CameraState::Error, err, now);
                    cam_error_state = true;
                    next_cam_try = now + 5.0;
                }
            }
        }
        if (pl.cam.running() && now - last_frame > 3.0) {
            log("camera: no frames for 3 s");
            pl.cam.stop();
            engine.on_camera(av::CameraState::Error, "no frames from the camera", now);
            cam_error_state = true;
            next_cam_try = now + 5.0;
        }

        engine.tick(now);
        send_output();
        if (ui_connected && !client.connected()) {  // a write found the socket broken
            log("the UI went away");
            engine.on_disconnect(now);
            ui_connected = false;
            next_connect = now + 0.5;
        }
    }

    pl.cam.stop();
    if (rec) std::fclose(rec);
    log("stopped");
    return 0;
}
