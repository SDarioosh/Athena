#include "orientation.h"

namespace av {

bool valid_rotation(int deg) { return deg == 0 || deg == 90 || deg == 180 || deg == 270; }

void display_size(const Orientation &o, int cam_w, int cam_h, int *disp_w, int *disp_h) {
    bool swap = o.rotate == 90 || o.rotate == 270;
    *disp_w = swap ? cam_h : cam_w;
    *disp_h = swap ? cam_w : cam_h;
}

Pt to_display_px(const Orientation &o, Pt c, int cam_w, int cam_h) {
    float W = static_cast<float>(cam_w), H = static_cast<float>(cam_h);
    Pt d;
    switch (o.rotate) {
    case 90:  // clockwise: the left edge becomes the top
        d = {H - c.y, c.x};
        break;
    case 180:
        d = {W - c.x, H - c.y};
        break;
    case 270:
        d = {c.y, W - c.x};
        break;
    default:
        d = c;
    }
    if (o.mirror) {
        int dw, dh;
        display_size(o, cam_w, cam_h, &dw, &dh);
        d.x = static_cast<float>(dw) - d.x;
    }
    return d;
}

Pt from_display_px(const Orientation &o, Pt d, int cam_w, int cam_h) {
    float W = static_cast<float>(cam_w), H = static_cast<float>(cam_h);
    if (o.mirror) {
        int dw, dh;
        display_size(o, cam_w, cam_h, &dw, &dh);
        d.x = static_cast<float>(dw) - d.x;
    }
    switch (o.rotate) {
    case 90: return {d.y, H - d.x};
    case 180: return {W - d.x, H - d.y};
    case 270: return {W - d.y, d.x};
    default: return d;
    }
}

}  // namespace av
