// A minimal test harness: TEST(name) { CHECK(...); } and a main() that runs them all
// (or those whose name contains argv[1]). No dependency, no network.
#pragma once

#include <cmath>
#include <cstdio>
#include <cstring>
#include <functional>
#include <string>
#include <vector>

namespace testlib {

struct Case {
    const char *name;
    std::function<void()> fn;
};

inline std::vector<Case> &cases() {
    static std::vector<Case> c;
    return c;
}
inline int &failures() {
    static int f = 0;
    return f;
}
inline int &checks() {
    static int c = 0;
    return c;
}

struct Register {
    Register(const char *name, std::function<void()> fn) { cases().push_back({name, std::move(fn)}); }
};

inline void fail(const char *file, int line, const std::string &what) {
    failures()++;
    std::fprintf(stderr, "%s:%d: FAILED: %s\n", file, line, what.c_str());
}

inline int run(int argc, char **argv) {
    const char *filter = argc > 1 ? argv[1] : nullptr;
    int ran = 0, failed_cases = 0;
    for (const Case &c : cases()) {
        if (filter && !std::strstr(c.name, filter)) continue;
        int before = failures();
        c.fn();
        ran++;
        bool ok = failures() == before;
        if (!ok) failed_cases++;
        std::printf("%s %s\n", ok ? "ok  " : "FAIL", c.name);
    }
    std::printf("%d tests, %d failed, %d checks\n", ran, failed_cases, checks());
    return failed_cases == 0 && ran > 0 ? 0 : 1;
}

}  // namespace testlib

#define TL_CAT2(a, b) a##b
#define TL_CAT(a, b) TL_CAT2(a, b)
#define TEST(name)                                                                   \
    static void name();                                                              \
    static testlib::Register TL_CAT(reg_, name)(#name, name);                         \
    static void name()

#define CHECK(cond)                                                                  \
    do {                                                                             \
        testlib::checks()++;                                                         \
        if (!(cond)) testlib::fail(__FILE__, __LINE__, #cond);                       \
    } while (0)

#define CHECK_MSG(cond, msg)                                                         \
    do {                                                                             \
        testlib::checks()++;                                                         \
        if (!(cond)) testlib::fail(__FILE__, __LINE__, std::string(#cond) + " -- " + (msg)); \
    } while (0)

#define CHECK_EQ(a, b)                                                               \
    do {                                                                             \
        testlib::checks()++;                                                         \
        auto tl_a = (a);                                                             \
        auto tl_b = (b);                                                             \
        if (!(tl_a == tl_b))                                                         \
            testlib::fail(__FILE__, __LINE__, std::string(#a " == " #b));            \
    } while (0)

#define CHECK_NEAR(a, b, eps)                                                        \
    do {                                                                             \
        testlib::checks()++;                                                         \
        double tl_a = (a), tl_b = (b);                                               \
        if (!(std::fabs(tl_a - tl_b) <= (eps)))                                      \
            testlib::fail(__FILE__, __LINE__,                                        \
                          std::string(#a " ~ " #b ": ") + std::to_string(tl_a) + " vs " + \
                              std::to_string(tl_b));                                 \
    } while (0)

#define TEST_MAIN()                                                                  \
    int main(int argc, char **argv) { return testlib::run(argc, argv); }
