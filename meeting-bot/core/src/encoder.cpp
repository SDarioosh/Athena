#include "encoder.h"

#include <opusenc.h>

#include <cstdio>
#include <cstring>

namespace athena {

namespace {

void put_le(std::string& o, uint32_t v, int bytes) {
  for (int i = 0; i < bytes; ++i) o += static_cast<char>((v >> (8 * i)) & 0xFF);
}

class WavEncoder : public ChunkEncoder {
 public:
  explicit WavEncoder(int rate) : rate_(rate) { data_.reserve(static_cast<size_t>(rate) * 2); }
  bool write(const int16_t* pcm, size_t samples) override {
    for (size_t i = 0; i < samples; ++i) put_le(data_, static_cast<uint16_t>(pcm[i]), 2);
    return true;
  }
  std::string finish() override {
    std::string o;
    o.reserve(44 + data_.size());
    const auto n = static_cast<uint32_t>(data_.size());
    o += "RIFF";
    put_le(o, 36 + n, 4);
    o += "WAVEfmt ";
    put_le(o, 16, 4);                                   // fmt chunk size
    put_le(o, 1, 2);                                    // PCM
    put_le(o, 1, 2);                                    // mono
    put_le(o, static_cast<uint32_t>(rate_), 4);         // sample rate
    put_le(o, static_cast<uint32_t>(rate_) * 2, 4);     // byte rate
    put_le(o, 2, 2);                                    // block align
    put_le(o, 16, 2);                                   // bits per sample
    o += "data";
    put_le(o, n, 4);
    o += data_;
    data_.clear();
    return o;
  }

 private:
  int rate_;
  std::string data_;
};

class OpusEncoder : public ChunkEncoder {
 public:
  explicit OpusEncoder(int rate) {
    OpusEncCallbacks cb{&OpusEncoder::on_write, &OpusEncoder::on_close};
    comments_ = ope_comments_create();
    int err = OPE_OK;
    if (comments_) enc_ = ope_encoder_create_callbacks(&cb, this, comments_, rate, 1, 0, &err);
    if (!enc_) {
      std::fprintf(stderr, "athena-core: opus encoder failed: %s\n", ope_strerror(err));
      return;
    }
    ope_encoder_ctl(enc_, OPUS_SET_BITRATE(kOpusBitrate));
    ope_encoder_ctl(enc_, OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE));
    // DTX: the silence that fills pauses inside a chunk costs almost nothing.
    ope_encoder_ctl(enc_, OPUS_SET_DTX(1));
  }
  ~OpusEncoder() override {
    if (enc_) ope_encoder_destroy(enc_);
    if (comments_) ope_comments_destroy(comments_);
  }
  bool ok() const { return enc_ != nullptr; }
  bool write(const int16_t* pcm, size_t samples) override {
    if (!enc_ || failed_) return false;
    if (ope_encoder_write(enc_, pcm, static_cast<int>(samples)) != OPE_OK) failed_ = true;
    return !failed_;
  }
  std::string finish() override {
    if (!enc_ || failed_) return {};
    if (ope_encoder_drain(enc_) != OPE_OK) return {};
    ope_encoder_destroy(enc_);
    enc_ = nullptr;
    return std::move(out_);
  }

 private:
  static int on_write(void* user, const unsigned char* ptr, opus_int32 len) {
    static_cast<OpusEncoder*>(user)->out_.append(reinterpret_cast<const char*>(ptr),
                                                 static_cast<size_t>(len));
    return 0;
  }
  static int on_close(void*) { return 0; }

  OggOpusComments* comments_ = nullptr;
  OggOpusEnc* enc_ = nullptr;
  bool failed_ = false;
  std::string out_;
};

}  // namespace

std::unique_ptr<ChunkEncoder> make_encoder(const std::string& codec, int sample_rate) {
  if (sample_rate < 8000 || sample_rate > 192000) return nullptr;
  if (codec == "wav") return std::make_unique<WavEncoder>(sample_rate);
  if (codec == "opus") {
    auto e = std::make_unique<OpusEncoder>(sample_rate);
    if (e->ok()) return e;
  }
  return nullptr;
}

const char* codec_extension(const std::string& codec) {
  return codec == "wav" ? "wav" : "opus";
}

const char* codec_mime(const std::string& codec) {
  return codec == "wav" ? "audio/wav" : "audio/ogg";
}

}  // namespace athena
