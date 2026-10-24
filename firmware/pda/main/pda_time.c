#include "pda_time.h"

#include <stdio.h>
#include <string.h>

#include "isotime.h"

static bool digits(const char *s, int n, int *out)
{
    int v = 0;
    for (int i = 0; i < n; i++) {
        if (s[i] < '0' || s[i] > '9') {
            return false;
        }
        v = v * 10 + (s[i] - '0');
    }
    *out = v;
    return true;
}

bool pda_time_parse(const char *s, int64_t *epoch, bool *date_only)
{
    if (!s) {
        return false;
    }
    isotime_fields_t f = {0};
    const size_t n = strlen(s);
    bool dummy;
    if (!date_only) {
        date_only = &dummy;
    }
    *date_only = false;
    if (n < 10 || !digits(s, 4, &f.year) || s[4] != '-' || !digits(s + 5, 2, &f.month) ||
        s[7] != '-' || !digits(s + 8, 2, &f.day)) {
        return false;
    }
    if (n == 10) {
        *date_only = true;
        return isotime_from_fields(&f, epoch);
    }
    const char *p = s + 10;
    if ((*p != 'T' && *p != ' ') || strlen(p) < 6 || !digits(p + 1, 2, &f.hour) || p[3] != ':' ||
        !digits(p + 4, 2, &f.minute)) {
        return false;
    }
    p += 6;
    if (*p == ':') {
        if (!digits(p + 1, 2, &f.second)) {
            return false;
        }
        p += 3;
        if (*p == '.') { /* fractional seconds: ignored, but at least one digit */
            p++;
            if (*p < '0' || *p > '9') {
                return false;
            }
            while (*p >= '0' && *p <= '9') {
                p++;
            }
        }
    }
    int offset_min = 0;
    if (*p == 'Z' || *p == 'z') {
        p++;
    } else if (*p == '+' || *p == '-') {
        int hh, mm = 0;
        if (!digits(p + 1, 2, &hh)) {
            return false;
        }
        const char *q = p + 3;
        if (*q == ':') {
            q++;
            if (!*q) {
                return false; /* "+02:" */
            }
        }
        if (*q) {
            if (!digits(q, 2, &mm)) {
                return false;
            }
            q += 2;
        }
        if (mm > 59 || hh * 60 + mm > 14 * 60) {
            return false; /* within ±14:00, as the T5's own clock (PDA_TZ_MAX_MIN) */
        }
        offset_min = (hh * 60 + mm) * (*p == '-' ? -1 : 1);
        p = q;
    }
    if (*p) {
        return false;
    }
    if (!isotime_from_fields(&f, epoch)) {
        return false;
    }
    *epoch -= (int64_t)offset_min * 60;
    return true;
}

static int64_t floor_div(int64_t a, int64_t b)
{
    /* b > 0; C division truncates toward zero, so step down for a negative remainder (no
     * negation of a, which overflows at INT64_MIN) */
    const int64_t q = a / b;
    return (a % b != 0 && a < 0) ? q - 1 : q;
}

int32_t pda_local_day(int64_t epoch, int tz_min)
{
    return (int32_t)floor_div(epoch + (int64_t)tz_min * 60, 86400);
}

static const char *const WDAY[] = {"Thu", "Fri", "Sat", "Sun", "Mon", "Tue", "Wed"};
static const char *const MON[] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};

void pda_fmt_day(int32_t day, char *out, size_t cap)
{
    isotime_fields_t f;
    isotime_to_fields((int64_t)day * 86400, &f);
    const int w = (int)(((day % 7) + 7) % 7);
    snprintf(out, cap, "%s %d %s", WDAY[w], f.day, MON[(f.month - 1) % 12]);
}

void pda_fmt_date(int64_t epoch, int tz_min, char *out, size_t cap)
{
    pda_fmt_day(pda_local_day(epoch, tz_min), out, cap);
}

void pda_fmt_hm(int64_t epoch, int tz_min, char *out, size_t cap)
{
    isotime_fields_t f;
    isotime_to_fields(epoch + (int64_t)tz_min * 60, &f);
    snprintf(out, cap, "%02d:%02d", f.hour, f.minute);
}

void pda_fmt_iso_local(int64_t epoch, int tz_min, char *out, size_t cap)
{
    isotime_fields_t f;
    isotime_to_fields(epoch + (int64_t)tz_min * 60, &f);
    const int a = tz_min < 0 ? -tz_min : tz_min;
    snprintf(out, cap, "%04d-%02d-%02dT%02d:%02d:%02d%c%02d:%02d", f.year, f.month, f.day, f.hour,
             f.minute, f.second, tz_min < 0 ? '-' : '+', a / 60, a % 60);
}

void pda_fmt_ymd(int32_t day, char *out, size_t cap)
{
    isotime_fields_t f;
    isotime_to_fields((int64_t)day * 86400, &f);
    snprintf(out, cap, "%04d-%02d-%02d", f.year, f.month, f.day);
}

void pda_fmt_age(int64_t s, char *out, size_t cap)
{
    if (s < 90) {
        snprintf(out, cap, "just now");
    } else if (s < 3600) {
        snprintf(out, cap, "%d min ago", (int)(s / 60));
    } else if (s < 48 * 3600) {
        snprintf(out, cap, "%d h ago", (int)(s / 3600));
    } else {
        snprintf(out, cap, "%d d ago", (int)(s / 86400));
    }
}
