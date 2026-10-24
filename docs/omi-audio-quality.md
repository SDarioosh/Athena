# How Omi gets usable transcripts from people around the wearer, and what to copy

Date: 2026-10-20. Source read: `BasedHardware/omi` @ `bc5482c` (2026-10-19), firmware
`omi/firmware/omi/`, backend `backend/utils/stt/`, `backend/parakeet/`. Related:
`docs/omi-ingestion.md` (BLE protocol), `docs/t-watch-ultra-plan.md`,
`docs/xiao-esp32s3-sense-plan.md`. Only the techniques are taken from Omi. No Omi code
is copied, and nothing here changes the local-only rule.

## 1. Short answer

Omi's hardware does almost nothing special. It has two cheap PDM MEMS mics averaged to
mono, a fixed hardware gain, and 32 kbps Opus. There is no beamforming, no noise
suppression and no echo cancelling on the device. The room pickup works because of
what the server does with quiet audio:

1. **Bounded peak AGC before VAD and before ASR.** Far-field voices arrive quiet. Omi
   boosts a *copy* of the audio before Silero scores it, so quiet speech across the room
   isn't dropped as silence. It boosts the audio sent to the ASR model only when a window
   is actually quiet.
2. **A strong local ASR model.** Omi runs NVIDIA Parakeet itself (`parakeet-tdt-0.6b-v3`
   for batch, RNNT 1.1b for streaming). Its hosted Deepgram is retired in code. Either
   model could run on our server.
3. **ASR windows that start at an utterance boundary**, plus a re-post when the model
   skips a leading utterance.
4. **Whole-file speaker clustering**, then a match against the enrolled owner with a
   threshold *and* a margin. We already do this.

We already do (3) and (4), and our firmware is *ahead* of Omi's on (0), the on-device
conditioning. The gaps are (1) on the server, a model A/B for (2), a higher Opus bitrate
for quiet speech, and an eval that measures far-field audio at all (§4.1).

## 2. What Omi does, layer by layer

### 2.1 Device (firmware `omi/firmware/omi/src/mic.c`, `lib/core/codec.c`, `config.h`)

| Item | Omi CV1 | Ours (T-Watch Ultra, `firmware/recorder/`) |
|---|---|---|
| Mic | 2 × PDM MEMS (L/R on one PDM bus, `CHANNELS 2`), the T5838 (`t5838_aad.c`) | 1 × TDK T3902 PDM |
| Multi-mic processing | `(L + R) >> 1`, a plain average. No beamforming | n/a |
| Conditioning | none in software. nRF PDM hardware gain only | 2nd-order HPF, T-Watch EQ, 30 dB gain, peak limiter (`audio_dsp.c`) |
| Gain | `mic_set_gain()` levels 0–8 → nRF `GAINL/R` 0x00–0x50. Default level 6 = `0x3C` | `gain` console command, 0–42 dB |
| Codec | Opus CELT, `RESTRICTED_LOWDELAY`, **32 kbps** VBR, complexity 3, 20 ms, `SIGNAL_VOICE`, no DTX/FEC | Opus `VOIP`, **16 kbps** VBR, complexity 1, no DTX |
| Wake on sound | T5838 hardware AAD: PDM off after 10 s below avg-abs 250, wakes at a 75 dB SPL threshold, 2 kHz LPF (`omi.conf`, `t5838_aad.c`) | none (manual record) |

Notes:
- Omi's gain comments are wrong. The nRF5340 PDM gain register is -20 dB at 0x00, 0 dB at
  0x28 and +20 dB at 0x50, in 0.5 dB steps. So the "+20 dB default" is really **+10 dB**,
  the maximum is +20 dB, and "mute" is -20 dB. Our T-Watch needed 30 dB of digital gain
  to bring speech at 30 cm up to about -24 dBFS. Omi's stream is quiet by design, which
  is why its server has an AGC.
- Averaging two omni mics gives at most ~3 dB SNR against uncorrelated mic self-noise and
  nothing against room noise. It is not directional.
- The AAD wake threshold (75 dB SPL) mostly wakes on the wearer's own voice. Once the
  mic is awake, a 10 s hold at a low software threshold keeps it running while others
  talk. This matters for the XIAO wake-on-sound idea (`xiao-esp32s3-sense-plan.md`
  §7.4): **a hardware wake threshold set for the wearer will miss someone who starts
  talking across the room while the wearer is silent.** Use a long hold, or keep the
  first second with a pre-roll buffer.

### 2.2 Server: level (`backend/utils/stt/parakeet_window.py`, `parakeet/stream_handler.py`)

- `bounded_agc_pcm16`: scale toward a peak of **0.8 of full scale**, gain capped at
  **4× (12 dB)**, never attenuate, digital silence unchanged.
- **Two stages with different rules:**
  - *Admission (VAD):* Silero always scores a gained **copy**. Their comment: this is
    "what admits quiet far-field". The stored audio isn't changed.
  - *Decode (ASR):* one uniform gain per posted window, applied only if the window's
    last 10 s peak at or below **0.4 of full scale** (a deadband), and limited so the
    whole window doesn't clip. Their measurement: gaining audio that was already loud
    raised substitutions on a dense-speech clip from 27 to 37. Judging the session-wide
    peak instead of each window dropped quiet passages (37 reference words lost).
- The streaming service uses the same AGC (`PARAKEET_AGC_TARGET=0.8`), Silero at 0.5,
  0.8 s hangover and a 30 s max segment.
