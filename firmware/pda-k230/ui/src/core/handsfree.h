/* Hands-free input: vision events -> UI commands, per the plan's gesture table
 * (docs/k230-pda-plan.md Contract 2). Pure C, time passed in; host-tested
 * (test/test_vision.c). Only used while Hands-free is on (ui.json `hands_free`).
 *
 * | vision                         | command                                          |
 * |--------------------------------|--------------------------------------------------|
 * | swipe_left / swipe_right       | HF_NEXT / HF_PREV (tab; in a detail: next / back)|
 * | swipe_up / swipe_down          | HF_PAGE_DOWN / HF_PAGE_UP                        |
 * | point                          | HF_FOCUS_AT the hand cursor                      |
 * | thumbs_up, a to-do focused     | HF_HOLD_START; HF_HOLD_COMMIT after 1 s          |
 * | palm                           | HF_HOME (back to Today)                          |
 * | fist                           | HF_SCREEN_OFF (ambient)                          |
 * | hand x, y, shape               | HF_CURSOR (shape none: HF_CURSOR_HIDE)           |
 *
 * The hold (the only gesture that changes data) starts on a `thumbs_up` gesture or on a
 * hand whose shape becomes thumbs_up, and only while a to-do is focused. It is aborted
 * (HF_HOLD_CANCEL) by a hand of any other shape, the hand going, another gesture, the
 * focus moving, or Hands-free going off; it commits once it has lasted HF_HOLD_MS.
 * Gestures under HF_MIN_CONF are ignored. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "vision_msg.h"

#ifdef __cplusplus
extern "C" {
#endif

#define HF_HOLD_MS 1000
#define HF_MIN_CONF 0.5

typedef enum {
    HF_NONE = 0,
    HF_NEXT,
    HF_PREV,
    HF_PAGE_DOWN,
    HF_PAGE_UP,
    HF_FOCUS_AT,  /* x, y: the cursor (0..1) */
    HF_HOLD_START,
    HF_HOLD_CANCEL,
    HF_HOLD_COMMIT,
    HF_HOME,
    HF_SCREEN_OFF,
    HF_CURSOR,      /* x, y, shape */
    HF_CURSOR_HIDE,
} hf_kind_t;

typedef struct {
    hf_kind_t kind;
    double x, y;
    vision_shape_t shape;
} hf_cmd_t;

typedef struct {
    bool cursor;          /* a hand is on screen */
    double x, y;          /* its last position (0..1) */
    vision_shape_t shape;
    bool holding;
    int64_t hold_start_ms;
} hf_state_t;

#define HF_CMDS_MAX 3

void hf_reset(hf_state_t *s);

/* One vision event. focused_todo: a to-do has the focus ring. Returns the number of
 * commands written to out (<= HF_CMDS_MAX). */
int hf_on_event(hf_state_t *s, const vision_event_t *ev, bool focused_todo, int64_t now_ms,
                hf_cmd_t out[HF_CMDS_MAX]);

/* Time passing: HF_HOLD_COMMIT once the hold has lasted HF_HOLD_MS (1 command or 0). */
int hf_tick(hf_state_t *s, int64_t now_ms, hf_cmd_t *out);

/* The focus moved or went: a running hold is cancelled (1 command or 0). */
int hf_focus_changed(hf_state_t *s, hf_cmd_t *out);

/* 0..1 of the hold ring, 0 when not holding. */
double hf_hold_progress(const hf_state_t *s, int64_t now_ms);

#ifdef __cplusplus
}
#endif
