// Per-track chunker: one track per Zoom user id, a sequence per track counting from 0.
// A chunk covers at most `chunk_seconds` of wall-clock time from its first sample,
// [start, start + 30 s), and holds the track's audio at its true offsets: a pause inside
// the window (the SDK delivered nothing) is filled with silence. The chunk ends at its
// last real sample (no trailing silence); audio after the window starts a new chunk with
// its own timestamp, so nothing is padded across chunks.
// Not thread-safe; Core holds a lock around it.
#pragma once

#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <string>

#include "encoder.h"

namespace athena {

// A block that starts at most this much after the end of the audio so far is appended
// as it is (delivery jitter); a later one is placed at its true offset, the pause filled
// with silence.
constexpr int64_t kJitterToleranceMs = 300;
// A block that starts this much earlier than the open chunk's end (the clock stepped
// back) starts a new chunk. Smaller differences are delivery jitter.
constexpr int64_t kClockBackMs = 5000;

struct FinishedChunk {
  std::string track;       // sanitized Zoom user id
  std::string track_name;  // the latest display name seen for the track
  int sequence = 0;
  int64_t start_ms = 0;    // epoch ms of the first sample
  int sample_rate = 0;
  size_t samples = 0;      // real audio plus the silence of pauses inside the chunk
  std::string data;        // the encoded file
  double seconds() const { return sample_rate ? double(samples) / sample_rate : 0.0; }
};

class TrackChunker {
 public:
  using Sink = std::function<void(FinishedChunk&&)>;
  using EncoderFactory = std::function<std::unique_ptr<ChunkEncoder>(int sample_rate)>;

  TrackChunker(int chunk_seconds, EncoderFactory factory, Sink sink);

  void add(const std::string& track_id, const std::string& track_name, const int16_t* pcm,
           size_t samples, int sample_rate, int64_t now_ms);
  // The participant left: their open chunk is finished now.
  void close_track(const std::string& track_id);
  // Finish the open chunks whose window is over at `now_ms`, so a track that went quiet
  // is uploaded without waiting for it to speak again.
  void close_expired(int64_t now_ms);
  // Finish every open chunk (a partial last chunk too).
  void flush();

  int next_sequence(const std::string& track_id) const;
  size_t open_chunks() const { return open_; }

 private:
  struct Track {
    std::string name;
    int next_seq = 0;
    std::unique_ptr<ChunkEncoder> enc;  // set while a chunk is open
    int64_t start_ms = 0;
    int rate = 0;
    size_t samples = 0;
    int64_t end_ms() const { return start_ms + int64_t(samples) * 1000 / rate; }
  };
  bool open(const std::string& key, Track& t, int rate, int64_t start_ms);
  void close(const std::string& key, Track& t);
  int64_t window_ms() const { return int64_t(chunk_seconds_) * 1000; }

  int chunk_seconds_;
  EncoderFactory factory_;
  Sink sink_;
  std::map<std::string, Track> tracks_;
  size_t open_ = 0;
};

}  // namespace athena
