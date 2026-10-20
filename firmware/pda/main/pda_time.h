/* Times in the feed (docs/t5-pda-plan.md §4): ISO-8601 with "Z", a "+HH:MM" offset or none
 * (UTC), optional fractional seconds, or a plain date. Display uses the phone's UTC offset
 * from the last TIME command. Pure C; host-tested (test/test_feed.c). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* "2026-10-20T07:00:00Z", "2026-10-20T09:00:00+02:00", "2026-10-20T07:00:00.250Z",
 * "2026-10-20 07:00", "2026-10-20" (date_only: midnight UTC of that day). */
bool pda_time_parse(const char *s, int64_t *epoch, bool *date_only);

/* Days since 1970-01-01 of a local time (epoch + offset). */
int32_t pda_local_day(int64_t epoch, int tz_min);

/* "Mon 20 Oct", "09:05", "Mon 20 Oct 09:05" in local time. */
void pda_fmt_date(int64_t epoch, int tz_min, char *out, size_t cap);
void pda_fmt_hm(int64_t epoch, int tz_min, char *out, size_t cap);
/* A day number as "Mon 20 Oct". */
void pda_fmt_day(int32_t day, char *out, size_t cap);
/* "YYYY-MM-DDTHH:MM:SS+HH:MM" in local time (for cal_add). */
void pda_fmt_iso_local(int64_t epoch, int tz_min, char *out, size_t cap);
/* "YYYY-MM-DD" of a day number. */
void pda_fmt_ymd(int32_t day, char *out, size_t cap);
/* "just now", "5 min ago", "3 h ago", "2 d ago". */
void pda_fmt_age(int64_t seconds, char *out, size_t cap);

#ifdef __cplusplus
}
#endif
