#include "chat.h"

#include <algorithm>
#include <cstdio>
#include <fstream>

#include "util.h"

namespace athena {

namespace {

std::string clean_name(const std::string& s) {
  return util::utf8_truncate(util::clean_line(s), kMaxNameChars);
}

// The server answers 400 to the whole call for a time outside 2000..2100.
int64_t sane_time(int64_t at_ms) {
  return util::plausible_epoch_ms(at_ms) ? at_ms : util::wall_now_ms();
}

}  // namespace

ChatLog::ChatLog(std::string work_dir)
    : chat_path_(work_dir + "/chat.jsonl"), participants_path_(work_dir + "/participants.json") {}

ChatLog::~ChatLog() {
  if (chat_) std::fclose(chat_);
}

void ChatLog::on_chat(int64_t at_ms, const std::string& from_name,
                      const std::string& from_track_id, const std::string& text,
                      bool private_to_bot) {
  std::lock_guard<std::mutex> lock(mu_);
  if (private_to_bot) {  // never written anywhere, not even its length
    ++private_dropped_;
    return;
  }
  const std::string clean = util::utf8_truncate(util::clean_line(text), kMaxTextChars);
  if (clean.empty()) return;  // the server skips empty lines too
  std::string line = "{\"at\": " + util::json_str(util::iso_utc_ms(sane_time(at_ms)));
  const std::string from = clean_name(from_name);
  if (!from.empty()) line += ", \"from\": " + util::json_str(from);
  if (util::valid_track_id(from_track_id))
    line += ", \"zoom_user_id\": " + util::json_str(from_track_id);
  line += ", \"text\": " + util::json_str(clean) + ", \"private\": false}\n";
  if (lines_ >= kMaxChatLines || bytes_ + line.size() > kMaxChatFileBytes) {
    if (dropped_++ == 0)
      std::fprintf(stderr, "athena-core: chat cap reached (%zu lines, %zu bytes), "
                           "later messages are not kept\n", lines_, bytes_);
    return;
  }
  if (!chat_) chat_ = std::fopen(chat_path_.c_str(), "a");
  if (!chat_ || std::fwrite(line.data(), 1, line.size(), chat_) != line.size() ||
      std::fflush(chat_) != 0) {
    std::fprintf(stderr, "athena-core: could not write %s\n", chat_path_.c_str());
    ++dropped_;
    return;
  }
  ++lines_;
  bytes_ += line.size();
}

void ChatLog::on_participant(const std::string& track_id, const std::string& name,
                             bool joined, int64_t at_ms) {
  std::lock_guard<std::mutex> lock(mu_);
  const int64_t at = util::plausible_epoch_ms(at_ms) ? at_ms : 0;
  const std::string id = util::valid_track_id(track_id) ? track_id : "";
  const std::string clean = clean_name(name);
  Participant* p = nullptr;
  if (!id.empty())
    for (auto& q : participants_)
      if (q.id == id) p = &q;
  if (!p) {
    if (!joined && id.empty()) return;
    if (participants_.size() >= kMaxParticipants) return;
    participants_.push_back(Participant{id, clean, joined ? at : 0, 0});
    p = &participants_.back();
  } else if (joined) {
    if (!clean.empty()) p->name = clean;  // a rename, or back after leaving
    if (p->joined_ms == 0) p->joined_ms = at;
    p->left_ms = 0;
  }
  if (!joined) p->left_ms = at ? at : util::wall_now_ms();
  write_participants_locked();
}

void ChatLog::set_topic(const std::string& topic) {
  std::lock_guard<std::mutex> lock(mu_);
  topic_ = util::utf8_truncate(util::clean_line(topic), kMaxTopicChars);
  write_participants_locked();
}

std::string ChatLog::tail_locked() const {
  std::string o;
  if (!topic_.empty()) o += ", \"topic\": " + util::json_str(topic_);
  if (participants_.empty()) return o;
  o += ", \"participants\": [";
  bool first_entry = true;
  for (const auto& p : participants_) {
    o += first_entry ? "{" : ", {";
    first_entry = false;
    if (!p.id.empty()) o += "\"zoom_user_id\": " + util::json_str(p.id) + ", ";
    if (!p.name.empty()) o += "\"name\": " + util::json_str(p.name) + ", ";
    o += "\"joined_at\": ";
    o += p.joined_ms ? util::json_str(util::iso_utc_ms(p.joined_ms)) : "null";
    o += ", \"left_at\": ";
    o += p.left_ms ? util::json_str(util::iso_utc_ms(p.left_ms)) : "null";
    o += "}";
  }
  return o + "]";
}

void ChatLog::write_participants_locked() {
  const std::string tail = tail_locked();
  const std::string tmp = participants_path_ + ".tmp";
  std::FILE* f = std::fopen(tmp.c_str(), "w");
  if (!f) return;
  // The same shape as the chat call without its lines.
  const std::string doc = "{\"lines\": []" + tail + "}\n";
  const bool ok = std::fwrite(doc.data(), 1, doc.size(), f) == doc.size();
  if (std::fclose(f) != 0 || !ok || std::rename(tmp.c_str(), participants_path_.c_str()) != 0) {
    std::remove(tmp.c_str());
    std::fprintf(stderr, "athena-core: could not write %s\n", participants_path_.c_str());
  }
}

std::string ChatLog::body() const {
  std::lock_guard<std::mutex> lock(mu_);
  const std::string tail = tail_locked();
  std::string o = "{\"lines\": [";
  const size_t budget = kMaxChatBodyBytes - std::min(kMaxChatBodyBytes, tail.size() + 16);
  size_t n = 0, cut = 0;
  if (lines_ > 0) {
    std::ifstream in(chat_path_);
    std::string line;
    while (std::getline(in, line)) {
      if (line.empty()) continue;
      if (o.size() + line.size() + 2 > budget) {
        ++cut;
        continue;
      }
      if (n++) o += ", ";
      o += line;
    }
  }
  if (cut)
    std::fprintf(stderr, "athena-core: %zu chat lines left out to stay under %zu bytes\n",
                 cut, kMaxChatBodyBytes);
  return o + "]" + tail + "}";
}

size_t ChatLog::lines() const {
  std::lock_guard<std::mutex> lock(mu_);
  return lines_;
}
size_t ChatLog::dropped() const {
  std::lock_guard<std::mutex> lock(mu_);
  return dropped_;
}
size_t ChatLog::private_dropped() const {
  std::lock_guard<std::mutex> lock(mu_);
  return private_dropped_;
}

void ChatLog::remove_files() {
  std::lock_guard<std::mutex> lock(mu_);
  if (chat_) {
    std::fclose(chat_);
    chat_ = nullptr;
  }
  std::remove(chat_path_.c_str());
  std::remove(participants_path_.c_str());
}

}  // namespace athena
