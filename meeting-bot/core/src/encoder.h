// Chunk encoders. Each chunk is encoded as it grows (one write per PCM block), so closing
// a 30 s chunk costs only the encoder's drain and no PCM piles up in memory.
#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>

namespace athena {

class ChunkEncoder {
 public:
  virtual ~ChunkEncoder() = default;
  // Mono 16-bit PCM at the rate given to make_encoder().
  virtual bool write(const int16_t* pcm, size_t samples) = 0;
  // The finished file (Ogg/Opus or WAV). Empty on failure. Called once.
  virtual std::string finish() = 0;
};

constexpr int kOpusBitrate = 32000;

// codec "opus" (Ogg/Opus, 32 kbps) or "wav"; nullptr for another codec or a failure.
std::unique_ptr<ChunkEncoder> make_encoder(const std::string& codec, int sample_rate);

// The file extension and the MIME type of the multipart part.
const char* codec_extension(const std::string& codec);
const char* codec_mime(const std::string& codec);

}  // namespace athena
