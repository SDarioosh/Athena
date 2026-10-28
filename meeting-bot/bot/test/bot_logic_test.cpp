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

// The host takes the recording permission away and gives it back later.
void test_permission_revoked() {
  Rules r(cfg(), T0);
  r.joined(s(1));
  r.others(1, s(1));
  r.recording(s(2));
  CHECK_EQ(r.status().line(), "recording");
  r.recording_stopped();
  CHECK(!r.is_recording());
  CHECK_EQ(r.status().line(), "waiting_permission");
  CHECK(!r.tick(s(200), s(10), false, false));  // no no_audio while not recording
  CHECK_EQ(r.status().line(), "waiting_permission");
  r.recording(s(201));
  CHECK_EQ(r.status().line(), "recording");
  CHECK(!r.tick(s(202), s(202), false, false));
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
                 Case{JoinFail::ExternalNotAllowed, "external_meeting_not_allowed"},
                 Case{JoinFail::OutsideUsersBlocked, "outside_users_blocked"},
                 Case{JoinFail::BlockedByAdmin, "blocked_by_account_admin"},
                 Case{JoinFail::SignInRequired, "zoom_signin_required"},
                 Case{JoinFail::Other, "join_failed"},
                 Case{JoinFail::OwnerNotIn, "join_failed"}}) {  // without obf
    Rules r(cfg(), T0);
    auto d = r.join_failed(c.why, s(1));
    CHECK(d.outcome == Outcome::Failed && d.reason == c.reason);
  }
  Rules r(cfg(true), T0);  // obf: Zoom refusing the app is no reason to wait for the owner
  auto d = r.join_failed(JoinFail::ExternalNotAllowed, s(1));
  CHECK(d.outcome == Outcome::Failed && d.reason == "external_meeting_not_allowed");
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
  Rules r6(cfg(false), T0);  // no obf: a dropped session is a drop: rejoin
  r6.joined(s(10));
  CHECK(!r6.join_failed(JoinFail::Other, s(500)));
  CHECK_EQ(r6.status().line(), "starting reconnecting");
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

// Network drop, the SDK gives up, the bot rejoins: same Rules, recording again.
void test_drop_rejoin() {
  Rules r(cfg(), T0);
  r.joined(s(1));
  r.recording(s(2));
  r.others(2, s(2));
  r.reconnecting(s(100));
  CHECK_EQ(r.status().line(), "starting reconnecting");
  CHECK(!r.is_recording());
  CHECK(!r.rejoin_due(s(110)));  // the SDK reconnects on its own: no Join from us
  CHECK(!r.tick(s(110), s(99), false, false));
  CHECK(!r.join_failed(JoinFail::Other, s(130)));  // the SDK gave up
  CHECK(r.rejoin_due(s(130)));  // 30 s after the drop: past the first try's 2 s
  r.rejoin_started(s(130));
  CHECK_EQ(r.rejoin_tries(), 1);
  CHECK(!r.rejoin_due(s(131)));  // one try at a time
  r.joined(s(133));
  CHECK_EQ(r.status().line(), "waiting_permission");  // recording must start again
  r.recording(s(134));
  CHECK_EQ(r.status().line(), "recording");
  CHECK(!r.tick(s(150), s(150), false, false));
  CHECK(r.ever_recorded());
  // the alone timer starts over after the rejoin: others unknown until the next poll
  CHECK(!r.tick(s(300), s(300), false, false));
  // a second drop gets a new budget
  r.reconnecting(s(1000));
  CHECK(!r.join_failed(JoinFail::Other, s(1001)));
  CHECK(!r.rejoin_due(s(1001)));
  CHECK(r.rejoin_due(s(1002)));
}

