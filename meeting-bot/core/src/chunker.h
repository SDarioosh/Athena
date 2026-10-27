// Per-track chunker: one track per Zoom user id, chunks of `chunk_seconds` of that track's
// wall-clock time, a sequence per track counting from 0. Audio the SDK doesn't deliver is
// not padded: a gap starts a new chunk whose timestamp is its first sample's.
// Not thread-safe; Core holds a lock around it.
#pragma once

#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <string>

#include "encoder.h"

namespace athena {

// A block that starts more than this later than the end of the track's open chunk (the
// SDK delivered nothing in between) closes that chunk; the block starts a new one.
constexpr int64_t kGapToleranceMs = 300;
// A block that starts this much earlier than the open chunk's end (the clock stepped
// back) also starts a new chunk. Smaller differences are delivery jitter.
constexpr int64_t kClockBackMs = 5000;

struct FinishedChunk {
  std::string track;       // sanitized Zoom user id
  std::string track_name;  // the latest display name seen for the track
  int sequence = 0;
  int64_t start_ms = 0;    // epoch ms of the first sample
  int sample_rate = 0;
  size_t samples = 0;
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
  // The participant left: their open chunk is finished now, not when they speak again.
  void close_track(const std::string& track_id);
  // Finish open chunks of tracks that went quiet (no block for longer than the gap
  // tolerance before `now_ms`), so their audio goes out without waiting for them.
  void close_stale(int64_t now_ms);
  // Finish every open chunk (a partial last chunk too).
  void flush();

  int next_sequence(const std::string& track_id) const;
  size_t open_chunks() const;

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

  int chunk_seconds_;
  EncoderFactory factory_;
  Sink sink_;
  std::map<std::string, Track> tracks_;
};

}  // namespace athena