- There is **no** denoising, high-pass or loudness normalisation anywhere in the backend
  (grep for denoise, rnnoise, deepfilter, loudnorm and highpass finds nothing).

### 2.3 Server: ASR

- `backend/parakeet/README.md`: batch `nvidia/parakeet-tdt-0.6b-v3` (punctuation,
  timestamps), streaming RNNT 1.1b (2 s chunks, 10 s left context, lowercase output).
  Long files use local attention (`change_attention_model("rel_pos_local_attn")`).
- Omi's provider policy (`docs/doc/developer/backend/transcription.mdx`) serves
  `modulate-velma-2,parakeet`. Hosted Deepgram can't be re-enabled from config.
- Windowing lessons from `parakeet_window.py`, useful for any ASR:
  - A fixed 6 s slice that starts mid-utterance makes TDT return nothing. Every request
    starts at a sentence boundary instead.
  - TDT sometimes drops a whole leading utterance of a long window. If the first segment
    starts ≥ 3 s in and the VAD saw ≥ 2 s of speech before it, the head is posted again
    on its own.
  - They add a 300 ms pre-roll before each VAD speech span (`VAD_GATE_PRE_ROLL_MS`).

### 2.4 Server: speakers (`parakeet/transcribe.py`, `utils/stt/speaker_match.py`)

- Embeddings: `pyannote/wespeaker-voxceleb-resnet34-LM`. Segments under a minimum
  length are not embedded. All embeddings are clustered once per file (cosine threshold
  0.60, at most 8 speakers). They replaced online centroid matching because drifting
  centroids absorbed a late third speaker.
- Owner match: threshold 0.65 **and** a 0.10 margin over the runner-up, ≥ 5 s of
  evidence, up to 3 clips.
- This is the same design as ours (ECAPA, ≥ 3 s windows, agglomerative clustering,
  enrolled voices, owner hint). Nothing new to take, except perhaps a margin test if
  "named wrong" goes up.

## 3. Where we stand

- `server/convo_server/audio.py` decodes to 16 kHz mono with no level change.
- `pipeline.ingest_chunk` runs `SileroVAD(threshold=0.5)` on that raw level, and **only
  VAD speech spans reach whisper**. A quiet speaker across the room that Silero scores
  below 0.5 is never transcribed, and nothing logs it.
- whisper itself is fairly level-tolerant (its log-mel input is normalised per window),
  so for us the VAD gate matters more than gaining what whisper hears.
- `eval/recognition.py` uses AMI **`Mix-Headset.wav`**: close-talk headset mics. Our
  WER/DER numbers say nothing about room pickup.
- Firmware gain is fixed at 30 dB, tuned for the wearer at ~30 cm. Someone 2 m away is
  roughly 15–20 dB quieter, so at about -40 dBFS, in the range where Silero starts to
  miss speech.

## 4. Proposals, in order

Each one is measured before it becomes the default, as with the whisper prompt and
pyannote.

### 4.1 Measure far-field first (eval only, small)

The owner wants both cases (2026-10-20): others at **~1 m** (a table) and at **1–3 m**
(standing or walking). Add `--audio {headset,array,sim}` to `eval/recognition.py`:

- `array` (the 1 m case): `{m}.Array1-01.wav`, one omni mic of the AMI table array in the
  middle of the table, about 1 m from each speaker. A real room, real reverb, real
  overlap. Same references, same metrics.
- `sim` (the 1–3 m case): AMI has no fixed-distance recordings, so simulate them. Take
  each speaker's own headset track (`{m}.Headset-<n>.wav`), keep the owner close
  (~20 cm), and place the others at 1, 2 and 3 m in a simulated room
  (`pyroomacoustics`, MIT, as an eval-only extra), plus some room noise. Report the
  result per distance.
- Report missed speech (a DER component) separately: that number is the VAD gate.

### 4.2 Gained copy for VAD (server, small)

In `Pipeline.ingest_chunk` (and the enroll path), run VAD on a copy scaled toward a
0.8 peak, with the gain capped (start at 4×, try 8× because our recorders may also
have no gain), never attenuating. Keep the stored audio as it was. Settings:
`CONVO_VAD_AGC_MAX_GAIN` (1 = off). Also try a 0.3 s pre-roll on each span
(`speech_pad_ms` in Silero). The aim is less missed speech on the array eval with no
rise in false alarm. The 30 s chunk is the window; a per-chunk peak is the same
per-window rule Omi found works better than a session peak.

### 4.3 Gain for whisper only when quiet (server, small, may not help)

Same bounded AGC on the PCM sent to whisper, with Omi's 0.4 deadband. Expect little,
because whisper normalises internally, but it is a one-line A/B on the array eval. Keep
it only if WER drops.

### 4.4 Parakeet TDT 0.6B v3 as a second `Transcriber` (server, medium; built, see §7)

- Local, CC-BY-4.0, ~0.6 B parameters. Fits the owner's GPU next to whisper; runs with
  NeMo, or ONNX via `sherpa-onnx` / `onnx-asr` without the NeMo stack.
- Public claims: better than whisper large-v3 on the clean English Open ASR
  leaderboard, much faster, and less prone to text on silence. **No public far-field
  comparison**, so this has to be our own measurement on the array eval.
- Gives timestamps per segment/word, so the diarizer and links work unchanged. Lacks
  `CONVO_WHISPER_PROMPT`-style name prompting, and covers 25 European languages
  (whisper: ~99).
