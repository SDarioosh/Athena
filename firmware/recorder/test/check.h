/* Tiny assertion helper for the host tests: no framework, just exit codes. */
#pragma once

#include <stdio.h>
#include <stdlib.h>

static int checks_failed = 0;

#define CHECK(cond)                                                                        \
    do {                                                                                   \
        if (!(cond)) {                                                                     \
            fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond);       \
            checks_failed++;                                                               \
        }                                                                                  \
    } while (0)

#define CHECK_EQ(a, b)                                                                     \
    do {                                                                                   \
        long long _a = (long long)(a), _b = (long long)(b);                                \
        if (_a != _b) {                                                                    \
            fprintf(stderr, "%s:%d: CHECK_EQ failed: %s == %s (%lld != %lld)\n", __FILE__, \
                    __LINE__, #a, #b, _a, _b);                                             \
            checks_failed++;                                                               \
        }                                                                                  \
    } while (0)

static inline int checks_done(const char *name)
{
    if (checks_failed) {
        fprintf(stderr, "%s: %d check(s) failed\n", name, checks_failed);
        return EXIT_FAILURE;
    }
    printf("%s: ok\n", name);
    return EXIT_SUCCESS;
}
