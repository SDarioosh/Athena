#include "camera.h"

#include <cerrno>
#include <cstring>
#include <linux/videodev2.h>
#include <poll.h>

#include <mmz.h>

namespace av::dev {

bool Camera::start(int device, int width, int height, std::string *err) {
    if (running_) return true;
    if (width <= 0 || height <= 0 || width > 4096 || height > 4096) {
        if (err) *err = "bad frame size";
        return false;
    }
    device_ = device >= 0 ? device : kd_mpi_get_vvcam_video00() + 1;
    v4l2_drm_default_context(&ctx_);
    ctx_.device = static_cast<unsigned>(device_);
    ctx_.display = false;
    ctx_.width = static_cast<unsigned>(width);
    ctx_.height = static_cast<unsigned>(height);
    ctx_.video_format = v4l2_fourcc('B', 'G', '3', 'P');
    ctx_.buffer_num = 3;
    if (v4l2_drm_setup(&ctx_, 1, nullptr) != 0) {
        // v4l2_drm_setup() cleans up after itself on failure (its `close:` path)
        if (err) *err = "camera setup failed on /dev/video" + std::to_string(device_);
        return false;
    }
    if (v4l2_drm_start(&ctx_) != 0) {
        if (err) *err = std::string("camera start failed: ") + std::strerror(errno);
        v4l2_drm_stop(&ctx_);
        return false;
    }
    running_ = true;
    held_ = false;
    return true;
}

void Camera::stop() {
    if (!running_) return;
    release();
    v4l2_drm_stop(&ctx_);  // STREAMOFF, frees the buffers, closes the device
    running_ = false;
}

Camera::Grab Camera::grab(CameraFrame *f, int timeout_ms, std::string *err) {
    if (!running_) {
        if (err) *err = "camera not running";
        return Grab::Error;
    }
    release();
    // Our own poll: v4l2_drm_dump() returns 0 on a timeout as if a frame had come.
    pollfd p{ctx_.video_fd, POLLIN | POLLPRI, 0};
    int r;
    do {
        r = ::poll(&p, 1, timeout_ms);
    } while (r < 0 && errno == EINTR);
    if (r == 0) return Grab::Timeout;
    if (r < 0 || (p.revents & (POLLERR | POLLHUP | POLLNVAL))) {
        if (err) *err = std::string("camera poll: ") + std::strerror(errno);
        return Grab::Error;
    }
    std::memset(&ctx_.vbuffer, 0, sizeof(ctx_.vbuffer));
    ctx_.vbuffer.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    ctx_.vbuffer.memory = V4L2_MEMORY_MMAP;
    if (v4l2_drm_dump(&ctx_, 0) != 0) {
        if (err) *err = std::string("camera dequeue: ") + std::strerror(errno);
        return Grab::Error;
    }
    held_ = true;
    unsigned idx = ctx_.vbuffer.index;
    if (idx >= ctx_.buffer_num || !ctx_.buffers || !ctx_.buffers[idx].mmap) {
        release();
        if (err) *err = "camera gave a bad buffer";
        return Grab::Error;
    }
    f->data = static_cast<const uint8_t *>(ctx_.buffers[idx].mmap);
    f->width = static_cast<int>(ctx_.width);
    f->height = static_cast<int>(ctx_.height);
    f->bytes = static_cast<size_t>(f->width) * static_cast<size_t>(f->height) * 3;
    return Grab::Frame;
}

void Camera::release() {
    if (!held_) return;
    v4l2_drm_dump_release(&ctx_);
    held_ = false;
}

}  // namespace av::dev