- Built as `ParakeetTranscriber` (in-process, sherpa-onnx int8 on CPU), set with
  `CONVO_TRANSCRIBER=parakeet` + `CONVO_PARAKEET_MODEL_DIR`, plus
  `CONVO_PARAKEET_REDECODE_HOLES=true`. Keep whisper as the default until the eval says
  otherwise. Post VAD spans as they are (they start at speech, which TDT needs). Add
  Omi's head re-post rule only if missing leading words show up.

### 4.5 Higher Opus bitrate for quiet speech (firmware, milestone 3)

Omi spends 32 kbps; we chose 16 kbps for battery. Low-level, reverberant far-field
speech is where low Opus bitrates lose the most. Add 24 and 32 kbps and the "others
at 1–2 m" case to the milestone 3 matrix (`t-watch-ultra-plan.md` §8). The cost is
BLE/SD bytes, not CPU (complexity stays 1). Sandbox step: re-encode the AMI array
audio at 12/16/24/32 kbps with ffmpeg `libopus` and run 4.1 on each, before any
watch test.

### 4.6 Hardware notes for later boards

- A second mic only helps with real processing (fixed beamforming toward the room is
  not what a wearable wants; at most, dual-mic noise suppression). Omi's averaging is
  not worth copying.
- Keep the device AGC-free (fixed gain + limiter, as now) so the server sees stable
  levels and can judge each window.
- If wake-on-sound comes (XIAO §7.4), see the note in §2.1.

## 5. Not worth copying

- Omi's streaming machinery (provider failover, admission, rollout arms): it serves a
  cloud fleet with live captions. We transcribe finished sessions in batch.
- Speech enhancement / denoising: Omi doesn't use it, and on whisper it often costs WER
  (artefacts). Only try it if 4.1 shows noise, not level, as the problem.

## 6. Owner decisions (2026-10-20)

1. **Both cases count:** others at ~1 m and at 1–3 m. The eval covers both (§4.1).
2. **A higher Opus bitrate is OK** if it gives notably better transcription. "Notably"
   still needs a number. Proposal: keep 24 or 32 kbps when it cuts WER by ≥ 2 points
   absolute, or missed speech by ≥ 20% relative, on the 2–3 m case.
3. **Parakeet: depends on its cost.** It is small, so the LLM doesn't need to shrink:
   - `parakeet-tdt-0.6b-v3` has ~0.6 B parameters, about 1.2 GB of weights in
     fp16/bf16. Expect roughly 2–3 GB of VRAM for long recordings with local attention
     (to measure). whisper large-v3 has 1.55 B parameters, and turbo q5 in whisper.cpp
     needs about 1–1.5 GB.
   - If Parakeet wins, it **replaces** whisper, so the extra VRAM is only the
     difference: about 1 GB.
   - The int8 ONNX export (`sherpa-onnx`, ~0.7 GB) runs on CPU faster than real time.
     Our transcription is batch work after a session ends, so it could stay off the
     GPU entirely.
   - Moving the LLM from qwen3.5-35b to a 9B model would save far more VRAM, but it
     would likely cost answer quality (target ≥ 15/20 is measured with the 35B model).
     Not needed for this.

## 7. Results (sandbox, 2026-10-20)

Setup: `eval/recognition.py` on 3 AMI meetings (ES2004a, IS1009a, TS3003a), 5 min each
(offset 300 s), 2 speakers enrolled, ECAPA diarizer, **whisper.cpp base.en on CPU** (the
owner's server runs large-v3-turbo, so absolute numbers are much worse here; compare rows
with each other only). Parakeet = `parakeet-tdt-0.6b-v3` int8 via sherpa-onnx, 2 CPU
threads. Pooled over the 3 meetings; differences under ~1.5 points are noise.

- `headset`: AMI close-talk mix (the old eval). `array`: AMI table mic Array1-01, the
  real ~1 m case, as recorded (~-48 dBFS on 2 of 3 meetings) or with `--level -34` (one
  fixed gain, roughly where people 1 m away land on the T-Watch with its 30 dB gain).
  `sim N m`: simulated room (5×4×2.7 m, RT60 0.5 s, SNR 30 dB), owner at 0.2 m at
  -24 dBFS, the others at N m.
- *WER owner / others* splits the errors by who said the reference word. *missed
  others*: share of the others' speech time with no transcript segment over it. It
  measures the VAD gate for whisper; for Parakeet it is inflated by tighter segment
  times, so compare it only within one model.


#### Baselines, whisper base.en

| run | WER | WER owner | WER others | missed others | confusion | named right | named wrong |
|---|---|---|---|---|---|---|---|
| headset (close-talk) (`headset-base`) | 28.9 | – | – | – | 6.7 | 89.1 | 2.5 |
| table mic, raw (~-48 dBFS) (`array`) | 49.4 | 47.0 | 53.1 | 18.0 | 21.4 | 55.9 | 2.8 |
| table mic at -34 dBFS (`array34`) | 44.2 | 41.6 | 48.4 | 6.7 | 19.6 | 74.5 | 4.5 |
| sim 1 m (`sim1`) | 33.8 | 21.3 | 53.0 | 11.8 | 14.0 | 79.7 | 2.6 |
| sim 2 m (`sim2`) | 36.8 | 21.5 | 60.3 | 12.1 | 18.4 | 70.8 | 1.7 |
| sim 3 m (`sim3`) | 40.4 | 22.5 | 68.0 | 21.3 | 13.4 | 76.3 | 2.5 |

#### VAD gain (whisper)

