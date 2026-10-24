#include "check.h"
#include "isotime.h"

#include <string.h>

int main(void)
{
    int64_t e = 0;
    CHECK_EQ(isotime_days_from_civil(1970, 1, 1), 0);
    CHECK_EQ(isotime_days_from_civil(2000, 3, 1), 11017);

    CHECK(isotime_parse("2026-10-19T12:34:56Z", &e));
    CHECK_EQ(e, 1792413296LL);
    CHECK(isotime_parse("2026-10-19T12:34:56", &e));
    CHECK_EQ(e, 1792413296LL);
    CHECK(isotime_parse("2026-10-19 12:34:56", &e));
    CHECK_EQ(e, 1792413296LL);
    CHECK(isotime_parse("1792413296", &e));
    CHECK_EQ(e, 1792413296LL);
    CHECK(isotime_parse("1970-01-01T00:00:00Z", &e));
    CHECK_EQ(e, 0);
    CHECK(isotime_parse("2024-02-29T23:59:59Z", &e)); /* leap day */
    CHECK_EQ(e, 1709251199LL);

    CHECK(!isotime_parse("2023-02-29T00:00:00Z", &e)); /* not a leap year */
    CHECK(!isotime_parse("2026-13-01T00:00:00Z", &e));
    CHECK(!isotime_parse("2026-10-19T24:00:00Z", &e));
    CHECK(!isotime_parse("2026-10-19T12:34:56+02:00", &e)); /* no offsets */
    CHECK(!isotime_parse("now", &e));
    CHECK(!isotime_parse("", &e));
    CHECK(!isotime_parse("99999999999", &e)); /* beyond 2100 */
    /* QC R14: sscanf took leading spaces and signs in every field and stopped at a space after
     * the "Z", so these all parsed. The form is exact now. */
    e = 42;
    CHECK(!isotime_parse("2026-10-24T12:00:00Z GARBAGE", &e));
    CHECK(!isotime_parse("2026-10-24T12:00:00 Z", &e));
    CHECK(!isotime_parse("2026-10-24T12:00:00\tx", &e));
    CHECK(!isotime_parse(" 2026-10-24T12:00:00Z", &e));
    CHECK(!isotime_parse("+2026-10-24T12:00:00Z", &e));
    CHECK(!isotime_parse("2026-+1-24T12:00:00Z", &e));
    CHECK(!isotime_parse("2026- 1-24T12:00:00Z", &e));
    CHECK(!isotime_parse("2026-10-24T 1:00:00Z", &e));
    CHECK(!isotime_parse("2026-10-24T12:-1:00Z", &e));
    CHECK(!isotime_parse("2026-1-24T12:00:00Z", &e));
    CHECK(!isotime_parse("-999-10-24T12:00:00Z", &e));
    CHECK(!isotime_parse("2026-10-24T12:00", &e));
    CHECK(!isotime_parse("2026-10-24T12:00:00ZZ", &e));
    CHECK(!isotime_parse("2026-10-24X12:00:00Z", &e));
    CHECK_EQ(e, 42); /* untouched on failure */
    CHECK(isotime_parse("2026-10-19t12:34:56z", &e)); /* lower case still fine */
    CHECK_EQ(e, 1792413296LL);
    /* No signed overflow at the ends of int (UBSan would flag the old year - 1). */
    isotime_days_from_civil(-2147483647 - 1, 1, 1);
    isotime_days_from_civil(2147483647, 12, 31);

    isotime_fields_t f;
    isotime_to_fields(1792413296LL, &f);
    CHECK_EQ(f.year, 2026);
    CHECK_EQ(f.month, 10);
    CHECK_EQ(f.day, 19);
    CHECK_EQ(f.hour, 12);
    CHECK_EQ(f.minute, 34);
    CHECK_EQ(f.second, 56);

    /* Round trip over a range of dates, including leap years and month ends. */
    for (int64_t t = 946684800LL; t < 4102444800LL; t += 86400LL * 37 + 12345) {
        int64_t back = -1;
        isotime_to_fields(t, &f);
        CHECK(isotime_from_fields(&f, &back));
        CHECK_EQ(back, t);
    }

    char buf[32];
    isotime_format(1792413296LL, buf, sizeof(buf));
    CHECK(strcmp(buf, "2026-10-19T12:34:56Z") == 0);
    isotime_format(0, buf, sizeof(buf));
    CHECK(strcmp(buf, "1970-01-01T00:00:00Z") == 0);

    CHECK(isotime_is_plausible(1792413296LL));
    CHECK(!isotime_is_plausible(946684800LL)); /* 2000-01-01: RTC power-up value */
    CHECK(!isotime_is_plausible(0));

    return checks_done("test_isotime");
}
