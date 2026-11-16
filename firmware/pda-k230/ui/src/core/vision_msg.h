/* vision.sock messages (docs/k230-pda-plan.md Contract 2): one JSON object per line, at
 * most 1024 bytes. Pure C; host-tested (test/test_vision.c). */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define VISION_LINE_MAX 1024

typedef enum {
    VIS_UNKNOWN = 0, /* a JSON object with an unknown "t": ignored */
    VIS_STATE,
    VIS_GESTURE,
    VIS_HAND,
    VIS_PRESENCE,
    VIS_QR,
} vision_type_t;

typedef enum {
    VIS_G_NONE = 0,
    VIS_G_SWIPE_LEFT,
    VIS_G_SWIPE_RIGHT,
    VIS_G_SWIPE_UP,
    VIS_G_SWIPE_DOWN,
    VIS_G_PALM,
    VIS_G_FIST,
    VIS_G_THUMBS_UP,
    VIS_G_POINT,
} vision_gesture_t;

typedef enum {
    VIS_SHAPE_NONE = 0,
    VIS_SHAPE_OPEN,
    VIS_SHAPE_FIST,
    VIS_SHAPE_POINT,
    VIS_SHAPE_THUMBS_UP,
    VIS_SHAPE_OTHER,
} vision_shape_t;

typedef enum {
    VIS_CAM_OFF = 0,
    VIS_CAM_ON,
    VIS_CAM_ERROR,
} vision_camera_t;

typedef struct {
    vision_type_t type;
    vision_camera_t camera; /* state */
    double fps;             /* state */
    char msg[96];           /* state */
    vision_gesture_t g;     /* gesture */
    double conf;            /* gesture, 0..1 */
    double x, y;            /* hand, 0..1 (mirrored by vision) */
    vision_shape_t shape;   /* hand */
    bool present;           /* presence */
    char text[VISION_LINE_MAX]; /* qr */
} vision_event_t;

/* One line without its "\n". false: not JSON, no string "t", or a known type with a
 * missing or bad field (a gesture with an unknown name, a hand without x/y). */
bool vision_parse_line(const char *line, size_t len, vision_event_t *out);

/* {"t":"mode","gestures":…,"presence":…,"qr":…}\n into out; the length with the "\n". */
size_t vision_mode_line(char *out, size_t cap, bool gestures, bool presence, bool qr);

const char *vision_gesture_name(vision_gesture_t g);

#ifdef __cplusplus
}
#endif