| run | WER | WER owner | WER others | missed others | confusion | named right | named wrong |
|---|---|---|---|---|---|---|---|
| table raw (`array`) | 49.4 | 47.0 | 53.1 | 18.0 | 21.4 | 55.9 | 2.8 |
| table raw, VAD gain 4x (`array-v4`) | 45.7 | 43.6 | 49.0 | 6.7 | 21.9 | 69.1 | 3.9 |
| table raw, VAD gain 8x (`array-v8`) | 45.4 | 43.1 | 49.1 | 3.5 | 24.6 | 62.4 | 4.2 |
| table -34 (`array34`) | 44.2 | 41.6 | 48.4 | 6.7 | 19.6 | 74.5 | 4.5 |
| table -34, VAD 4x (`array34-v4`) | 45.9 | 44.7 | 47.6 | 3.2 | 25.8 | 58.8 | 4.1 |
| table -34, VAD 4x + pad 300 ms (`array34-v4p`) | 44.1 | 41.7 | 47.9 | 4.3 | 28.1 | 50.9 | 2.4 |
| table -34, VAD 4x + ASR gain (`array34-v4a`) | 45.4 | 44.1 | 47.4 | 3.0 | 28.0 | 67.6 | 4.7 |
| sim 1 m, VAD 4x (`sim1-v4`) | 33.7 | 21.3 | 52.7 | 11.9 | 14.0 | 79.7 | 2.6 |
| sim 2 m, VAD 4x (`sim2-v4`) | 36.6 | 21.5 | 60.0 | 12.6 | 18.7 | 70.9 | 1.7 |
| sim 2 m, VAD 4x + pad (`sim2-v4p`) | 36.5 | 21.7 | 59.3 | 11.7 | 15.0 | 73.9 | 2.3 |
| sim 3 m, VAD 4x (`sim3-v4`) | 40.5 | 22.5 | 68.1 | 18.0 | 14.0 | 76.2 | 2.7 |
| sim 3 m, VAD 8x (`sim3-v8`) | 40.5 | 22.5 | 68.1 | 18.0 | 14.0 | 76.2 | 2.7 |
| sim 3 m, VAD 4x + pad (`sim3-v4p`) | 39.3 | 20.2 | 68.6 | 21.7 | 12.4 | 80.3 | 3.0 |
| sim 3 m, VAD 4x + ASR gain (`sim3-v4a`) | 40.4 | 22.5 | 68.0 | 17.8 | 13.6 | 76.2 | 2.7 |

#### Opus bitrate (whisper)

| run | WER | WER owner | WER others | missed others | confusion | named right | named wrong |
|---|---|---|---|---|---|---|---|
| sim 2 m, no codec (`sim2`) | 36.8 | 21.5 | 60.3 | 12.1 | 18.4 | 70.8 | 1.7 |
| sim 2 m, 16 kbps (`sim2-o16`) | 41.9 | 22.4 | 72.2 | 19.7 | 21.6 | 63.2 | 1.4 |
| sim 2 m, 24 kbps (`sim2-o24`) | 37.2 | 21.7 | 61.1 | 13.6 | 17.4 | 72.0 | 2.2 |
| sim 2 m, 32 kbps (`sim2-o32`) | 36.8 | 21.1 | 60.9 | 16.0 | 15.3 | 79.4 | 1.7 |
| sim 3 m, no codec (`sim3`) | 40.4 | 22.5 | 68.0 | 21.3 | 13.4 | 76.3 | 2.5 |
| sim 3 m, 16 kbps (`sim3-o16`) | 44.7 | 25.6 | 74.1 | 19.9 | 17.5 | 69.1 | 4.3 |
| sim 3 m, 24 kbps (`sim3-o24`) | 40.4 | 20.6 | 71.1 | 19.3 | 16.9 | 77.8 | 3.6 |
| sim 3 m, 32 kbps (`sim3-o32`) | 39.2 | 22.4 | 65.2 | 17.3 | 17.7 | 68.8 | 3.5 |

#### Parakeet TDT 0.6B v3 (int8, CPU) vs whisper base.en

