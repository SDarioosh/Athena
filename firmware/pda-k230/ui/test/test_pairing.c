/* pairing.c: the pairing screen's state machine; a QR text becomes LOCAL `pair` with the
 * raw text in `url` (what syncd's daemon.pair parses), typed input is checked like syncd's
 * check_url / check_code, pair_result and the timeout. */
#include <string.h>

#include "check.h"
#include "local_msg.h"
#include "pairing.h"

#define LINK "odysseus-pair:v1?url=https%3A%2F%2Fhub.example.ts.net&code=Fict1onalPairCode_k230"

/* a QR code read while scanning, then yes to "Pair with …?" */
static void read_and_confirm(pair_flow_t *p, char *out, size_t cap, size_t *n)
{
    CHECK_EQ(pair_on_qr(p, LINK, out, cap, n), PAIR_ASK);
    CHECK_EQ(pair_confirm(p, out, cap, n), PAIR_SEND);
}

static void test_scan(void)
{
    pair_flow_t p;
    pair_reset(&p);
    CHECK_EQ(p.stage, PAIR_CHOOSE);
    CHECK(!pair_wants_camera(&p));
    char out[PAIR_LOCAL_MAX];
    size_t n = 99;

    /* a QR code before scanning: ignored */
    CHECK_EQ(pair_on_qr(&p, LINK, out, sizeof(out), &n), PAIR_IGNORED);
    CHECK_EQ(n, 0);

    pair_start_scan(&p);
    CHECK_EQ(p.stage, PAIR_SCANNING);
    CHECK(pair_wants_camera(&p));

    /* some other QR code: a hint, still scanning */
    CHECK_EQ(pair_on_qr(&p, "https://example.org/menu", out, sizeof(out), &n), PAIR_REJECTED);
    CHECK_EQ(p.stage, PAIR_SCANNING);
    CHECK(p.hint && strstr(p.msg, "isn't an Odysseus pairing code") != NULL);
    CHECK(pair_wants_camera(&p));

    /* the hub's code, with white space around it: first the question, nothing sent */
    CHECK_EQ(pair_on_qr(&p, "  " LINK "\n", out, sizeof(out), &n), PAIR_ASK);
    CHECK_EQ(n, 0);
    CHECK_EQ(p.stage, PAIR_CONFIRM);
    CHECK(strcmp(p.host, "hub.example.ts.net") == 0);
    CHECK(!pair_wants_camera(&p)); /* the camera goes off while the owner decides */
    CHECK_EQ(pair_on_qr(&p, LINK, out, sizeof(out), &n), PAIR_IGNORED);
    /* Cancel: back to scanning, still nothing sent */
    CHECK(pair_back(&p));
    CHECK_EQ(p.stage, PAIR_SCANNING);
    CHECK(pair_wants_camera(&p));
    CHECK_EQ(pair_confirm(&p, out, sizeof(out), &n), PAIR_IGNORED);
    CHECK_EQ(n, 0);
    /* again, and yes: sent as is, trimmed, in `url` */
    CHECK_EQ(pair_on_qr(&p, "  " LINK "\n", out, sizeof(out), &n), PAIR_ASK);
    CHECK_EQ(pair_confirm(&p, out, sizeof(out), &n), PAIR_SEND);
    CHECK_EQ(n, strlen(out));
    CHECK(strcmp(out, "{\"t\":\"pair\",\"url\":\"" LINK "\",\"code\":\"\"}") == 0);
    local_in_t dummy;
    CHECK(local_parse(out, n, &dummy)); /* a JSON object with a "t" */
    pair_sent(&p, true, 1000);
    CHECK_EQ(p.stage, PAIR_WAITING);
    CHECK(!pair_wants_camera(&p)); /* the camera goes off once the code is read */
    CHECK(!pair_back(&p));         /* can't leave while the hub may be redeeming it */
    pair_start_scan(&p);
    CHECK_EQ(p.stage, PAIR_WAITING);
    CHECK_EQ(pair_on_qr(&p, LINK, out, sizeof(out), &n), PAIR_IGNORED);

    /* syncd answers */
    CHECK(!pair_tick(&p, 1000 + PAIR_WAIT_MS - 1));
    CHECK(pair_on_result(&p, true, "Paired with hub.example.ts.net."));
    CHECK_EQ(p.stage, PAIR_OK);
    CHECK(strcmp(p.msg, "Paired with hub.example.ts.net.") == 0);
    CHECK(!p.hint);
    /* a second result (a late one) is not taken */
    CHECK(!pair_on_result(&p, false, "late"));
    CHECK_EQ(p.stage, PAIR_OK);
    CHECK(pair_back(&p));
    CHECK_EQ(p.stage, PAIR_CHOOSE);

    /* a refusal keeps syncd's words; an empty msg gets ours */
    pair_start_scan(&p);
    read_and_confirm(&p, out, sizeof(out), &n);
    pair_sent(&p, true, 5000);
    CHECK(pair_on_result(&p, false, "The pairing code was already used."));
    CHECK_EQ(p.stage, PAIR_FAILED);
    CHECK(strcmp(p.msg, "The pairing code was already used.") == 0);
    pair_start_scan(&p);
    read_and_confirm(&p, out, sizeof(out), &n);
    pair_sent(&p, true, 5000);
    CHECK(pair_on_result(&p, false, ""));
    CHECK(strstr(p.msg, "Pair a phone") != NULL);

    /* no answer */
    pair_start_scan(&p);
    read_and_confirm(&p, out, sizeof(out), &n);
    pair_sent(&p, true, 5000);
    CHECK(!pair_tick(&p, 5000 + PAIR_WAIT_MS - 1));
    CHECK(pair_tick(&p, 5000 + PAIR_WAIT_MS));
    CHECK_EQ(p.stage, PAIR_FAILED);
    CHECK(strstr(p.msg, "No answer") != NULL);
    CHECK(!pair_tick(&p, 5000 + 2 * PAIR_WAIT_MS));

    /* syncd isn't connected: the send fails */
    pair_start_scan(&p);
    read_and_confirm(&p, out, sizeof(out), &n);
    pair_sent(&p, false, 5000);
    CHECK_EQ(p.stage, PAIR_FAILED);
    CHECK(strstr(p.msg, "sync service isn't running") != NULL);

    /* an Odysseus code that is damaged or names no http(s) host: not even asked */
    static const char *const BAD[] = {
        "odysseus-pair:v2?url=https%3A%2F%2Fhub.example.ts.net&code=Fict1onalPairCode_k230",
        "odysseus-pair:v1?code=Fict1onalPairCode_k230",
        "odysseus-pair:v1?url=https%3A%2F%2Fhub.example.ts.net%2&code=Fict1onalPairCode_k230",
        "odysseus-pair:v1?url=https%3A%2F%2Fhub%00.example&code=Fict1onalPairCode_k230",
        "odysseus-pair:v1?url=ftp%3A%2F%2Fhub.example.ts.net&code=Fict1onalPairCode_k230",
        "odysseus-pair:v1?url=https%3A%2F%2Fu%40hub.example.ts.net&code=Fict1onalPairCode_k230",
    };
    for (size_t i = 0; i < sizeof(BAD) / sizeof(BAD[0]); i++) {
        pair_start_scan(&p);
        CHECK_EQ(pair_on_qr(&p, BAD[i], out, sizeof(out), &n), PAIR_REJECTED);
        CHECK_EQ(p.stage, PAIR_SCANNING);
        CHECK(p.hint);
    }
    /* the first url= is the one asked about (syncd takes the first too) */
    pair_start_scan(&p);
    CHECK_EQ(pair_on_qr(&p, "odysseus-pair:v1?url=http%3A%2F%2F%5Bfd7a%3A%3A1%5D%3A7000&url=https%3A%2F%2Fevil"
                            "&code=Fict1onalPairCode_k230", out, sizeof(out), &n), PAIR_ASK);
    CHECK(strcmp(p.host, "fd7a::1") == 0);
    pair_back(&p);

    /* a QR text too long for a LOCAL frame: not sent */
    static char big[PAIR_LOCAL_MAX];
    memset(big, 'a', sizeof(big) - 1);
    memcpy(big, PAIR_PREFIX, strlen(PAIR_PREFIX));
    big[sizeof(big) - 1] = 0;
    pair_start_scan(&p);
    CHECK_EQ(pair_on_qr(&p, big, out, sizeof(out), &n), PAIR_REJECTED);
    CHECK_EQ(p.stage, PAIR_SCANNING);
}

