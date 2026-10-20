/* Tap, long press and swipe recognition. */
#include "check.h"
#include "gesture.h"

static gesture_t run(const int (*pts)[3], int n, int64_t up_at)
{
    gesture_state_t g;
    gesture_reset(&g);
    gesture_t last = {.kind = GESTURE_NONE};
    for (int i = 0; i < n; i++) {
        const gesture_t r = gesture_feed(&g, true, pts[i][0], pts[i][1], pts[i][2]);
        if (r.kind != GESTURE_NONE) {
            last = r;
        }
    }
    const gesture_t r = gesture_feed(&g, false, 0, 0, up_at);
    return r.kind != GESTURE_NONE ? r : last;
}

int main(void)
{
    static const int tap[][3] = {{100, 200, 0}, {104, 198, 50}, {102, 201, 100}};
    gesture_t r = run(tap, 3, 150);
    CHECK_EQ(r.kind, GESTURE_TAP);
    CHECK(r.x == 100 && r.y == 200);

    /* too slow for a tap, too short for a long press: nothing */
    static const int slow[][3] = {{100, 200, 0}, {100, 200, 650}};
    CHECK_EQ(run(slow, 2, 680).kind, GESTURE_NONE);

    /* long press fires while held, once; the lift adds nothing */
    gesture_state_t g;
    gesture_reset(&g);
    CHECK_EQ(gesture_feed(&g, true, 50, 60, 0).kind, GESTURE_NONE);
    CHECK_EQ(gesture_feed(&g, true, 52, 61, 400).kind, GESTURE_NONE);
    r = gesture_feed(&g, true, 52, 61, 720);
    CHECK_EQ(r.kind, GESTURE_LONG);
    CHECK(r.x == 50 && r.y == 60);
    CHECK_EQ(gesture_feed(&g, true, 52, 61, 2000).kind, GESTURE_NONE);
    CHECK_EQ(gesture_feed(&g, false, 0, 0, 2100).kind, GESTURE_NONE);

    static const int left[][3] = {{800, 300, 0}, {600, 310, 80}, {400, 320, 160}};
    CHECK_EQ(run(left, 3, 200).kind, GESTURE_SWIPE_LEFT);
    static const int right[][3] = {{100, 300, 0}, {400, 280, 160}};
    CHECK_EQ(run(right, 2, 200).kind, GESTURE_SWIPE_RIGHT);
    static const int up[][3] = {{500, 450, 0}, {510, 200, 160}};
    CHECK_EQ(run(up, 2, 200).kind, GESTURE_SWIPE_UP);
    static const int down[][3] = {{500, 100, 0}, {490, 400, 160}};
    CHECK_EQ(run(down, 2, 200).kind, GESTURE_SWIPE_DOWN);
    /* diagonal: neither */
    static const int diag[][3] = {{100, 100, 0}, {300, 300, 160}};
    CHECK_EQ(run(diag, 2, 200).kind, GESTURE_NONE);
    /* moved and came back slowly: not a long press, not a tap */
    static const int wander[][3] = {{100, 100, 0}, {180, 100, 300}, {100, 100, 800}};
    CHECK_EQ(run(wander, 3, 850).kind, GESTURE_NONE);
    return checks_done("test_gesture");
}