| run | WER | WER owner | WER others | missed others | confusion | named right | named wrong |
|---|---|---|---|---|---|---|---|
| headset, whisper (`headset-base`) | 28.9 | – | – | – | 6.7 | 89.1 | 2.5 |
| headset, Parakeet (`headset-pk`) | 32.6 | 22.7 | 48.0 | 38.5 | 4.9 | 90.5 | 1.7 |
| headset, Parakeet + holes (`headset-pk-h`) | 26.3 | 18.7 | 38.0 | 28.9 | 6.0 | 90.5 | 2.0 |
| table raw, whisper (`array`) | 49.4 | 47.0 | 53.1 | 18.0 | 21.4 | 55.9 | 2.8 |
| table raw, Parakeet (`array-pk`) | 47.6 | 45.4 | 50.9 | 41.9 | 7.2 | 82.7 | 0.6 |
| table raw, Parakeet + VAD 4x (`array-pk-v4`) | 44.8 | 41.4 | 50.1 | 38.0 | 9.7 | 81.0 | 0.7 |
| table -34, whisper (`array34`) | 44.2 | 41.6 | 48.4 | 6.7 | 19.6 | 74.5 | 4.5 |
| table -34, Parakeet (`array34-pk`) | 44.8 | 41.7 | 49.7 | 36.1 | 12.6 | 78.9 | 0.7 |
| table -34, Parakeet + VAD 4x (`array34-pk-v4`) | 44.1 | 41.4 | 48.4 | 35.8 | 12.3 | 74.0 | 0.4 |
| table -34, Parakeet + holes (`array34-pk-h`) | 42.5 | 39.7 | 46.9 | 32.3 | 14.5 | 76.5 | 0.7 |
| sim 1 m, whisper (`sim1`) | 33.8 | 21.3 | 53.0 | 11.8 | 14.0 | 79.7 | 2.6 |
| sim 1 m, Parakeet (`sim1-pk`) | 29.7 | 14.9 | 52.5 | 36.4 | 10.2 | 79.4 | 1.4 |
| sim 2 m, whisper (`sim2`) | 36.8 | 21.5 | 60.3 | 12.1 | 18.4 | 70.8 | 1.7 |
| sim 2 m, Parakeet (`sim2-pk`) | 31.8 | 15.7 | 56.5 | 39.8 | 12.8 | 75.5 | 0.2 |
| sim 2 m, Parakeet + VAD 4x (`sim2-pk-v4`) | 32.3 | 15.8 | 57.8 | 40.6 | 12.3 | 77.9 | 0.2 |
| sim 2 m, Parakeet + holes (`sim2-pk-h`) | 29.1 | 14.4 | 51.9 | 33.2 | 12.9 | 78.6 | 0.2 |
| sim 3 m, whisper (`sim3`) | 40.4 | 22.5 | 68.0 | 21.3 | 13.4 | 76.3 | 2.5 |
| sim 3 m, Parakeet (`sim3-pk`) | 35.0 | 14.9 | 66.1 | 39.7 | 10.2 | 80.8 | 0.7 |
| sim 3 m, Parakeet + VAD 4x (`sim3-pk-v4`) | 35.2 | 14.9 | 66.5 | 44.5 | 8.8 | 82.0 | 0.7 |
| sim 3 m, Parakeet + holes (`sim3-pk-h`) | 33.8 | 14.2 | 64.2 | 39.1 | 11.4 | 77.7 | 0.7 |


### 7.1 What the numbers say

1. **Opus at 16 kbps costs the people around the wearer.** At 2 m, WER goes from 36.8 % (no
   codec) to 41.9 % (+5.1), at 3 m from 40.4 % to 44.7 % (+4.3). For the others alone,
   60 % → 72 % at 2 m. 24 kbps is within noise of no codec (37.2 %, 40.4 %), and 32 kbps
   adds nothing reliable. This passes the owner's rule (≥ 2 points) clearly. **The
   firmware default is now 24 kbps** (`firmware/recorder/main/opus_enc.h`): 50 % more
   bytes over BLE and on the SD card. Battery and drain time still need measuring on the
   watch.
2. **Parakeet with hole re-decoding beats whisper base.en everywhere:** headset 26.3 vs
   28.9, table 42.5 vs 44.2, 2 m 29.1 vs 36.8, 3 m 33.8 vs 40.4. The owner's own speech
   improves most (≈ 14–15 % vs ≈ 21–22 %), and wrong names drop (≤ 0.7 % vs 1.7–4.5 %).
   Without hole re-decoding Parakeet skips whole stretches mid-chunk (headset 32.6 %,
   worse than whisper). It costs ~1.1 GB RAM and ~0.11× real time on 2 CPU threads, and
   no GPU. **The real question is Parakeet vs large-v3-turbo on the server**, which this
   sandbox can't answer. That is the Server verification. `CONVO_TRANSCRIBER` stays
   `whisper` until then.
3. **VAD gain only helps on very quiet input.** On the raw table mic (~-48 dBFS) a 4×
   gain cuts the others' missed speech from 18 % to 6.7 % and WER from 49.4 % to 45.7 %. At
   device levels (sim, table at -34 dBFS) it does nothing for WER, and at -34 dBFS it
   lets noise and crosstalk in: speaker confusion 19.6 → 25.8 %, named right 74.5 →
   58.8 %. Our firmware already applies a fixed 30 dB gain, so **it stays off by
   default**. It would matter for a device without gain, such as a phone mic or the XIAO
   before tuning. 8× is no better than 4×.
4. **300 ms VAD padding and ASR gain: no consistent gain**, and padding hurt speaker
   naming on the table mic. Both stay off.
5. Room acoustics matter more than distance: in the 0.5 s RT60 room the others are only
   10–13 dB below the owner at 1–3 m (the reverb radius is ~0.6 m), and their WER is
   still 53–68 % with whisper base. That gap is the open problem; better ASR (item 2) and
   the bitrate (item 1) are the levers found so far.

### 7.3 Parakeet vs whisper large-v3-turbo (the server's model), sandbox CPU

