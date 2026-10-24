#include "isotime.h"

#include <stdio.h>
#include <string.h>

int64_t isotime_days_from_civil(int year_in, int month, int day)
{
    /* In 64 bits: year - 1 and year - 399 overflow an int at its low end. */
    const int64_t year = (int64_t)year_in - (month <= 2);
    const int64_t era = (year >= 0 ? year : year - 399) / 400;
    const int64_t yoe = year - era * 400;
    const int64_t doy = (153 * (month + (month > 2 ? -3 : 9)) + 2) / 5 + day - 1;
    const int64_t doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + doe - 719468;
}

static int days_in_month(int year, int month)
{
    static const int table[12] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    if (month == 2) {
        const bool leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
        return leap ? 29 : 28;
    }
    return table[month - 1];
}

bool isotime_from_fields(const isotime_fields_t *f, int64_t *epoch)
{
    if (f->month < 1 || f->month > 12 || f->day < 1 || f->day > days_in_month(f->year, f->month) ||
        f->hour < 0 || f->hour > 23 || f->minute < 0 || f->minute > 59 || f->second < 0 ||
        f->second > 60) {
        return false;
    }
    const int64_t days = isotime_days_from_civil(f->year, f->month, f->day);
    *epoch = days * 86400 + f->hour * 3600 + f->minute * 60 + f->second;
    return true;
}

void isotime_to_fields(int64_t epoch, isotime_fields_t *f)
{
    int64_t days = epoch / 86400;
    int64_t rem = epoch % 86400;
    if (rem < 0) {
        rem += 86400;
        days -= 1;
    }
    /* civil_from_days */
    days += 719468;
    const int64_t era = (days >= 0 ? days : days - 146096) / 146097;
    const int64_t doe = days - era * 146097;
    const int64_t yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    const int64_t y = yoe + era * 400;
    const int64_t doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    const int64_t mp = (5 * doy + 2) / 153;
    const int d = (int)(doy - (153 * mp + 2) / 5 + 1);
    const int m = (int)(mp < 10 ? mp + 3 : mp - 9);
    f->year = (int)(y + (m <= 2));
    f->month = m;
    f->day = d;
    f->hour = (int)(rem / 3600);
    f->minute = (int)((rem % 3600) / 60);
    f->second = (int)(rem % 60);
}

static bool all_digits(const char *s)
{
    if (*s == '\0') {
        return false;
    }
    for (; *s; s++) {
        if (*s < '0' || *s > '9') {
            return false;
        }
    }
    return true;
}

static int digits(const char *s, int n)
{
    int v = 0;
    for (int i = 0; i < n; i++) {
        v = v * 10 + (s[i] - '0');
    }
    return v;
}

bool isotime_parse(const char *s, int64_t *epoch)
{
    if (s == NULL || epoch == NULL) {
        return false;
    }
    if (all_digits(s)) {
        int64_t v = 0;
        for (; *s; s++) {
            v = v * 10 + (*s - '0');
            if (v > 4102444800LL) { /* 2100-01-01 */
                return false;
            }
        }
        *epoch = v;
        return true;
    }
    /* Exactly "YYYY-MM-DD", T/t/space, "HH:MM:SS", then an optional Z/z and the end: no
     * signs, spaces or anything after (sscanf allowed all three). */
    static const char form[] = "dddd-dd-ddTdd:dd:dd";
    const size_t len = strlen(s);
    const size_t base = sizeof(form) - 1;
    if (len != base && !(len == base + 1 && (s[base] == 'Z' || s[base] == 'z'))) {
        return false;
    }
    for (size_t i = 0; i < base; i++) {
        const char c = s[i];
        const bool ok = form[i] == 'd'   ? (c >= '0' && c <= '9')
                        : form[i] == 'T' ? (c == 'T' || c == 't' || c == ' ')
                                         : c == form[i];
        if (!ok) {
            return false;
        }
    }
    const isotime_fields_t f = {
        .year = digits(s, 4),
        .month = digits(s + 5, 2),
        .day = digits(s + 8, 2),
        .hour = digits(s + 11, 2),
        .minute = digits(s + 14, 2),
        .second = digits(s + 17, 2),
    };
    return isotime_from_fields(&f, epoch);
}

void isotime_format(int64_t epoch, char *buf, unsigned buf_len)
{
    isotime_fields_t f;
    isotime_to_fields(epoch, &f);
    snprintf(buf, buf_len, "%04d-%02d-%02dT%02d:%02d:%02dZ", f.year, f.month, f.day, f.hour,
             f.minute, f.second);
}

bool isotime_is_plausible(int64_t epoch)
{
    /* Between 2024-01-01 and 2100-01-01. The RTC powers up at 2000 or so. */
    return epoch >= 1704067200LL && epoch < 4102444800LL;
}
