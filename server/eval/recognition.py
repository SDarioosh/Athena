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
  of everyone's speech labelled with a wrong enrolled name.
"""

from __future__ import annotations

import argparse
import json
import re
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
AUDIO_URL = ("https://groups.inf.ed.ac.uk/ami/AMICorpusMirror/amicorpus/{m}/audio/"
             "{m}.Mix-Headset.wav")
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


def fetch(meetings: list[str]) -> None:
    zip_path = DATA / "ami_public_manual_1.6.2.zip"
    _download(ANNOT_URL, zip_path)
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if any(name.startswith(f"words/{m}.") for m in meetings):
                z.extract(name, DATA)
    for m in meetings:
        _download(AUDIO_URL.format(m=m), DATA / f"{m}.Mix-Headset.wav")
        _download(RTTM_URL.format(m=m), DATA / f"{m}.rttm")
    print(f"AMI data in {DATA}")


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
                               getattr(inner, "temperature_inc", None)])
        self.dir = DATA.parent / "cache"
        self.dir.mkdir(parents=True, exist_ok=True)

    def transcribe(self, audio: np.ndarray, prompt: str = ""):
        import hashlib

        from convo_server.transcribe import TranscriptPiece

        h = hashlib.sha1(self.key.encode() + prompt.encode()
                         + np.ascontiguousarray(audio, np.float32).tobytes()).hexdigest()
        path = self.dir / f"{h}.json"
        if path.exists():
            return [TranscriptPiece(*p) for p in json.loads(path.read_text())]
        try:
            pieces = self.inner.transcribe(audio, prompt)
        except TypeError:  # pre-#24 transcriber: no prompt argument
            pieces = self.inner.transcribe(audio)
        path.write_text(json.dumps([[p.start, p.end, p.text] for p in pieces]))
        return pieces


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
    cover: dict[tuple[str, str], float] = {}
    for sg in segs:
        a, b = sg.started_at - t0 + offset, sg.ended_at - t0 + offset
        o = sum(max(0.0, min(b, y) - max(a, x)) for x, y, s in turns if s == spk)
        if sg.conversation_id and sg.speaker.startswith("Speaker "):
            key = (sg.conversation_id, sg.speaker)
            cover[key] = cover.get(key, 0.0) + o
    if not cover:
        return 0
    conv, label = max(cover, key=lambda k: cover[k])
    _, voice = store.rename_speaker(conv, label, name)
    if voice is None:
        return 0
    store.add_voice_sample(name, voice)
    return sum(r.lines for r in relabel_matches(store, name, threshold))


def run_meeting(meeting: str, args: argparse.Namespace, base: dict) -> dict:
    from pyannote.metrics.diarization import DiarizationErrorRate

    full = audio_mod.decode((DATA / f"{meeting}.Mix-Headset.wav").read_bytes(), "wav")
    start, end = args.offset, args.offset + args.seconds
    turns = rttm_turns(meeting)
    ref = annotation(turns, start, end)
    by_time = sorted(ref.labels(), key=lambda s: -ref.label_duration(s))
    if args.owner_boost_db:
        full = boost_speaker(full, turns, by_time[0], args.owner_boost_db)
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
        pipeline = Pipeline(settings, store, make_vad(settings.vad_backend), transcriber,
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

    ref_text = normalize(reference_words(meeting, start, end))
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
        "hypothesis": [[round(a, 2), round(b, 2), spk, text] for a, b, spk, text in segs],
    }
    out.update(_naming(turns, start, end, segs, enrolled))
    return out


def _naming(turns, start, end, segs, enrolled) -> dict:
    """Frame-level (0.1 s) check of enrolled names on single-speaker reference frames."""
    if not enrolled:
        return {}
    step = 0.1
    frames = np.arange(start, end, step)
    right = wrong = enrolled_n = n = 0
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
    return {"named_right": right / enrolled_n if enrolled_n else None,
            "named_wrong": wrong / n if n else None, "named_frames": n}


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
    named = [r for r in rows if r.get("named_right") is not None]
    if named:
        out["named_right"] = float(np.mean([r["named_right"] for r in named]))
        out["named_wrong"] = float(np.mean([r["named_wrong"] for r in named]))
    return out


def fmt(x) -> str:
    return "—" if x is None else f"{100 * x:.1f}%"


def table(results: dict[str, dict]) -> str:
    cols = ["wer", "der", "confusion", "missed", "false_alarm", "named_right", "named_wrong"]
    lines = ["| run | meeting | " + " | ".join(cols) + " | speakers (ref/hyp) | time |",
             "|---" * (len(cols) + 5) + "|"]
    for label, res in results.items():
        for r in res["meetings"]:
            lines.append(f"| {label} | {r['meeting']} | "
                         + " | ".join(fmt(r.get(c)) for c in cols)
                         + f" | {r['ref_speakers']}/{r['hyp_speakers']}"
                         f" | {r['seconds_to_process']}s |")
        p = res["pooled"]
        lines.append(f"| **{label}** | all | " + " | ".join(fmt(p.get(c)) for c in cols)
                     + f" | | {p['seconds_to_process']}s |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--meetings", nargs="+", default=MEETINGS)
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
    r.add_argument("--set", nargs="*", default=[], metavar="FIELD=VALUE",
                   help="extra Settings overrides, e.g. speaker_cluster_threshold=0.5")
    c = sub.add_parser("compare")
    c.add_argument("labels", nargs="+")
    args = ap.parse_args()

    if args.cmd == "fetch":
        fetch(args.meetings)
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
        print(f"  WER {fmt(rows[-1]['wer'])}  DER {fmt(rows[-1]['der'])}  confusion "
              f"{fmt(rows[-1]['confusion'] / rows[-1]['total'])}", flush=True)
    for row in rows:  # store rates, not durations
        for k in ("missed", "false_alarm", "confusion"):
            row[k] = row[k] / row["total"]
    pool_rows = [{**r, **{k: r[k] * r["total"] for k in ("missed", "false_alarm",
                                                          "confusion")}} for r in rows]
    result = {"label": args.label, "args": {k: v for k, v in vars(args).items()
                                            if k != "cmd"},
              "settings": {k: str(v) for k, v in base.items() if k != "ingest_token"},
              "meetings": rows, "pooled": pooled(pool_rows)}
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"recognition-{args.label}.json"
    out.write_text(json.dumps(result, indent=1))
    print(table({args.label: result}))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