static void test_typed(void)
{
    pair_flow_t p;
    pair_reset(&p);
    char out[PAIR_LOCAL_MAX];
    size_t n;
    CHECK_EQ(pair_submit(&p, "https://hub.example.ts.net", "Fict1onalPairCode_k230", out, sizeof(out), &n),
             PAIR_IGNORED);
    pair_start_typing(&p);
    CHECK_EQ(p.stage, PAIR_TYPING);
    CHECK(!pair_wants_camera(&p));

    CHECK_EQ(pair_submit(&p, "hub.example.ts.net", "Fict1onalPairCode_k230", out, sizeof(out), &n), PAIR_REJECTED);
    CHECK(strstr(p.msg, "http://") != NULL);
    CHECK_EQ(p.stage, PAIR_TYPING);
    CHECK_EQ(pair_submit(&p, "https://hub.example.ts.net", "short", out, sizeof(out), &n), PAIR_REJECTED);
    CHECK(strstr(p.msg, "8 to 100") != NULL);
    CHECK_EQ(pair_submit(&p, "https://hub.example.ts.net", "bad code here", out, sizeof(out), &n), PAIR_REJECTED);
    CHECK_EQ(pair_submit(&p, "https://", "Fict1onalPairCode_k230", out, sizeof(out), &n), PAIR_REJECTED);

    /* fine: trimmed, the trailing slash gone; first the question */
    CHECK_EQ(pair_submit(&p, " https://hub.example.ts.net:7000/ ", " Fict1onalPairCode_k230 ", out, sizeof(out), &n),
             PAIR_ASK);
    CHECK_EQ(n, 0);
    CHECK(strcmp(p.host, "hub.example.ts.net") == 0);
    CHECK(pair_back(&p)); /* Cancel goes back to the fields */
    CHECK_EQ(p.stage, PAIR_TYPING);
    CHECK_EQ(pair_submit(&p, " https://hub.example.ts.net:7000/ ", " Fict1onalPairCode_k230 ", out, sizeof(out), &n),
             PAIR_ASK);
    CHECK_EQ(pair_confirm(&p, out, sizeof(out), &n), PAIR_SEND);
    CHECK(strcmp(out, "{\"t\":\"pair\",\"url\":\"https://hub.example.ts.net:7000\",\"code\":\"Fict1onalPairCode_k230\"}") == 0);
    pair_sent(&p, true, 10);
    CHECK_EQ(p.stage, PAIR_WAITING);

    CHECK(pair_check_url("http://100.64.0.7:7000") == NULL);
    CHECK(pair_check_url("ftp://h") != NULL);
    CHECK(pair_check_url("http://u:p@h") != NULL);
    CHECK(pair_check_url("http://h/?q=1") != NULL);
    CHECK(pair_check_url("http://h #x") != NULL);
    CHECK(pair_check_url("http://:7000") != NULL);
    CHECK(pair_check_code("Fict1onal-Pair_Code") == NULL);
    CHECK(pair_check_code("Fict1onal.Pair") != NULL);
    CHECK(pair_check_code("") != NULL);

    char host[16];
    CHECK(pair_url_host("https://[fd7a:115c::1]:7000/x", host, sizeof(host)));
    CHECK(strcmp(host, "fd7a:115c::1") == 0);
    CHECK(pair_url_host("http://h\x01st/", host, sizeof(host)));
    CHECK(strcmp(host, "h?st") == 0);
    CHECK(pair_url_host("http://a-very-long-host-name.example", host, sizeof(host)));
    CHECK_EQ(strlen(host), sizeof(host) - 1);
    CHECK(!pair_url_host("http:///x", host, sizeof(host)));
    CHECK(!pair_url_host("hub.example", host, sizeof(host)));

    /* back from typing */
    pair_reset(&p);
    pair_start_typing(&p);
    CHECK(pair_back(&p));
    CHECK_EQ(p.stage, PAIR_CHOOSE);
}

int main(void)
{
    test_scan();
    test_typed();
    return checks_done("test_pairing");
}