Same eval, whisper.cpp `ggml-large-v3-turbo-q5_0` (the owner's server model) on 4 CPU
threads. WER %, pooled over 3 meetings:

| condition | whisper base.en | whisper large-v3-turbo | Parakeet + holes |
|---|---|---|---|
| headset | 28.9 | **23.6** | 26.3 |
| table ~1 m (-34 dBFS) | 44.2 | **36.7** | 42.5 |
| sim 1 m | 33.8 | 28.4 | **27.9** |
| sim 2 m | 36.8 | **29.0** | 29.1 |
| sim 3 m | 40.4 | **30.5** | 33.8 |
| *others only*, table / 2 m / 3 m | 48.4 / 60.3 / 68.0 | **42.9 / 48.8 / 52.4** | 46.9 / 51.9 / 64.2 |
| *owner only*, 1 / 2 / 3 m | 21.3 / 21.5 / 22.5 | 16.3 / 16.2 / 16.4 | **14.7 / 14.4 / 14.2** |
| speaker confusion, table / 3 m | 19.6 / 13.4 | 32.6 / 16.4 | **14.5 / 11.4** |
| named right, table / 3 m | 74.5 / 76.3 | 50.5 / 78.5 | **76.5 / 77.7** |

Speed and memory, 5 min of AMI audio, same idle 4-core CPU:

| model | time | real-time factor | peak RAM |
|---|---|---|---|
| whisper base.en | 44 s | 0.15 | 0.6 GB |
| whisper large-v3-turbo q5_0 | 282 s | 0.94 | 0.9 GB |
| Parakeet int8 + holes | 33 s | 0.11 | 1.3 GB |

- **turbo is the better model for the people around the wearer**, which is what this work is
  about: 4–12 points lower WER on the others at 1–3 m and on the table. **Keep whisper
  large-v3-turbo as the default.** Parakeet's win over base.en (§7.1) doesn't carry over.
- Parakeet is better on the **owner's own speech** (≈ 14 % vs 16 %) and gives **cleaner
  speaker labels** (on the table: confusion 14.5 % vs 32.6 %, named right 76.5 % vs
  50.5 %). turbo's segments seem to run across speaker changes on the table recording.
  That is a diarization problem worth its own look (e.g. splitting turbo's segments at
  word timestamps or VAD pauses before embedding).
- On CPU Parakeet is ~8.5× faster than turbo. On the server turbo runs on the GPU, so speed
  doesn't decide there. Parakeet stays as an option for a CPU-only box.
- The server-side Server verification still applies (real GPU, real turbo settings).
  Expected: turbo ahead on the others, Parakeet ahead on the owner.

### 7.2 Not settled here

- §7.1 uses whisper base.en; §7.3 adds large-v3-turbo on CPU (same weights as the server, q5_0).
- Simulated rooms and AMI's table mic stand in for a real watch on a wrist; milestone 3
  of the T-Watch plan records the real thing.
- `test_chunk_queue` (firmware host tests) fails in this sandbox with or without these
  changes (`cq_init` returns -5) but passes in CI, so it is sandbox-specific.

## 8. Capture quality: diarization and attribution (sandbox, 2026-10-23/24)

Hub personal-context stage 4.3. A fact attributed to the wrong speaker is wrong evidence
in the hub's belief ledger, so the numbers that matter most are **named wrong** (speech
given an enrolled person's name that someone else said) and **unenrolled named** (speech
of someone with no voice on file that got an enrolled name: a newcomer taking an existing
name). The owner runs pyannote (`CONVO_DIARIZER=pyannote`), so most work is on it.

Setup: `eval/recognition.py`, 3 AMI meetings × 5 min, whisper large-v3-turbo-q5_0 on 4 CPU
threads, 24 kbps Opus, 2 speakers enrolled (the owner and the next most active), pyannote
community-1 on CPU. New this round: `--late-joiners`, `--owner-dips`, owner precision and
recall, `unenrolled_named`, and `--cache` for pyannote turns. Percentages pooled over the
meetings; small CPU runs compare only with other sandbox runs. "split" =
`CONVO_SPEAKER_SPLIT_LINES=true` with `CONVO_SPEAKER_SPLIT_MIN_SECONDS`; "line check" =
`CONVO_SPEAKER_LINE_CHECK=true`; `Fb` = `CONVO_PYANNOTE_VBX_FB`.

#### Table mic, T-Watch level (array -24 dBFS, 24 kbps)

| run | confusion | named right | named wrong | owner precision | owner recall | unenrolled named |
|---|---|---|---|---|---|---|
| ECAPA, cluster 0.45 (default) (`a-ecapa`) | 36.4 | 42.9 | 0.8 | 97.9 | 55.5 | – |
| ECAPA, cluster 0.30 (`a-ecapa-c30`) | 17.8 | 70.0 | 1.3 | 98.4 | 77.6 | – |
| pyannote (`a-py2`) | 5.0 | 90.9 | 5.3 | 97.3 | 95.2 | 8.7 |
| pyannote + split 0.5 s (`a-py-split05`) | 4.8 | 91.4 | 4.9 | 97.7 | 94.9 | 6.7 |
| pyannote + line check (`a-pyannote-check`) | 7.2 | 88.7 | 1.8 | 98.3 | 94.5 | – |
| pyannote + split 0.5 s + line check (`a-py-best`) | 5.7 | 90.7 | 1.8 | 98.7 | 94.2 | 4.6 |
| pyannote + split 0.3 s + line check (`a-py-split03-check`) | 5.9 | 90.7 | 1.6 | 98.7 | 94.2 | 4.6 |
| … with WeSpeaker naming (`a-py-best-wes`) | 6.9 | 88.3 | 1.7 | 98.7 | 93.8 | 5.7 |
| pyannote + match margin 0.10 (`a-py-margin10`) | 5.0 | 90.9 | 5.3 | 97.3 | 95.2 | 8.7 |
| pyannote + name min 5 s (`a-py-min5`) | 5.0 | 90.9 | 5.3 | 97.3 | 95.2 | 8.7 |
| pyannote cluster threshold 0.5 (`a-py-pct0.5`) | 4.9 | 90.6 | 5.0 | 97.6 | 94.8 | 7.2 |
| pyannote Fb 0.3 (`a-py-fb0.3`) | 8.6 | 85.7 | 2.9 | 97.5 | 92.8 | 6.8 |
| pyannote Fb 0.3 + split 0.3 s + line check (`a-py-fb0.3-best3`) | 11.1 | 82.0 | 0.9 | 99.1 | 89.5 | 2.5 |

