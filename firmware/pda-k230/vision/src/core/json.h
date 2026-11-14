// A tiny JSON reader and writer for the line protocols (vision.sock and keypoint tracks).
// No dependency. The reader takes one complete JSON text, limits nesting depth and input
// size, and never throws: malformed input just fails.
#pragma once

#include <cstddef>
#include <map>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

namespace av::json {

struct Value {
    enum class Type { Null, Bool, Number, String, Array, Object };
    Type type = Type::Null;
    bool b = false;
    double num = 0.0;
    std::string str;
    std::vector<Value> arr;
    std::map<std::string, Value> obj;  // a repeated key keeps the last value

    bool is_object() const { return type == Type::Object; }
    // Object member, or nullptr if this is no object or the key is missing.
    const Value *get(const std::string &key) const;
    // Typed reads of an object member; false if missing or of another type.
    bool get_bool(const std::string &key, bool *out) const;
    bool get_number(const std::string &key, double *out) const;
    bool get_string(const std::string &key, std::string *out) const;
};

struct ParseLimits {
    size_t max_bytes = 64 * 1024;
    int max_depth = 8;
};

// Parses exactly one JSON value (surrounding whitespace allowed). On failure returns false
// and, if err is given, a short reason.
bool parse(std::string_view text, Value *out, std::string *err = nullptr,
           const ParseLimits &limits = ParseLimits());

// Builds one JSON object; numbers are written locale-independently.
class ObjectWriter {
public:
    ObjectWriter &str(const char *key, std::string_view value);
    ObjectWriter &num(const char *key, double value, int max_decimals = 3);
    ObjectWriter &boolean(const char *key, bool value);
    ObjectWriter &null(const char *key);
    // A raw, already encoded JSON value (array, nested object).
    ObjectWriter &raw(const char *key, std::string_view json_value);
    std::string done() const { return buf_ + "}"; }

private:
    void key(const char *k);
    std::string buf_ = "{";
    bool first_ = true;
};

// Appends value as a JSON string literal (quotes included). Control characters, quotes and
// backslashes are escaped; invalid UTF-8 bytes become U+FFFD.
void append_string(std::string &out, std::string_view value);
// Appends a number: finite values with up to max_decimals decimals, trailing zeros
// dropped; NaN and infinities as 0.
void append_number(std::string &out, double value, int max_decimals = 3);

}  // namespace av::json
