// Small helpers shared by the core: JSON strings, UTF-8, times, track ids.
#pragma once

#include <cstdint>
#include <string>

namespace athena::util {

// A JSON string literal (with quotes). Invalid UTF-8 becomes U+FFFD, so the server's JSON
// parser never rejects a whole call over one bad byte from the SDK.
std::string json_str(const std::string& s);

// `s` with every byte that is not part of valid UTF-8 replaced by U+FFFD (as json_str).
std::string utf8_fix(const std::string& s);

// At most `max_chars` Unicode code points (the server counts Python characters).
std::string utf8_truncate(const std::string& s, size_t max_chars);

// Control characters and line breaks as spaces, runs of spaces as one, trimmed.
std::string clean_line(const std::string& s);

// "2026-10-13T15:00:21.250Z"
std::string iso_utc_ms(int64_t epoch_ms);
// "1760367621.250": the chunk `timestamp` field (epoch seconds, fractional)
std::string epoch_seconds(int64_t epoch_ms);

// The server accepts times in 2000..2100 only.
bool plausible_epoch_ms(int64_t epoch_ms);
int64_t wall_now_ms();

// A chunk `track` / chat `zoom_user_id` the server accepts: [A-Za-z0-9._:-]{1,64}.
bool valid_track_id(const std::string& id);
// Other characters become '_', cut at 64; empty becomes "unknown".
std::string sanitize_track_id(const std::string& id);

// Percent-encodes everything but RFC 3986 unreserved characters (a URL path segment).
std::string url_escape(const std::string& s);

}  // namespace athena::util