#### Room, others at 2 m (sim, 24 kbps)

| run | confusion | named right | named wrong | owner precision | owner recall | unenrolled named |
|---|---|---|---|---|---|---|
| ECAPA, cluster 0.45 (default) (`sim2o24-ecapa`) | 18.6 | 68.4 | 0.7 | 99.2 | 82.4 | – |
| ECAPA, cluster 0.30 (`sim2o24-ecapa-c30`) | 6.3 | 93.0 | 1.8 | 98.0 | 97.5 | – |
| pyannote (`s-py2`) | 2.7 | 97.2 | 3.4 | 97.7 | 98.4 | 13.2 |
| pyannote + split 0.5 s (`s-py-split05`) | 2.4 | 97.6 | 3.4 | 97.8 | 98.7 | 13.1 |
| pyannote + line check (`sim2o24-pyannote-check`) | 3.3 | 96.4 | 2.7 | 97.8 | 98.0 | 11.5 |
| pyannote + split 0.3 s + line check (`s-py-split03-check`) | 3.2 | 96.7 | 2.5 | 98.1 | 98.3 | 10.0 |

#### Late joiners (sim 2 m; 3rd and 4th person join halfway)

| run | confusion | named right | named wrong | owner precision | owner recall | unenrolled named |
|---|---|---|---|---|---|---|
| pyannote (`j-py`) | 3.2 | 95.4 | 4.3 | 97.2 | 98.1 | 15.8 |
| pyannote, owner hint off (`j-py-nohint`) | 3.2 | 95.4 | 4.3 | 97.2 | 98.1 | 15.8 |
| pyannote + line check (`j-py-check`) | 3.7 | 94.7 | 3.8 | 97.4 | 97.8 | 15.6 |
| … + floor 0.35 (`j-py-check-f0.35`) | 5.1 | 92.9 | 3.6 | 97.5 | 97.1 | 14.5 |
| pyannote + split 0.3 s + line check (`j-py-split03-check`) | 3.2 | 95.5 | 2.6 | 98.3 | 97.9 | 11.0 |
| pyannote cluster threshold 0.4 (`j-py-pct0.4`) | 3.2 | 95.4 | 4.3 | 97.2 | 98.1 | 15.8 |
| pyannote Fb 0.3 (`j-py-fb0.3`) | 4.8 | 92.4 | 3.5 | 97.3 | 97.1 | 14.9 |
| pyannote Fb 0.3 + split 0.3 s + line check (`j-py-fb0.3-best3`) | 5.3 | 92.0 | 2.2 | 98.4 | 96.9 | 10.1 |
| pyannote Fb 0.2 (`j-py-fb0.2`) | 11.0 | 80.5 | 2.2 | 97.6 | 93.1 | 7.8 |
| pyannote Fb 0.2 + split 0.5 s + line check (`j-py-fb0.2-best`) | 11.6 | 79.6 | 1.4 | 98.4 | 92.5 | 7.8 |

#### Arm movement (sim 2 m; owner drops 6–12 dB in stretches)

| run | confusion | named right | named wrong | owner precision | owner recall | unenrolled named |
|---|---|---|---|---|---|---|
| pyannote (`d-py2`) | 4.1 | 93.8 | 4.0 | 97.5 | 97.2 | 12.7 |
| pyannote, owner hint off (`d-py-nohint`) | 4.1 | 93.8 | 4.0 | 97.5 | 97.2 | 12.7 |
| pyannote + line check (`d-py-check`) | 4.9 | 93.1 | 2.8 | 97.5 | 96.9 | 11.0 |
| pyannote + split 0.3 s + line check (`d-py-split03-check`) | 4.3 | 93.7 | 1.9 | 98.6 | 96.8 | 7.7 |

### 8.1 What the numbers say

1. **Where pyannote's wrong names come from** (table mic): about 70 % from whisper lines
   that run over a short turn pyannote missed or merged, about 30 % from pyannote putting a
   turn under the wrong speaker; **0 %** from naming a whole speaker after the wrong
   enrolled person (names matched at cosine 0.8+). So the fixes work on lines, not on
   whole speakers.
2. **Split + line check is the recommended pair** (opt-in, for the owner's server run):
   line splitting at 0.3–0.5 s keeps the short turns pyannote did find (the old fixed
   1.5 s minimum folded them back), and the line check unnames a line whose own voice fits
   another enrolled person at least as well. Together: named wrong 5.3 → 1.6 % (table),
   3.4 → 2.5 % (room), 4.3 → 2.6 % (late joiners), 4.0 → 1.9 % (arm movement); unenrolled
   named down by a third to a half; names right and owner recall within a point; owner
   precision up to 98–99 %. Moving such a line to the better-fitting person instead
   measured worse (the better fit is often someone not enrolled).
3. **People joining later**: pyannote put newcomers' turns inside an enrolled person's
   speaker (IS1009a: 29 s of two newcomers under one enrolled name). Its clustering
   threshold doesn't change that (in community-1 it only seeds VBx: 0.2, 0.4, 0.5, 0.6 and
   0.9 gave the same speakers). VBx's `Fb` does: at 0.2 newcomers under an enrolled name
   drop 15.8 → 7.8 % (ES2004a 23.8 → 1.8 %), but pyannote then finds 8–11 speakers for 4
   people and names right drops 95 → 80 % (more "Speaker N"). `Fb` 0.3 is no useful
   middle: newcomers 15.8 → 14.9 % only, names right 95 → 92 % (table 91 → 86 %).
