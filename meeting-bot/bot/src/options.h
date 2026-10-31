// Command line and environment of athena-meeting-bot, exactly as the launcher builds them
// (server/athena/meetings/bot/launcher.py docstring, docs/api/meetings.md "Bot contract"). No Zoom SDK
// here, so the host tests cover it.
#pragma once

#include <functional>
#include <optional>
#include <string>
#include <vector>

namespace athena::bot {

struct Options {
  std::string meeting_id;       // 9-11 digits
  std::string session_id;
  std::string upload_url;       // http(s)://host:port, no trailing slash needed
  std::string name = "Athena";
  int max_seconds = 0;          // > 0
  int alone_seconds = 0;        // > 0
  int waiting_room_seconds = 0; // > 0; also the owner wait (obf) and the wait for the host
  std::string codec = "opus";   // opus | wav
  std::string work_dir;
  bool sdk_log = false;         // --sdk-log: the SDK's own log in ~/.zoomsdk/logs (manual runs)

  // Environment. Secrets: never printed, never in argv.
  std::string jwt;              // ATHENA_MEETING_JWT (required)
  std::string upload_token;     // ATHENA_UPLOAD_TOKEN (required)
  std::string passcode;         // ATHENA_MEETING_PASSCODE (may be absent)
  std::string join_token;       // ATHENA_ZOOM_JOIN_TOKEN (may be absent)
  std::string join_token_kind;  // ATHENA_ZOOM_JOIN_TOKEN_KIND: "zak" | "obf" | "" (no token)

  bool obf() const { return !join_token.empty() && join_token_kind == "obf"; }
  bool zak() const { return !join_token.empty() && join_token_kind == "zak"; }
};

struct ParseResult {
  std::optional<Options> options;
  std::string error;  // why parsing failed; never contains a secret's value
};

using GetEnv = std::function<const char*(const char*)>;

// args = argv[1..]; getenv is injectable for tests.
ParseResult parse_options(const std::vector<std::string>& args, const GetEnv& getenv);

const char* usage();

}  // namespace athena::bot
