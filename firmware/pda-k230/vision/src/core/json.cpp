#include "json.h"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace av::json {

const Value *Value::get(const std::string &key) const {
    if (type != Type::Object) return nullptr;
    auto it = obj.find(key);
    return it == obj.end() ? nullptr : &it->second;
}

bool Value::get_bool(const std::string &key, bool *out) const {
    const Value *v = get(key);
    if (!v || v->type != Type::Bool) return false;
    *out = v->b;
    return true;
}

bool Value::get_number(const std::string &key, double *out) const {
    const Value *v = get(key);
    if (!v || v->type != Type::Number) return false;
    *out = v->num;
    return true;
}

bool Value::get_string(const std::string &key, std::string *out) const {
    const Value *v = get(key);
    if (!v || v->type != Type::String) return false;
    *out = v->str;
    return true;
}

namespace {

void put_utf8(std::string &out, unsigned cp) {
    if (cp < 0x80) {
        out += static_cast<char>(cp);
    } else if (cp < 0x800) {
        out += static_cast<char>(0xC0 | (cp >> 6));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    } else if (cp < 0x10000) {
        out += static_cast<char>(0xE0 | (cp >> 12));
        out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    } else {
        out += static_cast<char>(0xF0 | (cp >> 18));
        out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
        out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    }
}

class Parser {
public:
    Parser(std::string_view s, const ParseLimits &lim) : s_(s), lim_(lim) {}

    bool run(Value *out, std::string *err) {
        ws();
        if (!value(out, 0)) return fail(err);
        ws();
        if (i_ != s_.size()) {
            why_ = "trailing data";
            return fail(err);
        }
        return true;
    }

private:
    bool fail(std::string *err) {
        if (err) *err = why_.empty() ? "invalid JSON" : why_;
        return false;
    }
    bool eof() const { return i_ >= s_.size(); }
    char peek() const { return eof() ? '\0' : s_[i_]; }
    void ws() {
        while (!eof() && (s_[i_] == ' ' || s_[i_] == '\t' || s_[i_] == '\n' || s_[i_] == '\r')) {
            i_++;
        }
    }
    bool lit(const char *word) {
        size_t n = std::strlen(word);
        if (s_.substr(i_, n) != word) return false;
        i_ += n;
        return true;
    }

    bool value(Value *v, int depth) {
        if (depth > lim_.max_depth) {
            why_ = "too deep";
            return false;
        }
        char c = peek();
        if (c == '{') return object(v, depth);
        if (c == '[') return array(v, depth);
        if (c == '"') {
            v->type = Value::Type::String;
            return string(&v->str);
        }
        if (c == 't' && lit("true")) {
            v->type = Value::Type::Bool;
            v->b = true;
            return true;
        }
        if (c == 'f' && lit("false")) {
            v->type = Value::Type::Bool;
            v->b = false;
            return true;
        }
        if (c == 'n' && lit("null")) {
            v->type = Value::Type::Null;
            return true;
        }
        if (c == '-' || (c >= '0' && c <= '9')) return number(v);
        why_ = "unexpected character";
        return false;
    }

    bool number(Value *v) {
        size_t start = i_;
        if (peek() == '-') i_++;
        if (peek() == '0') {
            i_++;
        } else if (peek() >= '1' && peek() <= '9') {
            while (peek() >= '0' && peek() <= '9') i_++;
        } else {
            why_ = "bad number";
            return false;
        }
        if (peek() == '.') {
            i_++;
            if (!(peek() >= '0' && peek() <= '9')) {
                why_ = "bad number";
                return false;
            }
            while (peek() >= '0' && peek() <= '9') i_++;
        }
        if (peek() == 'e' || peek() == 'E') {
            i_++;
            if (peek() == '+' || peek() == '-') i_++;
            if (!(peek() >= '0' && peek() <= '9')) {
                why_ = "bad number";
                return false;
            }
            while (peek() >= '0' && peek() <= '9') i_++;
        }
        // The grammar is checked above; strtod converts (the program never calls
        // setlocale, so the decimal mark is '.'). Copy first: s_ isn't NUL-terminated.
        char buf[64];
        size_t n = i_ - start;
        if (n >= sizeof(buf)) {
            why_ = "number too long";
            return false;
        }
        std::memcpy(buf, s_.data() + start, n);
        buf[n] = '\0';
        char *end = nullptr;
        double d = std::strtod(buf, &end);
        if (end != buf + n || !std::isfinite(d)) {
            why_ = "bad number";
            return false;
        }
        v->type = Value::Type::Number;
        v->num = d;
        return true;
    }

