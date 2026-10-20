/* Host tests for the power log samples (main/power_samples.c). */
#include <string.h>

#include "check.h"
#include "power_samples.h"

static ps_sample_t at(uint32_t uptime, int percent, uint8_t flags)
{
    return (ps_sample_t){.epoch = 1792411200 + uptime, .uptime_s = uptime, .percent = (int16_t)percent,
                         .mv = 3900, .flags = flags};
}

static void test_ring(void)
{
    ps_sample_t buf[3];
    ps_ring_t r;
    ps_ring_init(&r, buf, 3);
    ps_sample_t s;
    CHECK(!ps_ring_get(&r, 0, &s));
    CHECK(!ps_ring_peek_unwritten(&r, &s));
    for (uint32_t i = 1; i <= 4; i++) {
        const ps_sample_t x = at(i * 300, 100 - (int)i, 0);
        ps_ring_push(&r, &x);
    }
    CHECK_EQ(r.count, 3);
    CHECK(ps_ring_get(&r, 0, &s) && s.uptime_s == 600); /* the oldest one went */
    CHECK(ps_ring_get(&r, 2, &s) && s.uptime_s == 1200);
    CHECK_EQ(r.unwritten, 3);
    CHECK(ps_ring_peek_unwritten(&r, &s) && s.uptime_s == 600);
    ps_ring_mark_written(&r);
    ps_ring_mark_written(&r);
    CHECK(ps_ring_peek_unwritten(&r, &s) && s.uptime_s == 1200);
    ps_ring_mark_written(&r);
    CHECK(!ps_ring_peek_unwritten(&r, &s));
    const ps_sample_t y = at(1500, 95, 0);
    ps_ring_push(&r, &y);
    CHECK(ps_ring_peek_unwritten(&r, &s) && s.uptime_s == 1500);
}

static void test_csv(void)
{
    char line[96];
    ps_sample_t s = at(0, 81, PS_RECORDING | PS_BLE_CONN | PS_SD | PS_USB | PS_CHARGING);
    s.mv = 3950;
    CHECK_EQ(ps_csv_line(&s, line, sizeof(line)), strlen(line));
    CHECK(strcmp(line, "2026-10-19T12:00:00Z,0,81,3950,1,1,rec+ble_conn+sd\n") == 0);
    s = (ps_sample_t){.uptime_s = 7, .percent = -1};
    ps_csv_line(&s, line, sizeof(line));
    CHECK(strcmp(line, ",7,-1,0,0,0,idle\n") == 0); /* no clock: empty time */
    CHECK_EQ(ps_csv_line(&s, line, 5), 0);
    char state[48];
    ps_state_name(PS_NOTE | PS_SCREEN | PS_BLE_ADV, state, sizeof(state));
    CHECK(strcmp(state, "note+ble_adv+screen") == 0);
    CHECK(strcmp(PS_CSV_HEADER, "time,uptime_s,percent,mv,charging,usb,state\n") == 0);
}

static void test_drain(void)
{
    ps_sample_t buf[16];
    ps_ring_t r;
    ps_ring_init(&r, buf, 16);
    double rate = 0;
    uint32_t span = 0;
    CHECK(!ps_drain_rate(&r, 1800, &rate, &span));
    /* On USB, then 2 h on battery from 100 % to 90 %. */
    ps_sample_t s = at(0, 100, PS_USB);
    ps_ring_push(&r, &s);
    for (uint32_t t = 600; t <= 600 + 7200; t += 1800) {
        s = at(t, 100 - (int)((t - 600) / 720), PS_RECORDING);
        ps_ring_push(&r, &s);
    }
    CHECK(ps_drain_rate(&r, 1800, &rate, &span));
    CHECK_EQ(span, 7200);
    CHECK(rate > 4.99 && rate < 5.01); /* 10 % in 2 h */
    CHECK(!ps_drain_rate(&r, 7201, &rate, &span)); /* too short a run */
    /* A reboot (uptime goes back) starts a new run. */
    s = at(60, 89, 0);
    ps_ring_push(&r, &s);
    CHECK(!ps_drain_rate(&r, 1, &rate, &span));
    /* Back on USB: no rate. */
    s = at(4000, 88, PS_USB | PS_CHARGING);
    ps_ring_push(&r, &s);
    CHECK(!ps_drain_rate(&r, 0, &rate, &span));
}

int main(void)
{
    test_ring();
    test_csv();
    test_drain();
    return checks_done("test_power_samples");
}
