/* Calendar helpers (proleptic Gregorian, UTC only). Pure C; host-tested.
 * Timestamps are UTC epoch seconds internally and ISO-8601 at the edges, as in the server. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    int year;   /* e.g. 2026 */
    int month;  /* 1..12 */
    int day;    /* 1..31 */
    int hour;   /* 0..23 */
    int minute; /* 0..59 */
    int second; /* 0..60 */
} isotime_fields_t;

/* Days since 1970-01-01 for a civil date (Howard Hinnant's algorithm). */
int64_t isotime_days_from_civil(int year, int month, int day);

/* Fields -> epoch seconds. Returns false if a field is out of range. */
bool isotime_from_fields(const isotime_fields_t *f, int64_t *epoch);

/* Epoch seconds -> fields. */
void isotime_to_fields(int64_t epoch, isotime_fields_t *f);

/* Parse "YYYY-MM-DDTHH:MM:SS" with optional "Z", a space instead of "T", or a plain
 * decimal epoch. Returns false on anything else. */
bool isotime_parse(const char *s, int64_t *epoch);

/* Format as "YYYY-MM-DDTHH:MM:SSZ". buf needs at least 21 bytes. */
void isotime_format(int64_t epoch, char *buf, unsigned buf_len);

/* True if the epoch looks like a real wall-clock time (set at least once). */
bool isotime_is_plausible(int64_t epoch);
