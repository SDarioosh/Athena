// Host tests for the bot's SDK-free logic: arguments and environment, the join/wait/leave
// rules, and the participant diff. No Zoom SDK, no network. Fictional data only.
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <vector>

#include "options.h"
#include "participants.h"
#include "rules.h"

using namespace athena::bot;

static int g_failures = 0;
#define CHECK(cond)                                                       \
  do {                                                                    \
    if (!(cond)) {                                                        \
      std::fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond); \
      ++g_failures;                                                       \
    }                                                                     \
  } while (0)
#define CHECK_EQ(a, b) CHECK((a) == (b))

namespace {

std::vector<std::string> launcher_args() {
  return {"--meeting-id", "81234567890", "--session-id", "zoom-abc123", "--upload-url",
          "http://127.0.0.1:8000", "--name", "Athena", "--max-seconds", "14400",
          "--alone-seconds", "60", "--waiting-room-seconds", "600", "--codec", "opus",
          "--work-dir", "/tmp/athena-test-meeting"};
}

std::map<std::string, std::string> base_env() {
  return {{"ATHENA_MEETING_JWT", "jwt-placeholder"}, {"ATHENA_UPLOAD_TOKEN", "upload-placeholder"}};
}

ParseResult parse(const std::vector<std::string>& args, const std::map<std::string, std::string>& env) {
  return parse_options(args, [&env](const char* n) -> const char* {
    auto it = env.find(n);
    return it == env.end() ? nullptr : it->second.c_str();
  });
}

void test_options() {
  auto r = parse(launcher_args(), base_env());
  CHECK(r.options.has_value());
  const Options& o = *r.options;
  CHECK_EQ(o.meeting_id, "81234567890");
  CHECK_EQ(o.session_id, "zoom-abc123");
  CHECK_EQ(o.upload_url, "http://127.0.0.1:8000");
  CHECK_EQ(o.max_seconds, 14400);
  CHECK_EQ(o.alone_seconds, 60);
  CHECK_EQ(o.waiting_room_seconds, 600);
  CHECK_EQ(o.codec, "opus");
  CHECK_EQ(o.work_dir, "/tmp/athena-test-meeting");
  CHECK(o.passcode.empty());
  CHECK(!o.obf() && !o.zak());
  CHECK(!o.sdk_log);

  auto env = base_env();
  env["ATHENA_MEETING_PASSCODE"] = "s3cr3t-passcode";
  env["ATHENA_ZOOM_JOIN_TOKEN"] = "obf-placeholder";
  env["ATHENA_ZOOM_JOIN_TOKEN_KIND"] = "obf";
  r = parse(launcher_args(), env);
  CHECK(r.options && r.options->obf() && r.options->passcode == "s3cr3t-passcode");
  env["ATHENA_ZOOM_JOIN_TOKEN_KIND"] = "zak";
  r = parse(launcher_args(), env);
  CHECK(r.options && r.options->zak() && !r.options->obf());
  env["ATHENA_ZOOM_JOIN_TOKEN_KIND"] = "bogus";
  r = parse(launcher_args(), env);
  CHECK(!r.options);
  // a kind without a token means no token
  env = base_env();
  env["ATHENA_ZOOM_JOIN_TOKEN_KIND"] = "obf";
  r = parse(launcher_args(), env);
  CHECK(r.options && !r.options->obf() && r.options->join_token_kind.empty());

  // required env
  env = base_env();
  env.erase("ATHENA_MEETING_JWT");
  CHECK(!parse(launcher_args(), env).options);
  env = base_env();
  env.erase("ATHENA_UPLOAD_TOKEN");
  r = parse(launcher_args(), env);
  CHECK(!r.options);
  CHECK(r.error.find("upload-placeholder") == std::string::npos);

  // bad values
  auto bad = [&](const std::string& flag, const std::string& value) {
    auto a = launcher_args();
    for (size_t i = 0; i + 1 < a.size(); ++i)
      if (a[i] == flag) a[i + 1] = value;
    return !parse(a, base_env()).options;
  };
  CHECK(bad("--meeting-id", "12345678"));       // 8 digits
  CHECK(bad("--meeting-id", "123456789012"));   // 12 digits
  CHECK(bad("--meeting-id", "8123456789a"));
  CHECK(bad("--codec", "mp3"));
  CHECK(bad("--max-seconds", "0"));
  CHECK(bad("--alone-seconds", "-5"));
  CHECK(bad("--waiting-room-seconds", "10s"));
  CHECK(bad("--upload-url", "ftp://127.0.0.1"));
  CHECK(!bad("--codec", "wav"));
  CHECK(!bad("--meeting-id", "812345678"));     // 9 digits

  auto a = launcher_args();
  a.push_back("--unknown");
  a.push_back("1");
  CHECK(!parse(a, base_env()).options);
  a = launcher_args();
  a.push_back("--sdk-log");
  r = parse(a, base_env());
  CHECK(r.options && r.options->sdk_log);
  a = launcher_args();
  a.pop_back();  // --work-dir without a value
  CHECK(!parse(a, base_env()).options);
  // every required flag
  for (const char* flag : {"--meeting-id", "--session-id", "--upload-url", "--max-seconds",
                           "--alone-seconds", "--waiting-room-seconds", "--work-dir"}) {
    auto b = launcher_args();
    for (size_t i = 0; i + 1 < b.size(); ++i)
      if (b[i] == flag) {
        b.erase(b.begin() + i, b.begin() + i + 2);
        break;
      }
    CHECK(!parse(b, base_env()).options);
  }
}

RulesConfig cfg(bool obf = false) {
  RulesConfig c;
  c.max_ms = 3600 * 1000;
  c.alone_ms = 60 * 1000;
  c.wait_ms = 600 * 1000;
  c.no_audio_ms = 60 * 1000;
  c.obf = obf;
  return c;
}

const int64_t T0 = 1'800'000'000'000;  // fictional epoch ms
int64_t s(int64_t secs) { return T0 + secs * 1000; }

void test_status_reasons() {
  CHECK(valid_reason("waiting_for_owner"));
  CHECK(valid_reason("max_duration"));
  CHECK(!valid_reason(""));
  CHECK(!valid_reason("Bad"));
  CHECK(!valid_reason("has space"));
  CHECK(!valid_reason(std::string(41, 'a')));
}

void test_happy_path() {
  Rules r(cfg(), T0);
  CHECK(r.status().state.empty());  // nothing printed while plain starting
  r.waiting_room(s(1));
  CHECK_EQ(r.status().line(), "waiting_room");
  r.joined(s(5));
  CHECK_EQ(r.status().line(), "waiting_permission");
  r.permission_denied();
  CHECK_EQ(r.status().line(), "waiting_permission denied");
  r.others(2, s(6));
  CHECK(!r.tick(s(300), 0, false, false));  // never leaves over permission
  r.recording(s(400));
  CHECK_EQ(r.status().line(), "recording");
  r.permission_denied();  // late deny after recording is ignored
  CHECK_EQ(r.status().line(), "recording");
  CHECK(!r.tick(s(430), s(429), false, false));
  CHECK_EQ(r.status().line(), "recording");
  CHECK(!r.tick(s(500), s(429), false, false));  // 71 s without frames
  CHECK_EQ(r.status().line(), "recording no_audio");
  CHECK(!r.tick(s(501), s(501), false, false));
  CHECK_EQ(r.status().line(), "recording");
  auto d = r.meeting_ended(EndKind::ByHost);
  CHECK(d.outcome == Outcome::Ended && d.reason == "meeting_ended");
  CHECK_EQ(r.status().line(), "ended meeting_ended");
  CHECK(!r.stop_signal());  // decided once
}

void test_waiting_room_timeout() {
  Rules r(cfg(), T0);
  r.waiting_room(s(2));
  CHECK(!r.tick(s(601), 0, false, false));
  auto d = r.tick(s(602), 0, false, false);
  CHECK(d.outcome == Outcome::Failed && d.reason == "waiting_room_timeout");
  CHECK_EQ(r.status().line(), "failed waiting_room_timeout");
}

void test_waiting_for_host() {
  Rules r(cfg(), T0);
  r.waiting_for_host(s(1));
  CHECK_EQ(r.status().line(), "starting waiting_for_host");
  auto d = r.tick(s(601), 0, false, false);
  CHECK(d.outcome == Outcome::Failed && d.reason == "meeting_not_started");
  Rules r2(cfg(), T0);
  d = r2.meeting_ended(EndKind::HostNeverCame);
  CHECK(d.outcome == Outcome::Failed && d.reason == "meeting_not_started");
}

void test_alone() {
  Rules r(cfg(), T0);
  r.joined(s(1));
  r.recording(s(1));
  CHECK(!r.tick(s(100), s(100), false, false));  // others not known yet: never alone
  r.others(0, s(100));
  CHECK(!r.tick(s(159), s(159), false, false));
  r.others(1, s(150));  // someone came back: timer reset
  r.others(0, s(170));
  CHECK(!r.tick(s(229), s(229), false, false));
  auto d = r.tick(s(230), s(230), false, false);
  CHECK(d.outcome == Outcome::Ended && d.reason == "alone");
}

void test_max_and_server_stop() {
  Rules r(cfg(), T0);
  r.joined(s(1));
  auto d = r.tick(s(3600), 0, false, false);
  CHECK(d.outcome == Outcome::Ended && d.reason == "max_duration");

  Rules r2(cfg(), T0);
  r2.joined(s(1));
  d = r2.tick(s(10), 0, true, false);
  CHECK(d.outcome == Outcome::Ended && d.reason == "server_stop");

  Rules r3(cfg(), T0);
  r3.joined(s(1));
  d = r3.tick(s(3601), 0, true, false);
  CHECK(d.reason == "max_duration");

  Rules r4(cfg(), T0);
  r4.joined(s(1));
  d = r4.tick(s(10), 0, true, true);  // token rejected wins: the launcher stopped it
  CHECK(d.outcome == Outcome::Ended && d.reason == "stopped");

  Rules r5(cfg(), T0);  // max also applies before joining (e.g. a long owner wait)
  r5.waiting_room(s(1));
  d = r5.tick(s(3600), 0, false, false);
  CHECK(d.reason == "max_duration");
}

void test_signal() {
  Rules r(cfg(), T0);
  r.waiting_room(s(1));
  auto d = r.stop_signal();
  CHECK(d.outcome == Outcome::Ended && d.reason == "stopped");
  CHECK(!r.tick(s(2000), 0, false, false));
}

void test_join_failures() {
  struct Case { JoinFail why; const char* reason; };
  for (Case c : {Case{JoinFail::WrongPasscode, "wrong_passcode"},
                 Case{JoinFail::NotStarted, "meeting_not_started"},
                 Case{JoinFail::NotFound, "meeting_not_found"},
                 Case{JoinFail::TokenExpired, "join_token_expired"},
                 Case{JoinFail::Other, "join_failed"},
                 Case{JoinFail::OwnerNotIn, "join_failed"}}) {  // without obf
    Rules r(cfg(), T0);
    auto d = r.join_failed(c.why, s(1));
    CHECK(d.outcome == Outcome::Failed && d.reason == c.reason);
  }
}

void test_owner_wait() {
  Rules r(cfg(true), T0);
  CHECK(!r.join_failed(JoinFail::OwnerNotIn, s(1)));
  CHECK_EQ(r.status().line(), "starting waiting_for_owner");
  CHECK(!r.join_failed(JoinFail::OwnerNotIn, s(300)));
  CHECK(!r.tick(s(600), 0, false, false));
  auto d = r.join_failed(JoinFail::OwnerNotIn, s(601));
  CHECK(d.outcome == Outcome::Failed && d.reason == "owner_not_joined");

  Rules r2(cfg(true), T0);  // the timer alone ends it too
  r2.join_failed(JoinFail::OwnerNotIn, s(1));
  d = r2.tick(s(601), 0, false, false);
  CHECK(d.reason == "owner_not_joined");

  Rules r3(cfg(true), T0);  // owner arrives: in, then Zoom drops us as he leaves
  r3.join_failed(JoinFail::OwnerNotIn, s(1));
  r3.joined(s(10));
  CHECK_EQ(r3.status().line(), "waiting_permission");
  d = r3.join_failed(JoinFail::Other, s(500));
  CHECK(d.outcome == Outcome::Ended && d.reason == "owner_left");

  Rules r4(cfg(true), T0);  // ended for a reason other than the host: owner left
  r4.joined(s(10));
  d = r4.meeting_ended(EndKind::Other);
  CHECK(d.reason == "owner_left");
  Rules r5(cfg(true), T0);
  r5.joined(s(10));
  d = r5.meeting_ended(EndKind::Removed);
  CHECK(d.reason == "removed");
  Rules r6(cfg(false), T0);  // no obf: a dropped session is a lost connection
  r6.joined(s(10));
  d = r6.join_failed(JoinFail::Other, s(500));
  CHECK(d.outcome == Outcome::Ended && d.reason == "connection_lost");
  Rules r7(cfg(false), T0);
  r7.joined(s(10));
  d = r7.meeting_ended(EndKind::Other);
  CHECK(d.reason == "meeting_ended");
}

void test_participants() {
  ParticipantTracker t;
  auto c = t.update({{16779264, "Dana Example"}, {16780288, "Lee Sample"}});
  CHECK_EQ(c.size(), 2u);
  CHECK(c[0].joined && c[0].track_id == "16779264" && c[0].name == "Dana Example");
  CHECK(t.update({{16779264, "Dana Example"}, {16780288, "Lee Sample"}}).empty());
  c = t.update({{16779264, "Dana E. (she/her)"}});  // rename + Lee left
  CHECK_EQ(c.size(), 2u);
  CHECK(c[0].joined && c[0].name == "Dana E. (she/her)");
  CHECK(!c[1].joined && c[1].track_id == "16780288" && c[1].name == "Lee Sample");
  CHECK_EQ(t.name_of(16780288), "Lee Sample");  // remembered after leaving
  c = t.update({{16779264, "Dana E. (she/her)"}, {16780288, "Lee Sample"}});  // rejoin
  CHECK(c.size() == 1 && c[0].joined && c[0].track_id == "16780288");
  CHECK_EQ(t.count(), 2u);
  CHECK(t.name_of(1).empty());
}

}  // namespace

int main() {
  test_options();
  test_status_reasons();
  test_happy_path();
  test_waiting_room_timeout();
  test_waiting_for_host();
  test_alone();
  test_max_and_server_stop();
  test_signal();
  test_join_failures();
  test_owner_wait();
  test_participants();
  if (g_failures) {
    std::fprintf(stderr, "%d check(s) failed\n", g_failures);
    return 1;
  }
  std::puts("bot logic: all checks passed");
  return 0;
}
