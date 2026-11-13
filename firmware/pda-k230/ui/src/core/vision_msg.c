#include "vision_msg.h"

#include <stdio.h>
#include <string.h>

#include "json.h"

#define TOKS 32

static const char *const GESTURES[] = {"",           "swipe_left", "swipe_right", "swipe_up", "swipe_down",
                                       "palm",       "fist",       "thumbs_up",   "point"};
static const char *const SHAPES[] = {"none", "open", "fist", "point", "thumbs_up", "other"};

const char *vision_gesture_name(vision_gesture_t g)
{
    return (unsigned)g < sizeof(GESTURES) / sizeof(GESTURES[0]) ? GESTURES[g] : "";
}

static double clamp01(double v)
{
    return v < 0 ? 0 : v > 1 ? 1 : v;
}

bool vision_parse_line(const char *line, size_t len, vision_event_t *out)
{
    memset(out, 0, sizeof(*out));
    json_tok_t t[TOKS];
    const int n = json_parse(line, len, t, TOKS);
    if (n < 1 || t[0].type != JSON_OBJ) {
        return false;
    }
    const json_doc_t d = {line, t, n};
    const int tt = json_get(&d, 0, "t");
    if (tt < 0 || t[tt].type != JSON_STR) {
        return false;
    }
    if (json_str_eq(&d, tt, "state")) {
        out->type = VIS_STATE;
        const int c = json_get(&d, 0, "camera");
        if (c >= 0 && json_str_eq(&d, c, "on")) {
            out->camera = VIS_CAM_ON;
        } else if (c >= 0 && json_str_eq(&d, c, "off")) {
            out->camera = VIS_CAM_OFF;
        } else if (c >= 0 && json_str_eq(&d, c, "error")) {
            out->camera = VIS_CAM_ERROR;
        } else {
            return false;
        }
        json_num(&d, json_get(&d, 0, "fps"), &out->fps);
        const int m = json_get(&d, 0, "msg");
        if (m >= 0) {
            json_str(&d, m, out->msg, sizeof(out->msg));
        }
        return true;
    }
    if (json_str_eq(&d, tt, "gesture")) {
        out->type = VIS_GESTURE;
        const int g = json_get(&d, 0, "g");
        for (int i = 1; i < (int)(sizeof(GESTURES) / sizeof(GESTURES[0])); i++) {
            if (g >= 0 && json_str_eq(&d, g, GESTURES[i])) {
                out->g = (vision_gesture_t)i;
            }
        }
        if (out->g == VIS_G_NONE) {
            return false;
        }
        out->conf = 1;
        if (json_num(&d, json_get(&d, 0, "conf"), &out->conf)) {
            out->conf = clamp01(out->conf);
        }
        return true;
    }
    if (json_str_eq(&d, tt, "hand")) {
        out->type = VIS_HAND;
        const int s = json_get(&d, 0, "shape");
        out->shape = VIS_SHAPE_OTHER;
        bool known = false;
        for (int i = 0; i < (int)(sizeof(SHAPES) / sizeof(SHAPES[0])); i++) {
            if (s >= 0 && json_str_eq(&d, s, SHAPES[i])) {
                out->shape = (vision_shape_t)i;
                known = true;
            }
        }
        if (s >= 0 && !known && t[s].type != JSON_STR) {
            return false;
        }
        const bool hx = json_num(&d, json_get(&d, 0, "x"), &out->x);
        const bool hy = json_num(&d, json_get(&d, 0, "y"), &out->y);
        if (out->shape != VIS_SHAPE_NONE && (!hx || !hy)) {
            return false;
        }
        out->x = clamp01(out->x);
        out->y = clamp01(out->y);
        return true;
    }
    if (json_str_eq(&d, tt, "presence")) {
        out->type = VIS_PRESENCE;
        return json_bool(&d, json_get(&d, 0, "present"), &out->present);
    }
    if (json_str_eq(&d, tt, "qr")) {
        out->type = VIS_QR;
        const int x = json_get(&d, 0, "text");
        return x >= 0 && json_str(&d, x, out->text, sizeof(out->text)) >= 0;
    }
    out->type = VIS_UNKNOWN;
    return true;
}

size_t vision_mode_line(char *out, size_t cap, bool gestures, bool presence, bool qr)
{
    const int n = snprintf(out, cap, "{\"t\":\"mode\",\"gestures\":%s,\"presence\":%s,\"qr\":%s}\n",
                           gestures ? "true" : "false", presence ? "true" : "false", qr ? "true" : "false");
    return n > 0 && (size_t)n < cap ? (size_t)n : 0;
}
