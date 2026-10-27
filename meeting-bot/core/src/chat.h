// The meeting's chat lines, topic and participants (lane 3A). Chat lines go to
// <work_dir>/chat.jsonl as they arrive (one JSON object per line, already in the shape of
// the server's `lines[]`); participants and topic stay in memory and in
// <work_dir>/participants.json. finish() sends them in one POST /v1/sessions/{id}/chat
// (server/README.md, "Meeting chat, topic and participants"). Thread-safe.
#pragma once

#include <cstdint>
#include <cstdio>
#include <mutex>
#include <string>
#include <vector>

namespace athena {

constexpr size_t kMaxChatLines = 2000;           // the server's cap per call
constexpr size_t kMaxChatFileBytes = 1000000;    // 1 MB of chat.jsonl
constexpr size_t kMaxChatBodyBytes = 1000000;    // the server's request body cap
constexpr size_t kMaxParticipants = 1000;        // the server's cap per call
constexpr size_t kMaxNameChars = 200;
constexpr size_t kMaxTextChars = 4000;
constexpr size_t kMaxTopicChars = 200;

class ChatLog {
 public:
  explicit ChatLog(std::string work_dir);
  ~ChatLog();

  void on_chat(int64_t at_ms, const std::string& from_name, const std::string& from_track_id,
               const std::string& text, bool private_to_bot);
  void on_participant(const std::string& track_id, const std::string& name, bool joined,
                      int64_t at_ms);
  void set_topic(const std::string& topic);

  // The JSON body of the chat call: {"lines": [...], "topic": ..., "participants": [...]}
  // (topic and participants left out when unknown, so the server keeps what it has).
  // At most kMaxChatBodyBytes: the newest lines are left out if needed.
  std::string body() const;

  size_t lines() const;
  size_t dropped() const;          // over a cap
  size_t private_dropped() const;  // sent to Athena alone: never stored
  void remove_files();             // after the server took them

 private:
  struct Participant {
    std::string id, name;
    int64_t joined_ms = 0, left_ms = 0;  // 0 = unknown / still in
  };
  void write_participants_locked();
  std::string tail_locked() const;  // ", \"topic\": ..., \"participants\": [...]"

  std::string chat_path_, participants_path_;
  mutable std::mutex mu_;
  std::FILE* chat_ = nullptr;
  size_t lines_ = 0, bytes_ = 0, dropped_ = 0, private_dropped_ = 0;
  std::string topic_;
  std::vector<Participant> participants_;  // order of first join
};

}  // namespace athena