void test_drop_lost() {
  Rules r(cfg(), T0);
  r.joined(s(1));
  r.recording(s(1));
  CHECK(!r.join_failed(JoinFail::Other, s(10)));  // dropped, no SDK reconnect
  CHECK(!r.rejoin_due(s(11)));
  CHECK(r.rejoin_due(s(12)));     // try 1 at +2 s
  r.rejoin_started(s(12));
  CHECK(!r.join_failed(JoinFail::Other, s(13)));
  CHECK(!r.rejoin_due(s(29)));
  CHECK(r.rejoin_due(s(30)));     // try 2 at +20 s
  r.rejoin_started(s(30));
  CHECK(!r.join_failed(JoinFail::Other, s(31)));
  CHECK(!r.rejoin_due(s(69)));
  CHECK(r.rejoin_due(s(70)));     // try 3 at +60 s
  r.rejoin_started(s(70));
  auto d = r.join_failed(JoinFail::Other, s(72));
  CHECK(d.outcome == Outcome::Ended && d.reason == "connection_lost");

  Rules r2(cfg(), T0);  // a try that never answers: 2 minutes after the drop
  r2.joined(s(1));
  r2.join_failed(JoinFail::Other, s(10));
  r2.rejoin_started(s(12));
  CHECK(!r2.tick(s(129), 0, false, false));
  d = r2.tick(s(130), 0, false, false);
  CHECK(d.outcome == Outcome::Ended && d.reason == "connection_lost");

  Rules r3(cfg(), T0);  // the SDK reconnects for over 2 minutes
  r3.joined(s(1));
  r3.reconnecting(s(10));
  d = r3.tick(s(130), 0, false, false);
  CHECK(d.reason == "connection_lost");

  Rules r4(cfg(), T0);  // a rejoin finds the meeting gone
  r4.joined(s(1));
  r4.join_failed(JoinFail::Other, s(10));
  r4.rejoin_started(s(12));
  d = r4.join_failed(JoinFail::NotFound, s(13));
  CHECK(d.outcome == Outcome::Ended && d.reason == "meeting_ended");

  Rules r5(cfg(), T0);  // a passcode that no longer works: no point in trying again
  r5.joined(s(1));
  r5.join_failed(JoinFail::Other, s(10));
  r5.rejoin_started(s(12));
  d = r5.join_failed(JoinFail::WrongPasscode, s(13));
  CHECK(d.reason == "connection_lost");

  Rules r6(cfg(), T0);  // the host ends the meeting while the bot reconnects
  r6.joined(s(1));
  r6.reconnecting(s(10));
  d = r6.meeting_ended(EndKind::ByHost);
  CHECK(d.reason == "meeting_ended");

  Rules r7(cfg(), T0);  // Zoom closes the session (no host reason) while reconnecting
  r7.joined(s(1));
  r7.reconnecting(s(10));
  CHECK(!r7.meeting_ended(EndKind::Other));
  CHECK(r7.rejoin_due(s(12)));
  r7.rejoin_started(s(12));
  CHECK(!r7.meeting_ended(EndKind::Other));  // a try ended that way counts as failed
  CHECK(!r7.rejoin_due(s(29)));
  CHECK(r7.rejoin_due(s(30)));

  Rules r8(cfg(), T0);  // SIGTERM while waiting to rejoin
  r8.joined(s(1));
  r8.join_failed(JoinFail::Other, s(10));
  d = r8.stop_signal();
  CHECK(d.reason == "stopped");
  CHECK(!r8.rejoin_due(s(100)));

  Rules r9(cfg(), T0);  // max duration also while reconnecting
  r9.joined(s(1));
  r9.reconnecting(s(3590));
  d = r9.tick(s(3600), 0, false, false);
  CHECK(d.reason == "max_duration");

  Rules r10(cfg(), T0);  // a rejoin try lands in the waiting room: connected again
  r10.joined(s(1));
  r10.join_failed(JoinFail::Other, s(10));
  r10.rejoin_started(s(12));
  r10.waiting_room(s(14));
  CHECK_EQ(r10.status().line(), "waiting_room");
  CHECK(!r10.tick(s(200), 0, false, false));  // no 2-minute give-up there
  d = r10.tick(s(614), 0, false, false);
  CHECK(d.outcome == Outcome::Ended && d.reason == "waiting_room_timeout");
}