4. **Owner loudness hint**: never fired in any run, also not with `--owner-dips` (the
   owner's voice still matched the enrollment). It now logs when it fires, so a real
   wrist recording shows whether it is the cause of wrong owner labels.
5. **Not worth it here**: Omi's naming rules (`CONVO_SPEAKER_MATCH_MARGIN`,
   `CONVO_SPEAKER_NAME_MIN_SECONDS`), naming with pyannote's WeSpeaker model
   (`CONVO_SPEAKER_EMBEDDER=pyannote`), the "fits nobody" floor
   (`CONVO_SPEAKER_LINE_FLOOR`), line splitting by voice with ECAPA (removed), whisper
   beam search 5 (table 35.3 → 36.1 % WER, room 28.1 → 27.7 %). Denoising before whisper
   was not tried: two published studies found it hurts whisper.
7. **A looser speech gate** (`CONVO_VAD_THRESHOLD=0.35`, default 0.5) lets more of the
   others' far speech through to whisper: room WER 28.1 → 27.1 %, the others 47.5 → 44.6 %,
   their missed speech 13.2 → 10.9 %, the owner unchanged (15.6 → 15.7 %). Small, so it is
   in the GPU script (`*-fix-vad35`) before anyone turns it on.
6. **ECAPA** (the default diarizer) over-splits far-field speakers (4 people → 12–23
   labels); `CONVO_SPEAKER_CLUSTER_THRESHOLD=0.30` cuts table-mic confusion 36.4 → 17.8 %
   for 0.5 points more wrong names. Kept at 0.45 by default; the owner uses pyannote.

### 8.2 Not settled here

- The server GPU run (PR Server verification): whether split + line check and `Fb` hold
  on large-v3-turbo on the GPU and real pyannote speed.
- Real wrist audio: the simulated room has the owner always closest; a wrist mic facing
  the ground is not simulated. Owner-hint log lines on real recordings will tell.
- Open-source ideas not tried yet (research notes in the PR): DiariZen (best published
  far-field DER, but CC-BY-NC weights: owner decision), word times from whisper-server
  `--dtw`, score normalisation against past unnamed speakers.

### 8.3 Server GPU run (owner, 2026-10-24)

`server/eval/capture_quality.sh` on the RTX 3060 (whisper large-v3-turbo-q5_0, pyannote
on CUDA). "fix" = split 0.3 s + line check.

| run | confusion | named right | named wrong | owner precision | owner recall | unenrolled named | WER |
|---|---|---|---|---|---|---|---|
| table, pyannote | 5.6 | 90.2 | 5.8 | 97.4 | 94.2 | 8.3 | 36.0 |
| table, fix | 6.2 | 90.6 | **1.6** | 98.7 | 94.1 | **4.7** | 36.1 |
| table, Fb 0.2 + fix | 16.6 | 72.8 | 0.6 | 99.6 | 87.7 | 0.8 | 36.2 |
| room, pyannote | 2.4 | 98.0 | 3.2 | 98.0 | 99.0 | 12.1 | 28.3 |
| room, fix | 3.3 | 96.6 | **2.1** | 98.6 | 98.2 | **7.7** | 28.4 |
| room, Fb 0.2 + fix | 6.4 | 92.0 | 1.1 | 98.6 | 97.3 | 2.7 | 28.3 |
| joiners, pyannote | 2.6 | 96.8 | 4.2 | 97.1 | 98.9 | 17.1 | 27.6 |
| joiners, fix | 2.9 | 96.4 | **2.6** | 98.5 | 98.2 | **9.8** | 27.7 |
| joiners, Fb 0.2 + fix | 10.8 | 82.1 | 1.1 | 98.8 | 93.0 | 4.0 | 27.7 |
| arm, pyannote | 3.8 | 94.0 | 3.6 | 97.7 | 97.6 | 14.9 | 27.4 |
| arm, fix | 4.5 | 93.3 | **2.6** | 98.2 | 97.0 | **11.1** | 27.5 |
| arm, Fb 0.2 + fix | 14.5 | 74.7 | 0.7 | 98.9 | 84.4 | 3.1 | 27.4 |

The sandbox findings hold on the GPU: the fix cuts wrong names by 30–70 % and newcomers
under an enrolled name by 25–45 %, names right within 1.4 points, WER unchanged.
`Fb` 0.2 + fix takes wrong names below ~1 % and newcomers to 1–4 %, at the cost of
names right (−6 to −19 points) and owner recall (−2 to −13): more "Speaker N" lines to
name by hand, fewer wrong ones. `Fb` 0.3 adds nothing over the fix alone. Full table
(all 24 runs) in the PR.

A looser speech gate on top of the fix (`CONVO_VAD_THRESHOLD=0.35`): WER table 36.1 →
35.2 %, room 28.4 → 27.6 % (the others gain most: e.g. the ES2004a table meeting 43.1 →
41.6 %, its missed speech 0.0 % on TS3003a); attribution about the same (table named wrong
1.6 → 1.8 %, unenrolled named 4.7 → 5.5 %; room 2.1 → 2.1 %, 7.7 → 7.4 %). Same direction
and size as the sandbox (room 28.1 → 27.1 %): worth turning on with the fix.

**Decision (owner, 2026-10-24):** with `CONVO_DIARIZER=pyannote`, split 0.3 s + line
check + `CONVO_VAD_THRESHOLD=0.35` are the defaults (`Settings._diarizer_defaults`);
ECAPA keeps the old values; `Fb` stays opt-in.
