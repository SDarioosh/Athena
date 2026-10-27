#include "util.h"

#include <chrono>
#include <cstdio>
#include <ctime>

namespace athena::util {

namespace {

// Length of the valid UTF-8 sequence starting at s[i], or 0 if it is invalid.
size_t utf8_seq_len(const std::string& s, size_t i) {
  const auto b = [&](size_t k) { return static_cast<unsigned char>(s[k]); };
  const unsigned char c = b(i);
  if (c < 0x80) return 1;
  size_t n;
  uint32_t cp;
  if ((c & 0xE0) == 0xC0) {
    n = 2;
    cp = c & 0x1F;
  } else if ((c & 0xF0) == 0xE0) {
    n = 3;
    cp = c & 0x0F;
  } else if ((c & 0xF8) == 0xF0) {
    n = 4;
    cp = c & 0x07;
  } else {
    return 0;
  }
  if (i + n > s.size()) return 0;
  for (size_t k = 1; k < n; ++k) {
    if ((b(i + k) & 0xC0) != 0x80) return 0;
    cp = (cp << 6) | (b(i + k) & 0x3F);
  }
  // overlong forms, surrogates, beyond U+10FFFF
  if ((n == 2 && cp < 0x80) || (n == 3 && cp < 0x800) || (n == 4 && cp < 0x10000) ||
      (cp >= 0xD800 && cp <= 0xDFFF) || cp > 0x10FFFF)
    return 0;
  return n;
}

}  // namespace

std::string json_str(const std::string& s) {
  std::string o;
  o.reserve(s.size() + 2);
  o += '"';
  for (size_t i = 0; i < s.size();) {
    const size_t n = utf8_seq_len(s, i);
    if (n == 0) {
      o += "\xEF\xBF\xBD";  // U+FFFD
      ++i;
      continue;
    }
    if (n > 1) {
      o.append(s, i, n);
      i += n;
      continue;
    }
    const char c = s[i++];
    switch (c) {
      case '"': o += "\\\""; break;
      case '\\': o += "\\\\"; break;
      case '\n': o += "\\n"; break;
      case '\r': o += "\\r"; break;
      case '\t': o += "\\t"; break;
      default:
        if (static_cast<unsigned char>(c) < 0x20 || c == 0x7F) {
          char b[8];
          std::snprintf(b, sizeof b, "\\u%04x", static_cast<unsigned char>(c));
          o += b;
        } else {
          o += c;
        }
    }
  }
  o += '"';
  return o;
}

std::string utf8_truncate(const std::string& s, size_t max_chars) {
  size_t i = 0, chars = 0;
  while (i < s.size() && chars < max_chars) {
    const size_t n = utf8_seq_len(s, i);
    i += n ? n : 1;  // an invalid byte counts as one character (json_str replaces it)
    ++chars;
  }
  return s.substr(0, i);
}

std::string clean_line(const std::string& s) {
  std::string o;
  o.reserve(s.size());
  bool space = false;
  for (const char c : s) {
    const auto u = static_cast<unsigned char>(c);
    if (u < 0x20 || u == 0x7F || c == ' ') {
      space = !o.empty();
      continue;
    }
    if (space) o += ' ';
    space = false;
    o += c;
  }
  return o;
}

std::string iso_utc_ms(int64_t epoch_ms) {
  int64_t secs = epoch_ms / 1000;
  int ms = static_cast<int>(epoch_ms % 1000);
  if (ms < 0) {
    ms += 1000;
    secs -= 1;
  }
  const time_t t = static_cast<time_t>(secs);
  struct tm tm {};
  gmtime_r(&t, &tm);
  char b[64];
  std::snprintf(b, sizeof b, "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ", tm.tm_year + 1900,
                tm.tm_mon + 1, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec, ms);
  return b;
}

std::string epoch_seconds(int64_t epoch_ms) {
  char b[32];
  std::snprintf(b, sizeof b, "%lld.%03lld", static_cast<long long>(epoch_ms / 1000),
                static_cast<long long>(epoch_ms % 1000));
  return b;
}

bool plausible_epoch_ms(int64_t epoch_ms) {
  return epoch_ms >= 946684800000LL && epoch_ms < 4102444800000LL;
}

int64_t wall_now_ms() {
  using namespace std::chrono;
  return duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

static bool track_char(char c) {
  return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
         c == '.' || c == '_' || c == ':' || c == '-';
}

bool valid_track_id(const std::string& id) {
  if (id.empty() || id.size() > 64) return false;
  for (const char c : id)
    if (!track_char(c)) return false;
  return true;
}

std::string sanitize_track_id(const std::string& id) {
  std::string o;
  for (const char c : id) {
    if (o.size() == 64) break;
    o += track_char(c) ? c : '_';
  }
  return o.empty() ? "unknown" : o;
}

std::string url_escape(const std::string& s) {
  static const char* hex = "0123456789ABCDEF";
  std::string o;
  for (const char c : s) {
    const auto u = static_cast<unsigned char>(c);
    if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
        c == '-' || c == '.' || c == '_' || c == '~') {
      o += c;
    } else {
      o += '%';
      o += hex[u >> 4];
      o += hex[u & 15];
    }
  }
  return o;
}

}  // namespace athena::util
