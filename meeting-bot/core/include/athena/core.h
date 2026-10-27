// Athena meeting bot core: per-track chunking, encoding, the on-disk upload queue, the
// uploader thread and the meeting's chat, topic and participants. No Zoom SDK here: the
// bot (meeting-bot/bot/) feeds it from the SDK's callbacks, and the host tests feed it
// fictional PCM. Contract: docs/zoom-meeting-bot-plan.md, lane 1A and 3A.
#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>

namespace athena {

struct CoreConfig {
  std::string upload_url;      // e.g. http://127.0.0.1:8000 (from --upload-url)
  std::string session_id;      // --session-id
  std::string upload_token;    // env ATHENA_UPLOAD_TOKEN (bearer)
  std::string codec = "opus";  // --codec opus|wav
  std::string work_dir;        // --work-dir (exists, owned by the bot; queue + chat file)
  std::string device = "zoom";
  int chunk_seconds = 30;
  int max_session_seconds = 14400;  // --max-seconds: cap on queued (unsent) audio
};

class Core {
 public:
  explicit Core(CoreConfig cfg);  // starts ONE uploader thread; no other thread/timer
  ~Core();                        // stops the uploader if finish() was not called
  Core(const Core&) = delete;
  Core& operator=(const Core&) = delete;

  // Thread-safe; called from the SDK's audio callback thread. One call per PCM block.
  // track_id = Zoom user id as decimal string (the chunk `track`), track_name = display
  // name (the chunk `track_name`). now_ms = wall-clock epoch ms of the block's start.
  void on_audio(const std::string& track_id, const std::string& track_name,
                const int16_t* pcm, size_t samples, int sample_rate, int64_t now_ms);

  // Participant joined/left (also rename: joined again with the new name).
  void on_participant(const std::string& track_id, const std::string& name,
                      bool joined, int64_t at_ms);
  // A chat message. private_to_bot = sent to Athena alone: dropped, never stored.
  void on_chat(int64_t at_ms, const std::string& from_name,
               const std::string& from_track_id, const std::string& text,
               bool private_to_bot);
  void set_topic(const std::string& topic);

  bool stop_requested() const;   // the server answered "stop": true to an upload
  bool upload_rejected() const;  // 401/403: token gone (launcher killed the meeting)

  // Close every open chunk (a partial last chunk is uploaded), drain the queue until
  // `deadline`, POST /v1/sessions/{id}/chat (lines, topic, participants) and
  // POST /v1/sessions/{id}/end, stop the uploader. Returns true if everything was sent.
  // Must return by the deadline (the bot calls it with ~4 s on SIGTERM; 5 s exit rule).
  bool finish(std::chrono::steady_clock::time_point deadline);

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace athena