void test_obf_drop() {
  Rules r(cfg(true), T0);  // obf, network drop, owner still in: rejoin
  r.joined(s(1));
  r.reconnecting(s(10));
  CHECK(!r.join_failed(JoinFail::Other, s(20)));
  r.rejoin_started(s(20));
  r.joined(s(22));
  CHECK_EQ(r.status().line(), "waiting_permission");

  Rules r2(cfg(true), T0);  // obf, network drop, the owner left meanwhile
  r2.joined(s(1));
  r2.reconnecting(s(10));
  r2.join_failed(JoinFail::Other, s(20));
  r2.rejoin_started(s(20));
  auto d = r2.join_failed(JoinFail::OwnerNotIn, s(21));
  CHECK(d.outcome == Outcome::Ended && d.reason == "owner_left");

  Rules r3(cfg(true), T0);  // obf, Zoom ends the session with no reconnect: owner left
  r3.joined(s(1));
  d = r3.join_failed(JoinFail::Other, s(20));
  CHECK(d.reason == "owner_left");
}

void test_waiting_room_after_joining() {
  Rules r(cfg(), T0);
  r.joined(s(1));
  r.recording(s(1));
  r.others(1, s(1));
  r.waiting_room(s(100));  // the host moved Athena back to the waiting room
  CHECK_EQ(r.status().line(), "waiting_room");
  CHECK(!r.tick(s(699), 0, false, false));
  auto d = r.tick(s(700), 0, false, false);
  CHECK(d.outcome == Outcome::Ended && d.reason == "waiting_room_timeout");

  Rules r2(cfg(), T0);  // let in again: same Rules, recording again
  r2.joined(s(1));
  r2.recording(s(1));
  r2.waiting_room(s(100));
  r2.joined(s(200));
  CHECK_EQ(r2.status().line(), "waiting_permission");
  r2.recording(s(201));
  CHECK_EQ(r2.status().line(), "recording");
  CHECK(!r2.tick(s(250), s(250), false, false));

  Rules r3(cfg(), T0);  // never in the meeting: still a failure
  r3.waiting_room(s(1));
  d = r3.tick(s(601), 0, false, false);
  CHECK(d.outcome == Outcome::Failed && d.reason == "waiting_room_timeout");
}

void test_breakouts() {
  Rules r(cfg(), T0);
  r.joined(s(1));
  r.recording(s(1));
  CHECK(r.others(4, s(2), false) == Breakouts::None);
  CHECK(r.others(4, s(3), true) == Breakouts::None);  // open, nobody moved yet
  CHECK(r.others(2, s(4), true) == Breakouts::None);  // half left: not most
  CHECK(r.others(0, s(5), true) == Breakouts::MostLeft);
  CHECK_EQ(r.breakouts_since(), s(3));
  CHECK(r.others(0, s(6), true) == Breakouts::None);  // noted once
  CHECK(!r.tick(s(600), s(600), false, false));  // alone, but rooms are open: stays
  CHECK(r.others(0, s(900), false) == Breakouts::Over);
  CHECK(!r.tick(s(959), s(959), false, false));  // alone counts from the close
  CHECK(r.others(0, s(960), false) == Breakouts::None);
  auto d = r.tick(s(1020), s(1020), false, false);
  CHECK(d.reason == "alone");

  Rules r2(cfg(), T0);  // rooms open, most stay: nothing noted, nothing at the close
  r2.joined(s(1));
  r2.others(3, s(2));
  CHECK(r2.others(2, s(3), true) == Breakouts::None);
  CHECK(r2.others(3, s(4), false) == Breakouts::None);

  Rules r3(cfg(), T0);  // already alone when rooms open (before=1): one left of one
  r3.joined(s(1));
  r3.others(1, s(2));
  CHECK(r3.others(0, s(3), true) == Breakouts::MostLeft);
}

}  // namespace

int main() {
  test_options();
  test_status_reasons();
  test_happy_path();
  test_permission_revoked();
  test_waiting_room_timeout();
  test_waiting_for_host();
  test_alone();
  test_max_and_server_stop();
  test_signal();
  test_join_failures();
  test_owner_wait();
  test_participants();
  test_drop_rejoin();
  test_drop_lost();
  test_obf_drop();
  test_waiting_room_after_joining();
  test_breakouts();
  if (g_failures) {
    std::fprintf(stderr, "%d check(s) failed\n", g_failures);
    return 1;
  }
  std::puts("bot logic: all checks passed");
  return 0;
}
