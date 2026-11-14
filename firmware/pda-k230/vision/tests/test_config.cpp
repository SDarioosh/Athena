// Config file and command line.
#include <cstdio>
#include <cstdlib>
#include <unistd.h>
#include <fstream>

#include "config.h"
#include "testlib.h"

TEST(set_every_type) {
    av::Config c;
    std::string err;
    CHECK(av::config_set(c, "swipe_min_dist_x", "0.3", &err));
    CHECK_NEAR(c.tracker.swipe_min_dist_x, 0.3, 1e-6);
    CHECK(av::config_set(c, "palm-hold-s", " 1.25 ", &err));
    CHECK_NEAR(c.tracker.palm_hold_s, 1.25, 1e-9);
    CHECK(av::config_set(c, "video_device", "3", &err));
    CHECK_EQ(c.device.video_device, 3);
    CHECK(av::config_set(c, "mirror", "false", &err));
    CHECK(!c.orientation.mirror);
    CHECK(av::config_set(c, "rotate", "270", &err));
    CHECK_EQ(c.orientation.rotate, 270);
    CHECK(av::config_set(c, "classifier", "canaan", &err));
    CHECK(c.classifier == av::Classifier::Canaan);
    CHECK(av::config_set(c, "model_dir", "/opt/models", &err));
    CHECK_EQ(c.device.model_dir, std::string("/opt/models"));
}

TEST(rejects_bad_values) {
    av::Config c;
    std::string err;
    CHECK(!av::config_set(c, "rotate", "45", &err));
    CHECK(!err.empty());
    CHECK(!av::config_set(c, "rotate", "ninety", &err));
    CHECK(!av::config_set(c, "mirror", "maybe", &err));
    CHECK(!av::config_set(c, "palm_hold_s", "1.0s", &err));
    CHECK(!av::config_set(c, "palm_hold_s", "", &err));
    CHECK(!av::config_set(c, "palm_hold_s", "nan", &err));
    CHECK(!av::config_set(c, "width", "99999999999", &err));
    CHECK(!av::config_set(c, "classifier", "magic", &err));
    CHECK(!av::config_set(c, "no_such_key", "1", &err));
    CHECK(err.find("no_such_key") != std::string::npos);
    CHECK_EQ(c.orientation.rotate, 90);  // unchanged
}

TEST(text_file_with_comments_and_errors) {
    av::Config c;
    std::vector<std::string> errs;
    av::config_load_text(c,
                         "# a comment\n"
                         "\n"
                         "rotate = 180   # trailing comment\n"
                         "swipe_min_speed=1.1\n"
                         "this line is wrong\n"
                         "unknown = 3\n"
                         "  mirror =  off  \r\n",
                         &errs);
    CHECK_EQ(c.orientation.rotate, 180);
    CHECK_NEAR(c.tracker.swipe_min_speed, 1.1, 1e-6);
    CHECK(!c.orientation.mirror);
    CHECK_EQ(errs.size(), size_t(2));
}

TEST(dump_reloads_to_the_same) {
    av::Config a;
    a.tracker.palm_hold_s = 0.95;
    a.orientation.rotate = 270;
    a.classifier = av::Classifier::Canaan;
    a.device.socket_path = "/tmp/x.sock";
    std::string dump = av::config_dump(a);
    av::Config b;
    std::vector<std::string> errs;
    av::config_load_text(b, dump, &errs);
    CHECK(errs.empty());
    CHECK_EQ(av::config_dump(b), dump);
    CHECK(dump.find("palm_hold_s = 0.95") != std::string::npos);
}

TEST(command_line) {
    char path[] = "/tmp/athena_vision_test_conf_XXXXXX";
    int fd = mkstemp(path);
    CHECK(fd >= 0);
    {
        std::ofstream f(path);
        f << "rotate = 180\nfist_hold_s = 0.7\n";
    }
    av::Config c;
    const char *argv[] = {"athena-vision", "--fist_hold_s", "0.9", "--config", path,
                          "--mirror", "--rotate=0", "--list-keys"};
    av::CliResult r = av::config_parse_cli(c, 8, argv, "/nonexistent/default.conf");
    CHECK(r.ok);
    CHECK(r.list_keys);
    CHECK_EQ(r.config_path, std::string(path));
    CHECK_EQ(c.orientation.rotate, 0);         // flag after the file wins
    CHECK_NEAR(c.tracker.fist_hold_s, 0.9, 1e-9);  // flags win over the file wherever they are
    CHECK(c.orientation.mirror);               // bare boolean flag
    std::remove(path);

    av::Config d;
    const char *bad[] = {"athena-vision", "--rotate", "33", "stray", "--width"};
    r = av::config_parse_cli(d, 5, bad, "");
    CHECK(!r.ok);
    CHECK_EQ(r.errors.size(), size_t(3));

    av::Config e;
    const char *missing[] = {"athena-vision", "--config", "/nonexistent/x.conf"};
    r = av::config_parse_cli(e, 3, missing, "");
    CHECK(!r.ok);  // an explicit config file must exist
    av::Config f2;
    const char *none[] = {"athena-vision"};
    r = av::config_parse_cli(f2, 1, none, "/nonexistent/default.conf");
    CHECK(r.ok);   // the default one may be missing
}

TEST_MAIN()
