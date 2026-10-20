/* Host tests for the low-battery guard (main/battery_guard.c). */
#include <stdbool.h>

#include "battery_guard.h"
#include "check.h"

static const bg_limits_t LIM = {.low_percent = 5, .low_mv = 3500, .resume_percent = 10};

static bg_reading_t bat(int percent, int mv)
{
    return (bg_reading_t){.present = true, .millivolts = mv, .percent = percent};
}

static void test_enter_needs_two_readings(void)
{
    battery_guard_t g;
    battery_guard_init(&g, &LIM);
    bg_reading_t r = bat(50, 3900);
    CHECK_EQ(battery_guard_update(&g, &r), BG_NONE);
    r = bat(5, 3600);
    CHECK_EQ(battery_guard_update(&g, &r), BG_NONE); /* one low reading is not enough */
    r = bat(6, 3600);
    CHECK_EQ(battery_guard_update(&g, &r), BG_NONE); /* the run is broken */
    r = bat(5, 3600);
    CHECK_EQ(battery_guard_update(&g, &r), BG_NONE);
    r = bat(4, 3600);
    CHECK_EQ(battery_guard_update(&g, &r), BG_ENTER_LOW);
    CHECK(g.low);
    CHECK_EQ(battery_guard_update(&g, &r), BG_NONE); /* reported once */
}

static void test_voltage_alone_is_enough(void)
{
    battery_guard_t g;
    battery_guard_init(&g, &LIM);
    bg_reading_t r = bat(40, 3450); /* the gauge is optimistic, the cell is not */
    battery_guard_update(&g, &r);
    CHECK_EQ(battery_guard_update(&g, &r), BG_ENTER_LOW);
}

static void test_hysteresis(void)
{
    battery_guard_t g;
    battery_guard_init(&g, &LIM);
    bg_reading_t r = bat(3, 3550);
    battery_guard_update(&g, &r);
    CHECK_EQ(battery_guard_update(&g, &r), BG_ENTER_LOW);
    r = bat(8, 3700); /* above low, below resume: still low */
    CHECK_EQ(battery_guard_update(&g, &r), BG_NONE);
    r = bat(10, 3450); /* percent back, voltage not */
    CHECK_EQ(battery_guard_update(&g, &r), BG_NONE);
    r = bat(10, 3750);
    CHECK_EQ(battery_guard_update(&g, &r), BG_LEAVE_LOW);
    CHECK(!g.low);
}

static void test_usb_power(void)
{
    battery_guard_t g;
    battery_guard_init(&g, &LIM);
    bg_reading_t r = bat(2, 3400);
    r.vbus = true;
    CHECK_EQ(battery_guard_update(&g, &r), BG_NONE); /* charging: never low */
    CHECK_EQ(battery_guard_update(&g, &r), BG_NONE);
    r.vbus = false;
    battery_guard_update(&g, &r);
    CHECK_EQ(battery_guard_update(&g, &r), BG_ENTER_LOW);
    r.vbus = true; /* plugged in: low ends at once */
    CHECK_EQ(battery_guard_update(&g, &r), BG_LEAVE_LOW);
}

static void test_unknown_and_absent(void)
{
    battery_guard_t g;
    battery_guard_init(&g, &LIM);
    bg_reading_t r = {.present = false};
    CHECK_EQ(battery_guard_update(&g, &r), BG_NONE);
    CHECK_EQ(battery_guard_update(&g, &r), BG_NONE);
    r = bat(-1, 3490); /* no gauge percent: voltage decides */
    battery_guard_update(&g, &r);
    CHECK_EQ(battery_guard_update(&g, &r), BG_ENTER_LOW);
    r = bat(-1, 3650);
    CHECK_EQ(battery_guard_update(&g, &r), BG_NONE); /* needs low_mv + hysteresis */
    r = bat(-1, 3700);
    CHECK_EQ(battery_guard_update(&g, &r), BG_LEAVE_LOW);
}

int main(void)
{
    test_enter_needs_two_readings();
    test_voltage_alone_is_enough();
    test_hysteresis();
    test_usb_power();
    test_unknown_and_absent();
    return checks_done("test_battery_guard");
}
