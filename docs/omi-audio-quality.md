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

### 4.4 Parakeet TDT 0.6B v3 as a second `Transcriber` (server, medium)

- Local, CC-BY-4.0, ~0.6 B parameters. Fits the owner's GPU next to whisper; runs with
  NeMo, or ONNX via `sherpa-onnx` / `onnx-asr` without the NeMo stack.
- Public claims: better than whisper large-v3 on the clean English Open ASR
  leaderboard, much faster, and less prone to text on silence. **No public far-field
  comparison**, so this has to be our own measurement on the array eval.
- Gives timestamps per segment/word, so the diarizer and links work unchanged. Lacks
  `CONVO_WHISPER_PROMPT`-style name prompting, and covers 25 European languages
  (whisper: ~99).
- Shape: `ParakeetServerTranscriber` behind the existing protocol, set with
  `CONVO_TRANSCRIBER=parakeet` + URL. Keep whisper as the default until the eval says
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