    bool hex4(unsigned *out) {
        if (i_ + 4 > s_.size()) return false;
        unsigned v = 0;
        for (int k = 0; k < 4; k++) {
            char c = s_[i_++];
            v <<= 4;
            if (c >= '0' && c <= '9') v |= static_cast<unsigned>(c - '0');
            else if (c >= 'a' && c <= 'f') v |= static_cast<unsigned>(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') v |= static_cast<unsigned>(c - 'A' + 10);
            else return false;
        }
        *out = v;
        return true;
    }

    bool string(std::string *out) {
        i_++;  // opening quote
        out->clear();
        while (true) {
            if (eof()) {
                why_ = "unterminated string";
                return false;
            }
            unsigned char c = static_cast<unsigned char>(s_[i_++]);
            if (c == '"') return true;
            if (c < 0x20) {
                why_ = "control character in string";
                return false;
            }
            if (c != '\\') {
                *out += static_cast<char>(c);
                continue;
            }
            if (eof()) {
                why_ = "unterminated string";
                return false;
            }
            char e = s_[i_++];
            switch (e) {
            case '"': *out += '"'; break;
            case '\\': *out += '\\'; break;
            case '/': *out += '/'; break;
            case 'b': *out += '\b'; break;
            case 'f': *out += '\f'; break;
            case 'n': *out += '\n'; break;
            case 'r': *out += '\r'; break;
            case 't': *out += '\t'; break;
            case 'u': {
                unsigned cp;
                if (!hex4(&cp)) {
                    why_ = "bad \\u escape";
                    return false;
                }
                if (cp >= 0xD800 && cp < 0xDC00) {
                    unsigned lo;
                    if (s_.substr(i_, 2) == "\\u") {
                        i_ += 2;
                        if (!hex4(&lo) || lo < 0xDC00 || lo >= 0xE000) {
                            why_ = "bad surrogate pair";
                            return false;
                        }
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                    } else {
                        cp = 0xFFFD;
                    }
                } else if (cp >= 0xDC00 && cp < 0xE000) {
                    cp = 0xFFFD;
                }
                put_utf8(*out, cp);
                break;
            }
            default:
                why_ = "bad escape";
                return false;
            }
        }
    }

    bool array(Value *v, int depth) {
        i_++;
        v->type = Value::Type::Array;
        ws();
        if (peek() == ']') {
            i_++;
            return true;
        }
        while (true) {
            ws();
            Value item;
            if (!value(&item, depth + 1)) return false;
            v->arr.push_back(std::move(item));
            ws();
            char c = peek();
            if (c == ',') {
                i_++;
                continue;
            }
            if (c == ']') {
                i_++;
                return true;
            }
            why_ = "expected , or ]";
            return false;
        }
    }

    bool object(Value *v, int depth) {
        i_++;
        v->type = Value::Type::Object;
        ws();
        if (peek() == '}') {
            i_++;
            return true;
        }
        while (true) {
            ws();
            if (peek() != '"') {
                why_ = "expected key";
                return false;
            }
            std::string key;
            if (!string(&key)) return false;
            ws();
            if (peek() != ':') {
                why_ = "expected :";
                return false;
            }
            i_++;
            ws();
            Value item;
            if (!value(&item, depth + 1)) return false;
            v->obj[key] = std::move(item);
            ws();
            char c = peek();
            if (c == ',') {
                i_++;
                continue;
            }
            if (c == '}') {
                i_++;
                return true;
            }
            why_ = "expected , or }";
            return false;
        }
    }

    std::string_view s_;
    ParseLimits lim_;
    size_t i_ = 0;
    std::string why_;
};

// Length of the valid UTF-8 sequence at s[i], or 0 if invalid.
size_t utf8_len(std::string_view s, size_t i) {
    unsigned char c = static_cast<unsigned char>(s[i]);
    size_t n;
    unsigned cp;
    if (c < 0x80) return 1;
    if ((c & 0xE0) == 0xC0) { n = 2; cp = c & 0x1F; }
    else if ((c & 0xF0) == 0xE0) { n = 3; cp = c & 0x0F; }
    else if ((c & 0xF8) == 0xF0) { n = 4; cp = c & 0x07; }
    else return 0;
    if (i + n > s.size()) return 0;
    for (size_t k = 1; k < n; k++) {
        unsigned char cc = static_cast<unsigned char>(s[i + k]);
        if ((cc & 0xC0) != 0x80) return 0;
        cp = (cp << 6) | (cc & 0x3F);
    }
    if ((n == 2 && cp < 0x80) || (n == 3 && cp < 0x800) || (n == 4 && cp < 0x10000) ||
        cp > 0x10FFFF || (cp >= 0xD800 && cp < 0xE000)) {
        return 0;
    }
    return n;
}

}  // namespace

bool parse(std::string_view text, Value *out, std::string *err, const ParseLimits &limits) {
    if (text.size() > limits.max_bytes) {
        if (err) *err = "too long";
        return false;
    }
    Value v;
    Parser p(text, limits);
    if (!p.run(&v, err)) return false;
    *out = std::move(v);
    return true;
}

void append_string(std::string &out, std::string_view s) {
    out += '"';
    for (size_t i = 0; i < s.size();) {
        unsigned char c = static_cast<unsigned char>(s[i]);
        if (c >= 0x80) {
            size_t n = utf8_len(s, i);
            if (n == 0) {
                out += "\xEF\xBF\xBD";
                i++;
            } else {
                out.append(s.data() + i, n);
                i += n;
            }
            continue;
        }
        switch (c) {
        case '"': out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        default:
            if (c < 0x20 || c == 0x7F) {
                char buf[8];
                std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                out += buf;
            } else {
                out += static_cast<char>(c);
            }
        }
        i++;
    }
    out += '"';
}

void append_number(std::string &out, double v, int max_decimals) {
    if (!std::isfinite(v)) v = 0.0;
    if (max_decimals < 0) max_decimals = 0;
    if (max_decimals > 9) max_decimals = 9;
    if (std::fabs(v) >= 1e15) {
        char buf[40];
        std::snprintf(buf, sizeof(buf), "%.0f", v);
        out += buf;
        return;
    }
    char buf[48];
    std::snprintf(buf, sizeof(buf), "%.*f", max_decimals, v);
    // The C locale is never changed here, but don't trust a ',' decimal mark either.
    for (char *p = buf; *p; p++) {
        if (*p == ',') *p = '.';
    }
    std::string s(buf);
    if (s.find('.') != std::string::npos) {
        while (!s.empty() && s.back() == '0') s.pop_back();
        if (!s.empty() && s.back() == '.') s.pop_back();
    }
    if (s == "-0") s = "0";
    out += s;
}

void ObjectWriter::key(const char *k) {
    if (!first_) buf_ += ',';
    first_ = false;
    append_string(buf_, k);
    buf_ += ':';
}

ObjectWriter &ObjectWriter::str(const char *k, std::string_view value) {
    key(k);
    append_string(buf_, value);
    return *this;
}

ObjectWriter &ObjectWriter::num(const char *k, double value, int max_decimals) {
    key(k);
    append_number(buf_, value, max_decimals);
    return *this;
}

ObjectWriter &ObjectWriter::boolean(const char *k, bool value) {
    key(k);
    buf_ += value ? "true" : "false";
    return *this;
}

ObjectWriter &ObjectWriter::null(const char *k) {
    key(k);
    buf_ += "null";
    return *this;
}

ObjectWriter &ObjectWriter::raw(const char *k, std::string_view json_value) {
    key(k);
    buf_.append(json_value.data(), json_value.size());
    return *this;
}

}  // namespace av::json
