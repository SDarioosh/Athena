#include "chunker.h"

#include <algorithm>
#include <cstdio>

#include "util.h"

namespace athena {

TrackChunker::TrackChunker(int chunk_seconds, EncoderFactory factory, Sink sink)
    : chunk_seconds_(chunk_seconds > 0 ? chunk_seconds : 30),
      factory_(std::move(factory)),
      sink_(std::move(sink)) {}

bool TrackChunker::open(const std::string& key, Track& t, int rate, int64_t start_ms) {
  t.enc = factory_(rate);
  if (!t.enc) {
    std::fprintf(stderr, "athena-core: no encoder for track %s at %d Hz, audio dropped\n",
                 key.c_str(), rate);
    return false;
  }
  t.start_ms = start_ms;
  t.rate = rate;
  t.samples = 0;
  return true;
}

void TrackChunker::close(const std::string& key, Track& t) {
  if (!t.enc) return;
  FinishedChunk c;
  c.data = t.enc->finish();
  t.enc.reset();
  if (t.samples == 0) return;
  if (c.data.empty()) {
    std::fprintf(stderr, "athena-core: encoding failed, chunk of track %s dropped\n",
                 key.c_str());
    return;
  }
  c.track = key;
  c.track_name = t.name;
  c.sequence = t.next_seq++;
  c.start_ms = t.start_ms;
  c.sample_rate = t.rate;
  c.samples = t.samples;
  sink_(std::move(c));
}

void TrackChunker::add(const std::string& track_id, const std::string& track_name,
                       const int16_t* pcm, size_t samples, int sample_rate,
                       int64_t now_ms) {
  if (!pcm || samples == 0 || sample_rate <= 0) return;
  const std::string key = util::sanitize_track_id(track_id);
  Track& t = tracks_[key];
  if (!track_name.empty()) t.name = track_name;
  if (t.enc) {
    const int64_t late = now_ms - t.end_ms();
    if (sample_rate != t.rate || late > kGapToleranceMs || late < -kClockBackMs)
      close(key, t);
  }
  if (!t.enc && !open(key, t, sample_rate, now_ms)) return;
  const size_t cap = size_t(chunk_seconds_) * size_t(t.rate);
  while (samples > 0) {
    const size_t take = std::min(samples, cap - t.samples);
    t.enc->write(pcm, take);
    t.samples += take;
    pcm += take;
    samples -= take;
    if (t.samples >= cap) {
      const int64_t next_start = t.start_ms + int64_t(chunk_seconds_) * 1000;
      const int rate = t.rate;
      close(key, t);
      if (samples > 0 && !open(key, t, rate, next_start)) return;
    }
  }
}

void TrackChunker::close_track(const std::string& track_id) {
  const std::string key = util::sanitize_track_id(track_id);
  auto it = tracks_.find(key);
  if (it != tracks_.end()) close(key, it->second);
}

void TrackChunker::close_stale(int64_t now_ms) {
  for (auto& [key, t] : tracks_)
    if (t.enc && now_ms - t.end_ms() > kGapToleranceMs) close(key, t);
}

void TrackChunker::flush() {
  for (auto& [key, t] : tracks_) close(key, t);
}

int TrackChunker::next_sequence(const std::string& track_id) const {
  auto it = tracks_.find(util::sanitize_track_id(track_id));
  return it == tracks_.end() ? 0 : it->second.next_seq;
}

size_t TrackChunker::open_chunks() const {
  size_t n = 0;
  for (const auto& kv : tracks_) n += kv.second.enc ? 1 : 0;
  return n;
}

}  // namespace athena
