#!/usr/bin/env python3
"""Recognition eval: word error rate and speaker-labelling error on public meeting audio.

Runs the server's own pipeline (Silero VAD -> whisper.cpp -> diarizer -> segments) on a
few minutes of AMI Meeting Corpus recordings and compares the stored transcript with the
corpus' manual annotations. The AMI corpus is CC-BY-4.0
(https://groups.inf.ed.ac.uk/ami/corpus/license.shtml); the speaker reference (RTTM)
comes from pyannote/AMI-diarization-setup (MIT). Nothing is committed: `fetch` downloads
into eval/data/ (gitignored).

    python eval/recognition.py fetch
    python eval/recognition.py run --label baseline              # whisper-server + ECAPA
    python eval/recognition.py run --label pyannote --diarizer pyannote --enroll 2
    python eval/recognition.py compare baseline pyannote

Audio (--audio), i.e. how far the people around the wearer are from the mic:
- ``headset`` (default, comparable with older results): AMI ``Mix-Headset.wav``, the
  close-talk headsets summed. Says nothing about room pickup.
- ``array``: ``Array1-01.wav``, one omni mic of the table array, about 1 m from everyone
  (the "others at a table" case): a real room, real reverb and overlap, its recorded
  level kept (that is the device scenario; the run prints the active speech level).
- ``sim``: the 1-3 m case, simulated (pyroomacoustics, eval extra) from each speaker's
  own headset: the owner 0.2 m from a chest mic, everyone else at --distance m in a
  shoebox room with --rt60 reverb and stationary noise at --snr dB, then a fixed device
  gain that puts the owner at -24 dBFS (our T-Watch firmware's 30 dB gain), with the
  firmware's limiter. Cached in eval/data/sim/. See ``simulate``.
--opus-kbps N round-trips the stream through libopus like our firmware does.

    python eval/recognition.py run --label array --audio array
    python eval/recognition.py run --label sim2m --audio sim --distance 2
    python eval/recognition.py run --label sim2m-o16 --audio sim --distance 2 --opus-kbps 16

Settings come from the CONVO_* environment like the server (CONVO_WHISPER_SERVER_URL,
CONVO_WHISPER_PROMPT, CONVO_DIARIZER, ...); the flags below override them. Results go to
eval/results/recognition-<label>.json (gitignored).

Metrics, per meeting window and pooled:
- WER (jiwer): lowercased, punctuation and filler words (um, uh, mm-hmm, ...) removed on
  both sides.
- DER (pyannote.metrics, 0.25 s collar), split into missed speech, false alarm and
  speaker confusion. Confusion is the speaker-labelling error; missed/false alarm mostly
  measure VAD and whisper segment boundaries.
- With --enroll N, the N speakers with the most speech get a voice profile from up to
  --enroll-seconds of their speech *outside* the window (the first is the owner).
  "named right" = share of their speech labelled with their name; "named wrong" = share
  of everyone's speech labelled with a wrong enrolled name. "owner precision" = share of
  the speech labelled as the owner that the owner said (the rest would be filed as the
  owner's words); "owner recall" = share of the owner's transcribed speech labelled as
  the owner. All on 0.1 s frames where one person speaks.
- Owner vs others (the owner is the speaker with the most speech in the window):
  ``wer_owner``/``wer_others`` split the overall WER's errors by speaker (AMI word files
  are per speaker): a substitution or deletion counts for the speaker of the reference
  word. An inserted word has no reference word, so it goes to the speaker it was
  probably said by: whisper gives no word times, so each segment's words get times
  spread over the segment by character count, and each word goes to the speaker of
  the reference word closest in time (else the overlapping turn). Approximate: an
  insertion during crosstalk can land in the wrong group, and whisper writing two
  overlapping speakers one after the other costs errors on both (as in the overall
  WER).
  ``missed_owner``/``missed_others``: share of the group's reference speech time (RTTM,
  no collar) not covered by any hypothesis segment, whoever it is labelled as: the
  VAD/whisper gate for quiet, far speech.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import httpx
import numpy as np


def _server_dir() -> Path:
    """--server-dir: run the eval against another checkout's server code (e.g. an older
    commit, for a baseline). Read before the imports below."""
    for i, a in enumerate(sys.argv):
        if a == "--server-dir" and i + 1 < len(sys.argv):
            return Path(sys.argv[i + 1]).resolve()
        if a.startswith("--server-dir="):
            return Path(a.split("=", 1)[1]).resolve()
    return Path(__file__).resolve().parents[1]


sys.path.insert(0, str(_server_dir()))

from convo_server import audio as audio_mod  # noqa: E402
from convo_server.audio import SAMPLE_RATE  # noqa: E402
from convo_server.config import Settings  # noqa: E402
from convo_server.pipeline import Pipeline  # noqa: E402
from convo_server.store import Store  # noqa: E402
from convo_server.vad import make_vad  # noqa: E402

try:
    from convo_server.cli import build_diarizer, build_transcriber
    from convo_server.diarize import enroll_voice
except ImportError:  # server code from before PR #24 (no prompt, one voiceprint each)
    from convo_server.diarize import Diarizer, EcapaEmbedder, enrollment_embedding, load_voices
    from convo_server.transcribe import WhisperServerTranscriber

    def build_transcriber(settings):  # type: ignore[no-redef]
        return WhisperServerTranscriber(settings.whisper_server_url, settings.language)

    def build_diarizer(settings, store):  # type: ignore[no-redef]
        if not settings.diarization:
            return None
        return Diarizer(EcapaEmbedder(DATA.parent / "models"), load_voices(store),
                        settings.speaker_match_threshold, settings.speaker_cluster_threshold)

    def enroll_voice(store, embedder, name, audio, is_owner=False):  # type: ignore[no-redef]
        store.put_speaker(name, enrollment_embedding(embedder, audio), is_owner)

HERE = Path(__file__).resolve().parent
DATA = HERE / "data" / "ami"
RESULTS = HERE / "results"
# AMI test-set meetings from three sites (Edinburgh, IDIAP, TNO).
MEETINGS = ["ES2004a", "IS1009a", "TS3003a"]
AUDIO_BASE = "https://groups.inf.ed.ac.uk/ami/AMICorpusMirror/amicorpus/{m}/audio/"
AUDIO_URL = AUDIO_BASE + "{m}.Mix-Headset.wav"
MIX_FILE = "{m}.Mix-Headset.wav"
ARRAY_FILE = "{m}.Array1-01.wav"  # table array 1, mic 1 (omni, ~1 m from each speaker)
HEADSET_FILE = "{m}.Headset-{ch}.wav"  # one speaker's own headset, AMI channel ch
AUDIO_MODES = ("headset", "array", "sim")
MEETINGS_XML = "corpusResources/meetings.xml"
SIM_DIR = DATA.parent / "sim"
ANNOT_URL = "https://groups.inf.ed.ac.uk/ami/AMICorpusAnnotations/ami_public_manual_1.6.2.zip"
RTTM_URL = ("https://raw.githubusercontent.com/pyannote/AMI-diarization-setup/main/"
            "only_words/rttms/test/{m}.rttm")
FILLERS = {"um", "uh", "uhm", "erm", "er", "hmm", "hm", "mm", "mmm", "mhm", "mmhmm", "uhhuh",
           "ah", "oh", "eh", "huh"}
T0 = 1_700_000_000.0  # fake wall clock for the session
CHUNK = 30.0


# ------------------------------------------------------------------- download
def _download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=600) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for block in r.iter_bytes(1 << 20):
                f.write(block)
    tmp.replace(dest)


def fetch(meetings: list[str], modes: tuple[str, ...] | list[str] = AUDIO_MODES) -> None:
    """Download what the --audio ``modes`` need; files already there are skipped."""
    zip_path = DATA / "ami_public_manual_1.6.2.zip"
    _download(ANNOT_URL, zip_path)
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if name == MEETINGS_XML or any(name.startswith(f"words/{m}.") for m in meetings):
                z.extract(name, DATA)
    for m in meetings:
        files = []
        if "headset" in modes:
            files.append(MIX_FILE.format(m=m))
        if "array" in modes:
            files.append(ARRAY_FILE.format(m=m))
        if "sim" in modes:
            files += [HEADSET_FILE.format(m=m, ch=s["channel"]) for s in meeting_speakers(m)]
        for name in files:
            _download(AUDIO_BASE.format(m=m) + name, DATA / name)
        _download(RTTM_URL.format(m=m), DATA / f"{m}.rttm")
    print(f"AMI data in {DATA}")


def meeting_speakers(meeting: str) -> list[dict]:
    """AMI speakers of ``meeting`` from corpusResources/meetings.xml: ``agent`` (the letter
    of the words file), ``speaker`` (global name, the id the RTTM uses) and ``channel``
    (the N of Headset-N.wav)."""
    root = ET.parse(DATA / MEETINGS_XML).getroot()
    for meet in root.iter():
        if meet.tag.endswith("meeting") and meet.get("observation") == meeting:
            return sorted(({"agent": s.get("nxt_agent"), "speaker": s.get("global_name"),
                            "channel": int(s.get("channel"))}
                           for s in meet if s.tag.endswith("speaker")),
                          key=lambda s: s["channel"])
    raise KeyError(f"{meeting} not in {MEETINGS_XML}")


# ------------------------------------------------------------------ reference
def normalize(text: str) -> str:
    text = text.lower().replace("-", " ")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    words = [w.strip("'") for w in text.split()]
    return " ".join(w for w in words if w and w.replace("'", "") not in FILLERS)


def reference_words(meeting: str, start: float, end: float) -> str:
    words: list[tuple[float, str]] = []
    for path in sorted((DATA / "words").glob(f"{meeting}.*.words.xml")):
        for w in ET.parse(path).getroot():
            if not w.tag.endswith("w") or w.get("punc") == "true" or not w.text:
                continue
            t = w.get("starttime")
            if t is None:
                continue
            if start <= float(t) < end:
                words.append((float(t), w.text))
    return " ".join(t for _, t in sorted(words))


def reference_words_by_speaker(meeting: str, start: float,
                               end: float) -> dict[str, list[tuple[float, float, str]]]:
    """Like ``reference_words``, per speaker (RTTM id): {speaker: [(start, end, word)]}."""
    agents = {s["agent"]: s["speaker"] for s in meeting_speakers(meeting)}
    out: dict[str, list[tuple[float, str]]] = {}
    for path in sorted((DATA / "words").glob(f"{meeting}.*.words.xml")):
        spk = agents.get(path.name.split(".")[1], path.name.split(".")[1])
        words = out.setdefault(spk, [])
        for w in ET.parse(path).getroot():
            if not w.tag.endswith("w") or w.get("punc") == "true" or not w.text:
                continue
            t = w.get("starttime")
            if t is not None and start <= float(t) < end:
                e = w.get("endtime")
                words.append((float(t), float(e) if e else float(t) + 0.3, w.text))
        words.sort()
    return out


def rttm_turns(meeting: str) -> list[tuple[float, float, str]]:
    turns = []
    for line in (DATA / f"{meeting}.rttm").read_text().splitlines():
        p = line.split()
        if p and p[0] == "SPEAKER":
            turns.append((float(p[3]), float(p[3]) + float(p[4]), p[7]))
    return turns


def annotation(turns, start: float = 0.0, end: float = float("inf")):
    from pyannote.core import Annotation, Segment

    ann = Annotation()
    for i, (a, b, spk) in enumerate(turns):
        a, b = max(a, start), min(b, end)
        if b > a:
            ann[Segment(a - start, b - start), i] = spk
    return ann


def solo_regions(turns, speaker: str, avoid: tuple[float, float]) -> list[tuple[float, float]]:
    """Stretches (>= 1 s) where only ``speaker`` talks, outside the ``avoid`` window."""
    others = sorted((a, b) for a, b, s in turns if s != speaker)
    out = []
    for a, b, s in turns:
        if s != speaker:
            continue
        pieces = [(a, b)]
        for oa, ob in [*others, avoid]:
            pieces = [q for x, y in pieces
                      for q in ((x, min(y, oa)), (max(x, ob), y)) if q[1] > q[0]]
        out += [(x, y) for x, y in pieces if y - x >= 1.0]
    return sorted(out)


# ---------------------------------------------------------------- audio stream
OWNER_DISTANCE = 0.2  # m, wearer's mouth to the chest mic
DEVICE_LEVEL_DBFS = -24.0  # owner's active speech level with the T-Watch's fixed 30 dB gain
LIMITER_CEILING_DBFS = -1.0  # firmware/recorder/main/audio_dsp.c
LIMITER_RELEASE_MS = 100.0
SIM_ROOM = (5.0, 4.0, 2.7)  # m
SIM_MIC = (1.5, 1.5, 1.3)  # chest height, off-centre so 3 m still fits inside the room
SIM_MOUTH_HEIGHT = 1.5  # the others (between seated and standing)
SIM_SEED = 0
SIM_VERSION = 1  # bump when the simulation changes, so cached WAVs are rebuilt


def active_level_dbfs(x: np.ndarray, frame: float = 0.03, floor_db: float = 30.0,
                      sample_rate: int = SAMPLE_RATE) -> float:
    """Active speech level in dBFS (RMS, full scale = 1.0), roughly like ITU-T P.56: the
    mean power of the 30 ms frames within ``floor_db`` of the loud (95th percentile)
    frames, so pauses don't pull it down. -inf for silence."""
    n = int(frame * sample_rate)
    if x.size < n:
        return float("-inf")
    e = np.mean(np.square(x[: x.size // n * n].reshape(-1, n), dtype=np.float64), axis=1)
    loud = float(np.percentile(e, 95))
    if loud <= 0:
        return float("-inf")
    return float(10 * np.log10(np.mean(e[e >= loud * 10 ** (-floor_db / 10)])))


def scale_to_level(x: np.ndarray, target_dbfs: float, mask: np.ndarray | None = None,
                   ) -> np.ndarray:
    """``x`` scaled so its active level (over the samples in ``mask``) is ``target_dbfs``."""
    level = active_level_dbfs(x if mask is None else x[mask])
    if not np.isfinite(level):
        return x.copy()
    return (x * 10 ** ((target_dbfs - level) / 20)).astype(np.float32)


def limiter(x: np.ndarray, ceiling_dbfs: float = LIMITER_CEILING_DBFS,
            release_ms: float = LIMITER_RELEASE_MS,
            sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """The firmware's peak limiter (audio_dsp.c: instant attack to the ceiling, exponential
    release), vectorised: 1 - envelope is a decaying running max of 1 - needed gain, i.e.
    r**i * cummax((1 - need[j]) / r**j), computed in the log domain."""
    ceiling = 10 ** (ceiling_dbfs / 20)
    mag = np.abs(x.astype(np.float64))
    need = np.minimum(1.0, ceiling / np.maximum(mag, 1e-12))
    if need.min() >= 1.0:
        return x.astype(np.float32)
    k = 1.0 / (sample_rate * release_ms / 1000.0)  # -log(release coefficient)
    i = np.arange(x.size, dtype=np.float64)
    with np.errstate(divide="ignore"):
        logd = np.maximum.accumulate(np.log(1.0 - need) + i * k) - i * k
    env = 1.0 - np.exp(logd)
    return (x * np.minimum(env, need)).astype(np.float32)


def device_gain(x: np.ndarray, owner_level_dbfs: float,
                target_dbfs: float = DEVICE_LEVEL_DBFS) -> tuple[np.ndarray, float]:
    """One fixed gain that puts the owner's speech at ``target_dbfs`` (like the firmware's
    fixed gain), then the firmware limiter so nothing clips. Returns (audio, gain dB)."""
    gain_db = target_dbfs - owner_level_dbfs
    return limiter(x * 10 ** (gain_db / 20)), gain_db


def speech_masks(turns, owner: str, n: int,
                 sample_rate: int = SAMPLE_RATE) -> dict[str, np.ndarray]:
    """Sample masks from the RTTM: ``owner``/``others`` where only one speaker of that group
    talks (no overlap), ``speech`` where anyone talks, and ``solo:<id>`` per speaker."""
    step = sample_rate // 100  # 10 ms grid
    m = -(-n // step)
    count = np.zeros(m, np.int16)
    per: dict[str, np.ndarray] = {}
    for a, b, spk in turns:
        ia, ib = max(0, int(a * 100)), min(m, int(np.ceil(b * 100)))
        if ib > ia:
            count[ia:ib] += 1
            per.setdefault(spk, np.zeros(m, bool))[ia:ib] = True
    solo = count == 1
    grid = {"speech": count > 0,
            "owner": solo & per.get(owner, np.zeros(m, bool)),
            "others": solo & ~per.get(owner, np.zeros(m, bool))}
    grid.update({f"solo:{s}": solo & v for s, v in per.items()})
    return {k: np.repeat(v, step)[:n] for k, v in grid.items()}


def stream_levels(audio: np.ndarray, turns, owner: str) -> dict[str, float]:
    masks = speech_masks(turns, owner, audio.size)
    return {f"{k}_dbfs": round(active_level_dbfs(audio[masks[k]]), 1)
            for k in ("speech", "owner", "others")}


def gate_to_turns(x: np.ndarray, turns, speaker: str, pad: float = 0.25,
                  ramp: float = 0.02, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """``x`` silenced outside ``speaker``'s turns (each widened by ``pad`` s), with linear
    ``ramp`` s fades so the gate doesn't click."""
    keep = np.zeros(x.size, np.float64)
    for a, b, s in turns:
        if s == speaker:
            keep[max(0, int((a - pad) * sample_rate)): max(0, int((b + pad) * sample_rate))] = 1
    w = max(1, int(ramp * sample_rate))
    c = np.concatenate([[0.0], np.cumsum(np.pad(keep, (w // 2, w - w // 2 - 1)))])
    return (x * ((c[w:] - c[:-w]) / w)).astype(np.float32)


def place_sources(n_others: int, distance: float, seed: int = SIM_SEED,
                  room=SIM_ROOM, mic=SIM_MIC, margin: float = 0.3,
                  ) -> tuple[np.ndarray, list[np.ndarray]]:
    """Owner's mouth 0.2 m above the chest mic; the others' mouths ``distance`` m from it
    (3-D), spread over the directions that keep them ``margin`` m inside the room, with a
    seeded jitter. Deterministic for a given seed."""
    mic = np.asarray(mic, float)
    owner = mic + [0.0, 0.0, OWNER_DISTANCE]
    dz = SIM_MOUTH_HEIGHT - mic[2]
    r = float(np.sqrt(max(distance ** 2 - dz ** 2, 0.0)))
    az = np.radians(np.arange(0, 360, 2.0))
    pts = np.stack([mic[0] + r * np.cos(az), mic[1] + r * np.sin(az),
                    np.full(az.size, SIM_MOUTH_HEIGHT)], axis=1)
    ok = np.all((pts >= margin) & (pts <= np.asarray(room) - margin), axis=1)
    feasible = pts[ok]
    if n_others and not feasible.size:
        raise ValueError(f"{distance} m does not fit in a {room} m room")
    rng = np.random.default_rng(seed)
    per = len(feasible) / max(n_others, 1)
    idx = [min(len(feasible) - 1, int((k + 0.5 + rng.uniform(-0.3, 0.3)) * per))
           for k in range(n_others)]
    return owner, [feasible[i] for i in idx]


def room_rirs(sources, rt60: float, room=SIM_ROOM, mic=SIM_MIC) -> list[np.ndarray]:
    """Image-source RIRs (pyroomacoustics) from each source to the mic, with the
    library's fractional-delay offset removed so the audio stays aligned with the RTTM."""
    import pyroomacoustics as pra

    e_abs, max_order = pra.inverse_sabine(rt60, list(room))
    shoebox = pra.ShoeBox(list(room), fs=SAMPLE_RATE, materials=pra.Material(e_abs),
                          max_order=max_order)
    for s in sources:
        shoebox.add_source(list(s))
    shoebox.add_microphone(np.asarray(mic, float))
    shoebox.compute_rir()
    skip = pra.constants.get("frac_delay_length") // 2
    return [np.asarray(h, np.float64)[skip:] for h in shoebox.rir[0]]


def pink_noise(n: int, seed: int = SIM_SEED) -> np.ndarray:
    """Stationary pink-ish room noise at unit RMS (Kellet's economy pinking filter)."""
    from scipy.signal import lfilter

    white = np.random.default_rng(seed).standard_normal(n)
    pink = lfilter([0.049922035, -0.095993537, 0.050612699, -0.004408786],
                   [1.0, -2.494956002, 2.017265875, -0.522189400], white)
    return (pink / (np.sqrt(np.mean(pink ** 2)) or 1.0)).astype(np.float32)


def sim_path(meeting: str, owner: str, distance: float, rt60: float, snr: float,
             mutes: tuple = (), dips_db: float = 0.0) -> Path:
    extra = ""
    if mutes:
        import hashlib

        extra += "-mute" + hashlib.sha1(repr(sorted(mutes)).encode()).hexdigest()[:8]
    if dips_db:
        extra += f"-dips{dips_db:g}"
    return SIM_DIR / (f"{meeting}-{owner}-d{distance:g}-rt{rt60:g}-snr{snr:g}"
                      f"-seed{SIM_SEED}-v{SIM_VERSION}{extra}.wav")


def owner_dips(n: int, max_db: float, seed: int = SIM_SEED, min_db: float = 6.0,
               ramp: float = 0.05) -> np.ndarray:
    """A gain envelope for the owner's voice (--owner-dips): a wrist mic turns away when
    the arm moves. Every 5-20 s the level drops by a random ``min_db``..``max_db`` dB for
    2-8 s (50 ms ramps). Deterministic for a given seed."""
    env = np.ones(n, np.float32)
    rng = np.random.default_rng(seed + 1)
    t = rng.uniform(5, 20)
    sr = SAMPLE_RATE
    r = int(ramp * sr)
    while t < n / sr:
        dur = rng.uniform(2, 8)
        g = 10 ** (-rng.uniform(min_db, max(min_db, max_db)) / 20)
        a, b = int(t * sr), min(n, int((t + dur) * sr))
        env[a:b] = g
        up = np.linspace(1, g, min(r, b - a), dtype=np.float32)
        env[a:a + up.size] = up
        down = np.linspace(g, 1, min(r, n - b), dtype=np.float32)
        env[b:b + down.size] = down
        t += dur + rng.uniform(5, 20)
    return env


def late_joiner_mutes(by_time: list[str], start: float, end: float,
                      keep: int = 2) -> tuple[tuple[str, float, float], ...]:
    """--late-joiners: everyone after the ``keep`` most active speakers is silent in the
    first half of the window (they join halfway)."""
    mid = (start + end) / 2
    return tuple((spk, start, mid) for spk in by_time[keep:])


def cut_turns(turns, mutes) -> list[tuple[float, float, str]]:
    """Reference turns without the muted stretches (a muted speaker said nothing then)."""
    out = []
    for a, b, s in turns:
        parts = [(a, b)]
        for spk, x, y in mutes:
            if spk != s:
                continue
            parts = [p for c, d in parts
                     for p in ((c, min(d, x)), (max(c, y), d)) if p[1] > p[0]]
        out += [(c, d, s) for c, d in parts]
    return sorted(out)


def cut_words(ref_words: dict[str, list[tuple[float, float, str]]], mutes
              ) -> dict[str, list[tuple[float, float, str]]]:
    return {spk: [w for w in ws if not any(m == spk and x <= w[0] < y for m, x, y in mutes)]
            for spk, ws in ref_words.items()}


def simulate(meeting: str, owner: str, distance: float = 2.0, rt60: float = 0.5,
             snr: float = 30.0, mutes: tuple = (), dips_db: float = 0.0) -> np.ndarray:
    """The --audio sim stream: ``owner`` close to a chest mic, everyone else at
    ``distance`` m, in a reverberant room with noise, as a fixed-gain device records it.

    1. Each speaker's own headset track (Headset-<channel>.wav) is gated to their RTTM
       turns (+-0.25 s, 20 ms ramps) and scaled to the same active speech level over
       their solo turns, so distance is the only level difference. The gate matters:
       the owner's headset hears the others only ~10-15 dB down, as loud as their
       simulated far-field voices, so ungated bleed would put the others next to the
       mic. Inside the turns (overlapping speech, short back-channels) a little bleed of
       the other speakers stays and is convolved with the wrong position; acceptable for
       a level/pickup test. Some AMI headsets are also noisy (ES2004a ch2: noise ~7 dB
       under its speech); that noise travels with the voice.
    2. Each track is convolved with its RIR (``place_sources``, ``room_rirs``, RT60
       ``rt60``) and the tracks are summed.
    3. Stationary pink noise at ``snr`` dB below the owner's (reverberant) speech level.
    4. ``device_gain``: one fixed gain for the owner at -24 dBFS, firmware limiter.
    ``mutes`` ((speaker, start, end) in seconds) silence a speaker's track there
    (--late-joiners); ``dips_db`` turns the owner's voice down in stretches
    (--owner-dips, ``owner_dips``).
    Deterministic (fixed seed); cached in eval/data/sim/ by meeting and parameters.
    """
    path = sim_path(meeting, owner, distance, rt60, snr, mutes, dips_db)
    if path.exists():
        return audio_mod.decode(path.read_bytes(), "wav")
    from scipy.signal import oaconvolve

    turns = rttm_turns(meeting)
    tracks: dict[str, np.ndarray] = {}
    for s in meeting_speakers(meeting):
        f = DATA / HEADSET_FILE.format(m=meeting, ch=s["channel"])
        if not f.exists():
            raise SystemExit(f"{f.name} missing: run `fetch --audio sim` first")
        tracks[s["speaker"]] = audio_mod.decode(f.read_bytes(), "wav")
    if owner not in tracks:
        raise SystemExit(f"{meeting}: no headset for the owner {owner}")
    n = max(t.size for t in tracks.values())
    masks = speech_masks(turns, owner, n)
    tracks = {spk: np.pad(t, (0, n - t.size)) for spk, t in tracks.items()}
    # channel check (meetings.xml channel -> speaker): a headset is loudest while its own
    # wearer talks (compare one track across turns: tracks differ in gain and noise)
    print(f"  {meeting}: headset check (track level in its speaker's solo turns vs in the "
          f"loudest other speaker's):")
    for spk, t in tracks.items():
        lv = {o: active_level_dbfs(t[masks[f"solo:{o}"]]) for o in tracks
              if f"solo:{o}" in masks}
        a = lv.get(spk, float("-inf"))
        b = max((v for o, v in lv.items() if o != spk), default=float("-inf"))
        flag = "" if a > b else "  <-- channel mapping suspect"  # IS1009a bleeds: ~2.5 dB
        print(f"    {spk}: {a:.1f} vs {b:.1f} dBFS{flag}")
    for spk, t in tracks.items():
        own = masks.get(f"solo:{spk}", np.zeros(n, bool))
        tracks[spk] = scale_to_level(gate_to_turns(t, turns, spk), -26.0,
                                     own if own.any() else None)
    for spk, a, b in mutes:
        if spk in tracks:
            tracks[spk][int(a * SAMPLE_RATE): int(b * SAMPLE_RATE)] = 0.0
    if dips_db:
        tracks[owner] = tracks[owner] * owner_dips(n, dips_db)
    others = sorted(s for s in tracks if s != owner)
    owner_pos, other_pos = place_sources(len(others), distance)
    rirs = room_rirs([owner_pos, *other_pos], rt60)
    mix = np.zeros(n, np.float64)
    owner_part = None
    for spk, h in zip([owner, *others], rirs, strict=True):
        y = oaconvolve(tracks[spk].astype(np.float64), h)[:n]
        if spk == owner:
            owner_part = y
        mix += y
    owner_db = active_level_dbfs(owner_part[masks["owner"]].astype(np.float32))
    mix += pink_noise(n) * 10 ** ((owner_db - snr) / 20)
    out, gain_db = device_gain(mix.astype(np.float32), owner_db)
    over = np.abs(mix) * 10 ** (gain_db / 20) > 10 ** (LIMITER_CEILING_DBFS / 20)
    print(f"  {meeting}: sim {distance:g} m, RT60 {rt60:g} s, SNR {snr:g} dB: device gain "
          f"{gain_db:+.1f} dB, {over.mean():.3%} of samples over the limiter ceiling")
    SIM_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(audio_mod.to_wav_bytes(out))
    tmp.replace(path)
    return audio_mod.decode(path.read_bytes(), "wav")


def opus_roundtrip(audio: np.ndarray, kbps: float) -> np.ndarray:
    """Encode with libopus like our firmware (firmware/recorder/main/opus_enc.c: 16 kHz
    mono, VoIP, 20 ms frames, unconstrained VBR, complexity 1) and decode back to 16 kHz
    float, the same length as the input."""
    enc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "f32le", "-ar", str(SAMPLE_RATE),
         "-ac", "1", "-i", "pipe:0", "-c:a", "libopus", "-b:a", f"{kbps:g}k", "-vbr", "on",
         "-application", "voip", "-compression_level", "1", "-frame_duration", "20",
         "-f", "ogg", "pipe:1"],
        input=np.ascontiguousarray(audio, "<f4").tobytes(), capture_output=True, check=True)
    dec = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-f", "f32le",
         "-ar", str(SAMPLE_RATE), "-ac", "1", "pipe:1"],
        input=enc.stdout, capture_output=True, check=True)
    out = np.frombuffer(dec.stdout, "<f4").astype(np.float32)
    if out.size < audio.size:
        out = np.pad(out, (0, audio.size - out.size))
    return out[: audio.size]


def load_stream(meeting: str, args: argparse.Namespace, turns, owner: str,
                mutes: tuple = ()) -> np.ndarray:
    """The whole decoded meeting as the device would deliver it (--audio, --opus-kbps);
    the window and the enrollment audio are both cut from it."""
    mode = getattr(args, "audio", "headset")
    if mode == "sim":
        full = simulate(meeting, owner, args.distance, args.rt60, args.snr, mutes,
                        getattr(args, "owner_dips", 0.0) or 0.0)
    else:
        f = DATA / (ARRAY_FILE if mode == "array" else MIX_FILE).format(m=meeting)
        if not f.exists():
            raise SystemExit(f"{f.name} missing: run `fetch --audio {mode}` first")
        full = audio_mod.decode(f.read_bytes(), "wav")
    if args.owner_boost_db:
        full = boost_speaker(full, turns, owner, args.owner_boost_db)
    level = getattr(args, "level", None)
    if level is not None:
        # one fixed device gain (before the encoder, like the firmware): all speech at
        # `level` dBFS, then the firmware limiter
        mask = speech_masks(turns, owner, full.size)["speech"]
        full = limiter(full * 10 ** ((level - active_level_dbfs(full[mask])) / 20))
    if getattr(args, "opus_kbps", 0):
        full = opus_roundtrip(full, args.opus_kbps)
    return full


# ------------------------------------------------------------ owner vs others
def assign_words(segs, turns, start: float = 0.0,
                 ref_words: dict | None = None) -> list[tuple[str | None, str]]:
    """Hypothesis words with the reference speaker each probably belongs to. ``segs`` are
    (start, end, label, text) relative to the window at ``start``; ``turns`` absolute.
    Word times are spread over the segment by character count; each word goes to the
    speaker of the reference word (``ref_words``, as from ``reference_words_by_speaker``)
    that matches its time best (intersection over union), else whose turn overlaps it
    most, else whose turn overlaps the segment most, else the nearest turn (see the
    module docstring: an approximation). Reference words before turns: a long RTTM turn
    often contains someone else's short "okay, thank you", and the turn alone can't tell
    which of the two said it."""
    spans = [(a, b, spk) for spk, ws in (ref_words or {}).items() for a, b, w in ws
             if normalize(w)]

    def closest_word(a: float, b: float) -> str | None:
        top, who = 0.0, None
        for x, y, s in spans:
            o = min(b, y) - max(a, x)
            if o > 0 and o / (max(b, y) - min(a, x)) > top:
                top, who = o / (max(b, y) - min(a, x)), s
        return who

    def best(a: float, b: float) -> str | None:
        ov: dict[str, float] = {}
        for x, y, s in turns:
            o = min(b, y) - max(a, x)
            if o > 0:
                ov[s] = ov.get(s, 0.0) + o
        return max(ov, key=lambda s: ov[s]) if ov else None

    out: list[tuple[str | None, str]] = []
    for a, b, _, text in segs:
        words = normalize(text).split()
        if not words:
            continue
        a, b = a + start, b + start
        seg_spk = best(a, b)
        if seg_spk is None and turns:
            mid = (a + b) / 2
            seg_spk = min(turns, key=lambda t: max(t[0] - mid, mid - t[1], 0.0))[2]
        edges = np.cumsum([0] + [len(w) + 1 for w in words])
        edges = a + (b - a) * edges / edges[-1]
        for w, wa, wb in zip(words, edges[:-1], edges[1:], strict=True):
            out.append((closest_word(wa, wb) or best(wa, wb) or seg_spk, w))
    return out


def split_wer(ref_by_speaker: dict[str, list[tuple[float, float, str]]],
              hyp_words: list[tuple[str | None, str]], owner: str) -> dict:
    """WER for the owner and for everyone else, with error counts for pooling.

    One alignment of the whole hypothesis against the whole (time-ordered) reference, the
    same one the overall WER uses; each error goes to a speaker: a substitution or
    deletion to the speaker of the reference word, an insertion to the speaker
    ``assign_words`` gave the hypothesis word. So owner + others errors = all errors, and
    a perfect transcript scores 0 for both whatever the segment boundaries."""
    import jiwer

    ref: list[tuple[str, str]] = []  # (word, speaker) in time order
    # ordered like ``reference_words`` (start time, then text), so the alignment is the same
    for _, w, spk in sorted((a, w, spk) for spk, ws in ref_by_speaker.items()
                            for a, _, w in ws):
        ref += [(x, spk) for x in normalize(w).split()]
    words = {"owner": 0, "others": 0}
    errs = {"owner": 0, "others": 0}

    def group(spk: str | None) -> str:
        return "owner" if spk == owner else "others"

    for _, spk in ref:
        words[group(spk)] += 1
    if ref:
        m = jiwer.process_words(" ".join(w for w, _ in ref), " ".join(w for _, w in hyp_words)
                                or "")
        for c in m.alignments[0]:
            if c.type in ("substitute", "delete"):
                for i in range(c.ref_start_idx, c.ref_end_idx):
                    errs[group(ref[i][1])] += 1
            elif c.type == "insert":
                for i in range(c.hyp_start_idx, c.hyp_end_idx):
                    errs[group(hyp_words[i][0])] += 1
    else:
        for spk, _ in hyp_words:
            errs[group(spk)] += 1
    out: dict = {}
    for g in ("owner", "others"):
        out.update({f"{g}_ref_words": words[g], f"{g}_errors": errs[g],
                    f"wer_{g}": errs[g] / words[g] if words[g] else None})
    return out


def missed_by_group(turns, segs, owner: str, start: float, end: float) -> dict:
    """Share of the owner's / others' reference speech (RTTM, 10 ms grid, no collar) that
    no hypothesis segment covers, whoever it is labelled as."""
    m = int(round((end - start) * 100))
    covered = np.zeros(m, bool)
    for a, b, *_ in segs:
        covered[max(0, int(a * 100)): max(0, int(np.ceil(b * 100)))] = True
    out: dict = {}
    for group, mine in (("owner", lambda s: s == owner), ("others", lambda s: s != owner)):
        speech = np.zeros(m, bool)
        for a, b, s in turns:
            if mine(s):
                speech[max(0, int((a - start) * 100)): max(0, int(np.ceil((b - start)
                                                                           * 100)))] = True
        total, missed = speech.sum() / 100, (speech & ~covered).sum() / 100
        out.update({f"{group}_speech_s": float(total), f"{group}_missed_s": float(missed),
                    f"missed_{group}": float(missed / total) if total else None})
    return out


# ------------------------------------------------------------------------ run
# Enrolled AMI speakers get these fictional names (AMI ids like "MEE006" are not words,
# so they would be a meaningless whisper prompt). Results map them back to the ids.
NAMES = ["Maria", "Tom", "Priya", "Jonas"]


def boost_speaker(audio: np.ndarray, turns, speaker: str, db: float) -> np.ndarray:
    """Make ``speaker`` ``db`` louder wherever they talk alone (a wearer is closer to the
    mic than the people around them), then scale the whole recording back below 0 dBFS."""
    out = audio.copy()
    gain = 10 ** (db / 20)
    for a, b in solo_regions(turns, speaker, (0.0, 0.0)):
        out[int(a * SAMPLE_RATE): int(b * SAMPLE_RATE)] *= gain
    peak = float(np.max(np.abs(out))) or 1.0
    return out / peak * 0.9 if peak > 0.9 else out


class CachedTranscriber:
    """--cache: reuse whisper results for the same audio, prompt and settings, so runs
    that only change the diarizer skip transcription (eval/data/cache/, gitignored)."""

    def __init__(self, inner, settings: Settings) -> None:
        self.inner = inner
        self.key = json.dumps([type(inner).__name__, settings.whisper_server_url,
                               settings.whisper_model, settings.language,
                               getattr(settings, "transcriber", "whisper"),
                               str(getattr(settings, "parakeet_model_dir", None)),
                               getattr(settings, "parakeet_redecode_holes", False),
                               getattr(inner, "temperature_inc", None),
                               "words"])  # entries keep word times (older ones didn't)
        self.dir = DATA.parent / "cache"
        self.dir.mkdir(parents=True, exist_ok=True)

    def transcribe(self, audio: np.ndarray, prompt: str = ""):
        import hashlib

        from convo_server.transcribe import TranscriptPiece

        h = hashlib.sha1(self.key.encode() + prompt.encode()
                         + np.ascontiguousarray(audio, np.float32).tobytes()).hexdigest()
        path = self.dir / f"{h}.json"
        if path.exists():
            return [TranscriptPiece(p[0], p[1], p[2], tuple(tuple(w) for w in p[3]))
                    for p in json.loads(path.read_text())]
        try:
            pieces = self.inner.transcribe(audio, prompt)
        except TypeError:  # pre-#24 transcriber: no prompt argument
            pieces = self.inner.transcribe(audio)
        path.write_text(json.dumps([[p.start, p.end, p.text, [list(w) for w in p.words]]
                                    for p in pieces]))
        return pieces


class CachedSegmenter:
    """--cache for pyannote: reuse its turns for the same audio and pyannote settings, so
    runs that only change naming (owner hint, line check, thresholds) skip pyannote."""

    def __init__(self, inner, settings: Settings) -> None:
        self.inner = inner
        key = ["pyannote", getattr(settings, "pyannote_model", ""),
               getattr(settings, "pyannote_cluster_threshold", None),
               getattr(settings, "pyannote_min_speakers", 0),
               getattr(settings, "pyannote_max_speakers", 0)]
        vbx = [getattr(settings, "pyannote_vbx_fa", None),
               getattr(settings, "pyannote_vbx_fb", None)]
        if any(v is not None for v in vbx):  # only when set: older entries still hit
            key += ["vbx", *vbx]
        self.key = json.dumps(key)
        self.dir = DATA.parent / "cache"
        self.dir.mkdir(parents=True, exist_ok=True)

    def __call__(self, audio: np.ndarray):
        import hashlib

        h = hashlib.sha1(self.key.encode()
                         + np.ascontiguousarray(audio, np.float32).tobytes()).hexdigest()
        path = self.dir / f"pyannote-{h}.json"
        if path.exists():
            return [tuple(t) for t in json.loads(path.read_text())]
        turns = self.inner(audio)
        path.write_text(json.dumps([list(t) for t in turns]))
        return turns


def _process(pipeline: Pipeline, sid: str, audio: np.ndarray, t0: float) -> None:
    step = int(CHUNK * SAMPLE_RATE)
    for i in range(0, audio.size, step):
        pipeline.ingest_chunk(device="eval", session_id=sid, sequence=i // step,
                              started_at=t0 + i / SAMPLE_RATE, codec="wav",
                              data=audio_mod.to_wav_bytes(audio[i: i + step]))
    pipeline.end_session(sid)
    pipeline.process_session(sid)
    sess = pipeline.store.get_session(sid)
    if sess and sess["status"] != "done":
        raise RuntimeError(f"{sid}: session {sess['status']}: {sess['error']}")


def _enroll_audio(full, turns, spk, avoid, seconds) -> tuple[np.ndarray | None, float]:
    parts, total = [], 0.0
    for a, b in solo_regions(turns, spk, avoid):
        if total >= seconds:
            break
        parts.append(full[int(a * SAMPLE_RATE): int(b * SAMPLE_RATE)])
        total += b - a
    return (np.concatenate(parts) if parts else None), total


def _name_by_overlap(store: Store, sid: str, t0: float, turns, spk: str, name: str,
                     offset: float, threshold: float) -> int:
    """What a user does with name_speaker: name the label that covers most of ``spk``'s
    speech in session ``sid``. Returns how many lines elsewhere were relabelled."""
    from convo_server.speakers import relabel_matches

    segs = store.session_segments(sid)
    cover: dict[tuple[str, str], dict[str, float]] = {}
    for sg in segs:
        if not (sg.conversation_id and sg.speaker.startswith("Speaker ")):
            continue
        a, b = sg.started_at - t0 + offset, sg.ended_at - t0 + offset
        who = cover.setdefault((sg.conversation_id, sg.speaker), {})
        for x, y, s in turns:
            o = max(0.0, min(b, y) - max(a, x))
            if o:
                who[s] = who.get(s, 0.0) + o
    # like a user who listens to a few lines: a label where this person is the main voice
    mine = {k: w[spk] for k, w in cover.items()
            if w.get(spk) and max(w, key=lambda s: w[s]) == spk}
    if not mine:
        return 0
    conv, label = max(mine, key=lambda k: mine[k])
    _, voice = store.rename_speaker(conv, label, name)
    if voice is None:
        return 0
    store.add_voice_sample(name, voice)
    return sum(r.lines for r in relabel_matches(store, name, threshold))


def run_meeting(meeting: str, args: argparse.Namespace, base: dict) -> dict:
    from pyannote.metrics.diarization import DiarizationErrorRate

    start, end = args.offset, args.offset + args.seconds
    turns = rttm_turns(meeting)
    ref = annotation(turns, start, end)
    by_time = sorted(ref.labels(), key=lambda s: -ref.label_duration(s))
    owner = by_time[0]
    mutes = (late_joiner_mutes(by_time, start, end) if getattr(args, "late_joiners", False)
             else ())
    full = load_stream(meeting, args, turns, owner, mutes)
    if mutes:  # the reference: the late joiners said nothing before they joined
        turns = cut_turns(turns, mutes)
        ref = annotation(turns, start, end)
    levels = stream_levels(full, turns, owner)
    print(f"  {meeting}: {getattr(args, 'audio', 'headset')} active speech level "
          f"{levels['speech_dbfs']} dBFS (owner {owner} {levels['owner_dbfs']}, others "
          f"{levels['others_dbfs']})", flush=True)
    window = full[int(start * SAMPLE_RATE): int(end * SAMPLE_RATE)]
    relabeled = 0

    with tempfile.TemporaryDirectory(prefix="convo-eval-") as tmp:
        # an older --server-dir lacks newer settings; say which ones are ignored
        dropped = sorted(set(base) - set(Settings.model_fields))
        if dropped:
            print(f"  settings not in this server code, ignored: {', '.join(dropped)}")
        settings = Settings(**{k: v for k, v in {**base, "data_dir": Path(tmp)}.items()
                               if k not in dropped})
        store = Store(settings.db_path)
        diarizer = build_diarizer(settings, store)
        if args.cache and diarizer is not None and hasattr(diarizer, "segmenter"):
            diarizer.segmenter = CachedSegmenter(diarizer.segmenter, settings)
        label_scores: dict[str, float] = {}  # label -> its voice's match score (last run)
        if diarizer is not None and hasattr(diarizer, "name_clusters"):
            _wrap_name_clusters(diarizer, label_scores)
        named: dict[str, str] = {}  # fictional name -> AMI speaker id
        targets = by_time[: args.enroll] if diarizer is not None else []
        if not args.relabel:
            for i, spk in enumerate(targets):
                pcm, total = _enroll_audio(full, turns, spk, (start, end), args.enroll_seconds)
                if pcm is None or total < 10:
                    print(f"  {meeting}: only {total:.0f}s solo speech for {spk}, skipped")
                    continue
                enroll_voice(store, diarizer.embedder, NAMES[i], pcm, is_owner=(i == 0))
                named[NAMES[i]] = spk
        transcriber = build_transcriber(settings)
        if args.cache and transcriber is not None:
            transcriber = CachedTranscriber(transcriber, settings)
        pipeline = Pipeline(settings, store, make_vad(settings.vad_backend, settings), transcriber,
                            diarizer)
        sid = f"eval-{meeting}"
        t = time.time()
        _process(pipeline, sid, window, T0)
        elapsed = time.time() - t
        if args.relabel:
            # name the speakers in a later recording (the window before this one) and let
            # relabelling rename them here
            a = max(0.0, start - args.seconds)
            t1 = T0 + 86_400
            _process(pipeline, sid + "-later", full[int(a * SAMPLE_RATE): int(start
                                                                              * SAMPLE_RATE)],
                     t1)
            for i, spk in enumerate(targets):
                relabeled += _name_by_overlap(store, sid + "-later", t1, turns, spk, NAMES[i],
                                              a, settings.speaker_relabel_threshold)
                named[NAMES[i]] = spk
        segs = [(sg.started_at - T0, sg.ended_at - T0, named.get(sg.speaker, sg.speaker),
                 sg.text) for sg in store.session_segments(sid)]
        store.close()

    import jiwer

    ref_words = cut_words(reference_words_by_speaker(meeting, start, end), mutes)
    ref_text = (normalize(" ".join(w for _, w in sorted((t, w) for ws in ref_words.values()
                                                         for t, _, w in ws)))
                if mutes else normalize(reference_words(meeting, start, end)))
    hyp_text = normalize(" ".join(sg[3] for sg in segs))
    m = jiwer.process_words(ref_text, hyp_text)
    hyp = annotation([(a, b, spk) for a, b, spk, _ in segs])
    der = DiarizationErrorRate(collar=0.25)
    from pyannote.core import Segment as PSeg
    from pyannote.core import Timeline

    d = der(ref, hyp, uem=Timeline([PSeg(0, args.seconds)]), detailed=True)
    enrolled = list(named.values())
    out = {
        "meeting": meeting, "window": [start, end], "seconds_to_process": round(elapsed, 1),
        "ref_words": len(ref_text.split()), "wer": m.wer, "substitutions": m.substitutions,
        "deletions": m.deletions, "insertions": m.insertions,
        "der": d["diarization error rate"], "total": d["total"],
        "missed": d["missed detection"], "false_alarm": d["false alarm"],
        "confusion": d["confusion"], "ref_speakers": len(ref.labels()),
        "hyp_speakers": len(hyp.labels()), "hyp_labels": sorted(hyp.labels()),
        "enrolled": enrolled, "lines": len(segs), "relabeled_lines": relabeled,
        "owner": owner, "levels": levels,
        "hypothesis": [[round(a, 2), round(b, 2), spk, text] for a, b, spk, text in segs],
        # the enrolled-voice score behind each name (eval names: Maria is the owner, ...)
        "label_scores": {named.get(k, k): round(v, 3) for k, v in label_scores.items()},
    }
    out.update(_naming(turns, start, end, segs, enrolled))
    if mutes:
        out["late_joiners"] = [spk for spk, _, _ in mutes]
    out.update(split_wer(ref_words, assign_words(segs, turns, start, ref_words), owner))
    out.update(missed_by_group(turns, segs, owner, start, end))
    return out


def _wrap_name_clusters(diarizer, scores: dict[str, float]) -> None:
    """Record, for each name the diarizer gives, how close that cluster's voice was to
    the named person's (cosine score; "Speaker N" labels get their best score too)."""
    inner = diarizer.name_clusters

    def name_clusters(centroids, *rest):
        names, unnamed = inner(centroids, *rest)
        scores.clear()
        for lab, c in zip(names, centroids, strict=True):
            sims = [v.score(c) for v in diarizer.voices if v.name == lab.name]
            best = [v.score(c) for v in diarizer.voices]
            scores[lab.name] = float(sims[0] if sims else max(best, default=0.0))
        return names, unnamed

    diarizer.name_clusters = name_clusters


def _naming(turns, start, end, segs, enrolled) -> dict:
    """Frame-level (0.1 s) check of enrolled names on single-speaker reference frames.

    ``enrolled[0]`` is the owner. ``owner_precision``: of the frames labelled as the
    owner, the share the owner said (else someone's words would be filed as the
    owner's). ``owner_recall``: of the owner's frames with a line over them, the share
    labelled as the owner.
    """
    if not enrolled:
        return {}
    step = 0.1
    frames = np.arange(start, end, step)
    right = wrong = enrolled_n = n = 0
    owner = enrolled[0]
    as_owner = owner_true = owner_frames = 0
    stranger_frames = stranger_named = 0
    seg_t = [(a + start, b + start, spk) for a, b, spk, _ in segs]
    for t in frames:
        refs = {s for a, b, s in turns if a <= t < b}
        if len(refs) != 1:
            continue
        (spk,) = refs
        hyp = next((s for a, b, s in seg_t if a <= t < b), None)
        if hyp is None:
            continue
        n += 1
        if spk in enrolled:
            enrolled_n += 1
            right += hyp == spk
        if hyp in enrolled and hyp != spk:
            wrong += 1
        if hyp == owner:
            as_owner += 1
            owner_true += spk == owner
        owner_frames += spk == owner
        if spk not in enrolled:
            stranger_frames += 1
            stranger_named += hyp in enrolled
    return {"named_right": right / enrolled_n if enrolled_n else None,
            "named_wrong": wrong / n if n else None, "named_frames": n,
            "owner_precision": owner_true / as_owner if as_owner else None,
            "owner_recall": owner_true / owner_frames if owner_frames else None,
            "owner_labelled_frames": as_owner, "owner_frames": owner_frames,
            "owner_true_frames": owner_true,
            "unenrolled_named": stranger_named / stranger_frames if stranger_frames else None,
            "unenrolled_frames": stranger_frames, "unenrolled_named_frames": stranger_named}


def pooled(rows: list[dict]) -> dict:
    words = sum(r["ref_words"] for r in rows)
    errs = sum(r["substitutions"] + r["deletions"] + r["insertions"] for r in rows)
    total = sum(r["total"] for r in rows)
    out = {"wer": errs / words if words else None,
           "der": sum(r["missed"] + r["false_alarm"] + r["confusion"] for r in rows) / total,
           "missed": sum(r["missed"] for r in rows) / total,
           "false_alarm": sum(r["false_alarm"] for r in rows) / total,
           "confusion": sum(r["confusion"] for r in rows) / total,
           "seconds_to_process": round(sum(r["seconds_to_process"] for r in rows), 1)}
    for g in ("owner", "others"):
        if all(f"{g}_ref_words" in r for r in rows):
            n = sum(r[f"{g}_ref_words"] for r in rows)
            out[f"wer_{g}"] = sum(r[f"{g}_errors"] for r in rows) / n if n else None
            t = sum(r[f"{g}_speech_s"] for r in rows)
            out[f"missed_{g}"] = sum(r[f"{g}_missed_s"] for r in rows) / t if t else None
    named = [r for r in rows if r.get("named_right") is not None]
    if named:
        out["named_right"] = float(np.mean([r["named_right"] for r in named]))
        out["named_wrong"] = float(np.mean([r["named_wrong"] for r in named]))
    if all("owner_labelled_frames" in r for r in rows) and rows:
        labelled = sum(r["owner_labelled_frames"] for r in rows)
        frames = sum(r["owner_frames"] for r in rows)
        true = sum(r["owner_true_frames"] for r in rows)
        out["owner_precision"] = true / labelled if labelled else None
        out["owner_recall"] = true / frames if frames else None
    if all("unenrolled_frames" in r for r in rows) and rows:
        n = sum(r["unenrolled_frames"] for r in rows)
        out["unenrolled_named"] = (sum(r["unenrolled_named_frames"] for r in rows) / n
                                   if n else None)
    return out


def fmt(x) -> str:
    return "—" if x is None else f"{100 * x:.1f}%"


def audio_desc(res: dict) -> str:
    """The audio a run used; results from before --audio were headset, no Opus."""
    a = res.get("audio") or {"mode": "headset"}
    out = a["mode"]
    if a["mode"] == "sim":
        out += f" {a['distance']:g} m, RT60 {a['rt60']:g} s, SNR {a['snr']:g} dB"
        if a.get("late_joiners"):
            out += ", late joiners"
        if a.get("owner_dips"):
            out += f", owner dips up to {a['owner_dips']:g} dB"
    if a.get("level") is not None:
        out += f", speech at {a['level']:g} dBFS"
    if a.get("opus_kbps"):
        out += f", opus {a['opus_kbps']:g} kbps"
    return out


def table(results: dict[str, dict]) -> str:
    cols = ["wer", "wer_owner", "wer_others", "der", "confusion", "missed", "missed_others",
            "false_alarm", "named_right", "named_wrong", "owner_precision", "owner_recall",
            "unenrolled_named"]
    lines = ["| run | meeting | " + " | ".join(cols) + " | speakers (ref/hyp) | time |",
             "|---" * (len(cols) + 4) + "|"]
    for label, res in results.items():
        for r in res["meetings"]:
            lines.append(f"| {label} | {r['meeting']} | "
                         + " | ".join(fmt(r.get(c)) for c in cols)
                         + f" | {r['ref_speakers']}/{r['hyp_speakers']}"
                         f" | {r['seconds_to_process']}s |")
        p = res["pooled"]
        lines.append(f"| **{label}** | all | " + " | ".join(fmt(p.get(c)) for c in cols)
                     + f" | | {p['seconds_to_process']}s |")
    lines.append("")
    for label, res in results.items():
        lines.append(f"- {label}: {audio_desc(res)}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--meetings", nargs="+", default=MEETINGS)
    f.add_argument("--audio", nargs="+", choices=AUDIO_MODES, default=list(AUDIO_MODES),
                   help="download only what these --audio modes need (default: all)")
    r = sub.add_parser("run")
    r.add_argument("--label", required=True)
    r.add_argument("--server-dir", help="server/ of another checkout to evaluate "
                   "(default: this one)")
    r.add_argument("--meetings", nargs="+", default=MEETINGS)
    r.add_argument("--offset", type=float, default=300.0, help="window start (s)")
    r.add_argument("--seconds", type=float, default=300.0, help="window length (s)")
    r.add_argument("--whisper-url", help="whisper-server URL (else CONVO_WHISPER_*)")
    r.add_argument("--diarizer", help="ecapa, pyannote or none (else CONVO_DIARIZER)")
    r.add_argument("--prompt", help="CONVO_WHISPER_PROMPT override")
    r.add_argument("--enroll", type=int, default=0, help="enroll the N most active speakers")
    r.add_argument("--enroll-seconds", type=float, default=60.0)
    r.add_argument("--relabel", action="store_true",
                   help="don't enroll; process the window, then name the --enroll speakers "
                        "in the window before it and measure the relabelled window")
    r.add_argument("--cache", action="store_true",
                   help="reuse whisper results of earlier runs (same audio and prompt)")
    r.add_argument("--owner-boost-db", type=float, default=0.0,
                   help="make the most active speaker this much louder (a wearer)")
    r.add_argument("--audio", choices=AUDIO_MODES, default="headset",
                   help="headset mix (close-talk), table array mic (~1 m) or simulated "
                        "room at --distance (see the module docstring)")
    r.add_argument("--distance", type=float, default=2.0,
                   help="sim: the others' distance from the mic (m)")
    r.add_argument("--rt60", type=float, default=0.5, help="sim: reverberation time (s)")
    r.add_argument("--late-joiners", action="store_true",
                   help="sim: everyone after the two most active speakers is silent in the "
                        "first half of the window (people joining a conversation)")
    r.add_argument("--owner-dips", type=float, default=0.0, metavar="DB",
                   help="sim: the owner's voice drops by 6..DB dB for 2-8 s every 5-20 s "
                        "(a wrist mic turning away as the arm moves)")
    r.add_argument("--snr", type=float, default=30.0,
                   help="sim: owner's speech level over the room noise (dB)")
    r.add_argument("--level", type=float, default=None,
                   help="headset/array: one fixed gain so all speech sits at this dBFS "
                        "(a device's fixed gain; e.g. -34 = others at ~1 m on the T-Watch)")
    r.add_argument("--opus-kbps", "--opus", type=float, default=0.0, dest="opus_kbps",
                   help="round-trip the stream through libopus at this bitrate, like the "
                        "firmware (0 = off)")
    r.add_argument("--mic", choices=("Mix-Headset", "Array1-01"),
                   help="older spelling of --audio: Mix-Headset = headset, "
                        "Array1-01 = array")
    r.add_argument("--set", nargs="*", default=[], metavar="FIELD=VALUE",
                   help="extra Settings overrides, e.g. speaker_cluster_threshold=0.5")
    c = sub.add_parser("compare")
    c.add_argument("labels", nargs="+")
    args = ap.parse_args()
    if getattr(args, "mic", None):
        args.audio = "array" if args.mic == "Array1-01" else "headset"

    if args.cmd == "run" and (args.late_joiners or args.owner_dips) and args.audio != "sim":
        ap.error("--late-joiners and --owner-dips need --audio sim")
    if args.cmd == "fetch":
        fetch(args.meetings, args.audio)
        return
    if args.cmd == "compare":
        res = {lab: json.loads((RESULTS / f"recognition-{lab}.json").read_text())
               for lab in args.labels}
        print(table(res))
        return

    base: dict = {"ingest_token": "eval-token-not-a-secret-000000", "vad_backend": "silero",
                  "models_dir": DATA.parent / "models"}
    if args.whisper_url:
        base["whisper_server_url"] = args.whisper_url
    if args.diarizer:
        base["diarization"] = args.diarizer != "none"
        if args.diarizer != "none":
            base["diarizer"] = args.diarizer
    if args.prompt is not None:
        base["whisper_prompt"] = args.prompt
    for kv in args.set:
        k, _, v = kv.partition("=")
        base[k] = v
    rows = []
    for m in args.meetings:
        print(f"{args.label}: {m} ...", flush=True)
        rows.append(run_meeting(m, args, base))
        print(f"  WER {fmt(rows[-1]['wer'])} (owner {fmt(rows[-1]['wer_owner'])}, others "
              f"{fmt(rows[-1]['wer_others'])})  DER {fmt(rows[-1]['der'])}  confusion "
              f"{fmt(rows[-1]['confusion'] / rows[-1]['total'])}  missed others "
              f"{fmt(rows[-1]['missed_others'])}", flush=True)
    for row in rows:  # store rates, not durations
        for k in ("missed", "false_alarm", "confusion"):
            row[k] = row[k] / row["total"]
    pool_rows = [{**r, **{k: r[k] * r["total"] for k in ("missed", "false_alarm",
                                                          "confusion")}} for r in rows]
    result = {"label": args.label, "args": {k: v for k, v in vars(args).items()
                                            if k != "cmd"},
              "settings": {k: str(v) for k, v in base.items() if k != "ingest_token"},
              "audio": {"mode": args.audio,
                        **({"distance": args.distance, "rt60": args.rt60, "snr": args.snr,
                            "late_joiners": args.late_joiners, "owner_dips": args.owner_dips}
                           if args.audio == "sim" else {}),
                        "opus_kbps": args.opus_kbps,
                        **({"level": args.level} if args.level is not None else {})},
              "meetings": rows, "pooled": pooled(pool_rows)}
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"recognition-{args.label}.json"
    out.write_text(json.dumps(result, indent=1))
    print(table({args.label: result}))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
