#include "options.h"

#include <cctype>
#include <climits>
#include <cstdlib>

namespace athena::bot {

namespace {

bool all_digits(const std::string& s) {
  if (s.empty()) return false;
  for (unsigned char c : s)
    if (!std::isdigit(c)) return false;
  return true;
}

// Strict positive int: digits only, no sign, no trailing junk, fits in int.
bool positive_int(const std::string& s, int* out) {
  if (!all_digits(s) || s.size() > 9) return false;
  long v = std::strtol(s.c_str(), nullptr, 10);
  if (v <= 0 || v > INT_MAX) return false;
  *out = static_cast<int>(v);
  return true;
}

std::string env(const GetEnv& getenv, const char* name) {
  const char* v = getenv(name);
  return v ? v : "";
}

ParseResult fail(std::string why) { return ParseResult{std::nullopt, std::move(why)}; }

}  // namespace

const char* usage() {
  return "usage: athena-meeting-bot --meeting-id <9-11 digits> --session-id <id>\n"
         "         --upload-url <http://127.0.0.1:port> --name <name> --max-seconds <s>\n"
         "         --alone-seconds <s> --waiting-room-seconds <s> --codec opus|wav\n"
         "         --work-dir <dir> [--sdk-log]\n"
         "env: ATHENA_MEETING_JWT, ATHENA_UPLOAD_TOKEN (required);\n"
         "     ATHENA_MEETING_PASSCODE, ATHENA_ZOOM_JOIN_TOKEN + ATHENA_ZOOM_JOIN_TOKEN_KIND\n"
         "     (zak|obf) (optional)\n";
}

ParseResult parse_options(const std::vector<std::string>& args, const GetEnv& getenv) {
  Options o;
  bool seen_meeting = false, seen_session = false, seen_url = false, seen_max = false,
       seen_alone = false, seen_wait = false, seen_work = false;
  for (size_t i = 0; i < args.size(); ++i) {
    const std::string& a = args[i];
    if (a == "--sdk-log") {
      o.sdk_log = true;
      continue;
    }
    if (a.rfind("--", 0) != 0) return fail("unexpected argument");
    if (i + 1 >= args.size()) return fail(a + " needs a value");
    const std::string& v = args[++i];
    if (a == "--meeting-id") {
      o.meeting_id = v;
      seen_meeting = true;
    } else if (a == "--session-id") {
      o.session_id = v;
      seen_session = true;
    } else if (a == "--upload-url") {
      o.upload_url = v;
      seen_url = true;
    } else if (a == "--name") {
      o.name = v;
    } else if (a == "--max-seconds") {
      if (!positive_int(v, &o.max_seconds)) return fail("--max-seconds must be a positive integer");
      seen_max = true;
    } else if (a == "--alone-seconds") {
      if (!positive_int(v, &o.alone_seconds))
        return fail("--alone-seconds must be a positive integer");
      seen_alone = true;
    } else if (a == "--waiting-room-seconds") {
      if (!positive_int(v, &o.waiting_room_seconds))
        return fail("--waiting-room-seconds must be a positive integer");
      seen_wait = true;
    } else if (a == "--codec") {
      o.codec = v;
    } else if (a == "--work-dir") {
      o.work_dir = v;
      seen_work = true;
    } else {
      return fail("unknown option " + a);
    }
  }
  if (!seen_meeting || !all_digits(o.meeting_id) || o.meeting_id.size() < 9 ||
      o.meeting_id.size() > 11)
    return fail("--meeting-id must be 9 to 11 digits");
  if (!seen_session || o.session_id.empty()) return fail("--session-id is required");
  if (!seen_url || (o.upload_url.rfind("http://", 0) != 0 && o.upload_url.rfind("https://", 0) != 0))
    return fail("--upload-url must start with http:// or https://");
  if (o.name.empty()) return fail("--name must not be empty");
  if (!seen_max) return fail("--max-seconds is required");
  if (!seen_alone) return fail("--alone-seconds is required");
  if (!seen_wait) return fail("--waiting-room-seconds is required");
  if (o.codec != "opus" && o.codec != "wav") return fail("--codec must be opus or wav");
  if (!seen_work || o.work_dir.empty()) return fail("--work-dir is required");

  o.jwt = env(getenv, "ATHENA_MEETING_JWT");
  o.upload_token = env(getenv, "ATHENA_UPLOAD_TOKEN");
  o.passcode = env(getenv, "ATHENA_MEETING_PASSCODE");
  o.join_token = env(getenv, "ATHENA_ZOOM_JOIN_TOKEN");
  o.join_token_kind = env(getenv, "ATHENA_ZOOM_JOIN_TOKEN_KIND");
  if (o.jwt.empty()) return fail("ATHENA_MEETING_JWT is not set");
  if (o.upload_token.empty()) return fail("ATHENA_UPLOAD_TOKEN is not set");
  if (o.join_token.empty()) {
    o.join_token_kind.clear();  // a kind without a token means no token
  } else if (o.join_token_kind.empty()) {
    o.join_token_kind = "obf";  // the launcher always sets it; obf is its default mode
  } else if (o.join_token_kind != "zak" && o.join_token_kind != "obf") {
    return fail("ATHENA_ZOOM_JOIN_TOKEN_KIND must be zak or obf");
  }
  return ParseResult{std::move(o), ""};
}

}  // namespace athena::bot
