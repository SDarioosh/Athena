// The camera: the vvcam ISP's AI channel through Canaan's v4l2-drm helper (libv4l2-drm,
// package vvcam of k230_linux_sdk), as the SDK's AI demos capture it: planar 3-channel
// frames ('BG3P', 3 x H x W bytes, channel order as the demos feed the models) from the
// video device after `vvcam-video.0.0`. The frames stay in the driver's buffers: nothing
// is copied out except into the KPU's input tensor, nothing is written anywhere.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

#include <v4l2-drm.h>

namespace av::dev {

struct CameraFrame {
    const uint8_t *data = nullptr;   // planar: plane 0, 1, 2, each width * height bytes
    size_t bytes = 0;
    int width = 0, height = 0;
};

class Camera {
public:
    enum class Grab { Frame, Timeout, Error };

    ~Camera() { stop(); }

    // device < 0: the vvcam AI channel (vvcam-video.0.0's index + 1, as the demos use).
    bool start(int device, int width, int height, std::string *err);
    void stop();
    bool running() const { return running_; }
    int fd() const { return running_ ? ctx_.video_fd : -1; }
    int device() const { return device_; }

    // Waits up to timeout_ms for a frame. The frame stays valid until release().
    Grab grab(CameraFrame *f, int timeout_ms, std::string *err);
    void release();

private:
    v4l2_drm_context ctx_{};
    bool running_ = false;
    bool held_ = false;
    int device_ = -1;
};

}  // namespace av::dev
