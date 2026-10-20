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
    python eval/recognition.py run --label far --mic Array1-01 --opus 16 --diarizer none
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
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from convo_server import audio as audio_mod  # noqa: E402
from convo_server.audio import SAMPLE_RATE  # noqa: E402
from convo_server.cli import build_diarizer, build_transcriber  # noqa: E402
from convo_server.config import Settings  # noqa: E402
from convo_server.diarize import enroll_voice  # noqa: E402
from convo_server.pipeline import Pipeline  # noqa: E402
from convo_server.store import Store  # noqa: E402
from convo_server.vad import make_vad  # noqa: E402

HERE = Path(__file__).resolve().parent
DATA = HERE / "data" / "ami"
RESULTS = HERE / "results"
# AMI test-set meetings from three sites (Edinburgh, IDIAP, TNO).
MEETINGS = ["ES2004a", "IS1009a", "TS3003a"]
AUDIO_URL = ("https://groups.inf.ed.ac.uk/ami/AMICorpusMirror/amicorpus/{m}/audio/"
             "{m}.{mic}.wav")
# Mix-Headset: every speaker close-miked (clean). Array1-01: one far-field room mic, closer
# to a wearable hearing the people around its owner.
MICS = ["Mix-Headset", "Array1-01"]
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


def fetch(meetings: list[str], mics: list[str]) -> None:
    zip_path = DATA / "ami_public_manual_1.6.2.zip"
    _download(ANNOT_URL, zip_path)
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if any(name.startswith(f"words/{m}.") for m in meetings):
                z.extract(name, DATA)
    for m in meetings:
        for mic in mics:
            _download(AUDIO_URL.format(m=m, mic=mic), DATA / f"{m}.{mic}.wav")
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
def opus_roundtrip(pcm: np.ndarray, kbps: float, complexity: int) -> bytes:
    """Encode a chunk the way a recorder does (Ogg/Opus, 20 ms frames, VBR)."""
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "s16le",
         "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0", "-c:a", "libopus",
         "-b:a", f"{kbps}k", "-compression_level", str(complexity), "-frame_duration", "20",
         "-application", "voip", "-f", "ogg", "pipe:1"],
        input=audio_mod.float_to_pcm16(pcm), capture_output=True, check=True)
    return proc.stdout


def run_meeting(meeting: str, args: argparse.Namespace, base: dict) -> dict:
    from pyannote.metrics.diarization import DiarizationErrorRate

    full = audio_mod.decode((DATA / f"{meeting}.{args.mic}.wav").read_bytes(), "wav")
    start, end = args.offset, args.offset + args.seconds
    window = full[int(start * SAMPLE_RATE): int(end * SAMPLE_RATE)]
    turns = rttm_turns(meeting)
    ref = annotation(turns, start, end)

    with tempfile.TemporaryDirectory(prefix="convo-eval-") as tmp:
        settings = Settings(**{**base, "data_dir": Path(tmp)})
        store = Store(settings.db_path)
        diarizer = build_diarizer(settings, store)
        enrolled: list[str] = []
        if args.enroll and diarizer is not None:
            by_time = sorted(ref.labels(), key=lambda s: -ref.label_duration(s))
            for i, spk in enumerate(by_time[: args.enroll]):
                regions = solo_regions(turns, spk, (start, end))
                parts, total = [], 0.0
                for a, b in regions:
                    if total >= args.enroll_seconds:
                        break
                    parts.append(full[int(a * SAMPLE_RATE): int(b * SAMPLE_RATE)])
                    total += b - a
                if total < 10:
                    print(f"  {meeting}: only {total:.0f}s solo speech for {spk}, skipped")
                    continue
                enroll_voice(store, diarizer.embedder, spk, np.concatenate(parts),
                             is_owner=(i == 0))
                enrolled.append(spk)
        pipeline = Pipeline(settings, store, make_vad(settings.vad_backend, settings),
                            build_transcriber(settings), diarizer)
        sid = f"eval-{meeting}"
        t = time.time()
        for i in range(0, window.size, int(CHUNK * SAMPLE_RATE)):
            part = window[i: i + int(CHUNK * SAMPLE_RATE)]
            if args.opus:
                codec, data = "opus", opus_roundtrip(part, args.opus, args.opus_complexity)
            else:
                codec, data = "wav", audio_mod.to_wav_bytes(part)
            pipeline.ingest_chunk(device="eval", session_id=sid, sequence=i // int(CHUNK *
                                  SAMPLE_RATE), started_at=T0 + i / SAMPLE_RATE, codec=codec,
                                  data=data)
        pipeline.end_session(sid)
        pipeline.process_session(sid)
        elapsed = time.time() - t
        sess = store.get_session(sid)
        if sess and sess["status"] != "done":
            raise RuntimeError(f"{meeting}: session {sess['status']}: {sess['error']}")
        segs = store.session_segments(sid)
        store.close()

    import jiwer

    ref_text = normalize(reference_words(meeting, start, end))
    hyp_text = normalize(" ".join(s.text for s in segs))
    m = jiwer.process_words(ref_text, hyp_text)
    hyp = annotation([(s.started_at - T0, s.ended_at - T0, s.speaker) for s in segs])
    der = DiarizationErrorRate(collar=0.25)
    from pyannote.core import Segment as PSeg
    from pyannote.core import Timeline

    d = der(ref, hyp, uem=Timeline([PSeg(0, args.seconds)]), detailed=True)
    out = {
        "meeting": meeting, "window": [start, end], "seconds_to_process": round(elapsed, 1),
        "ref_words": len(ref_text.split()), "wer": m.wer, "substitutions": m.substitutions,
        "deletions": m.deletions, "insertions": m.insertions,
        "der": d["diarization error rate"], "total": d["total"],
        "missed": d["missed detection"], "false_alarm": d["false alarm"],
        "confusion": d["confusion"], "ref_speakers": len(ref.labels()),
        "hyp_speakers": len(hyp.labels()), "hyp_labels": sorted(hyp.labels()),
        "enrolled": enrolled, "lines": len(segs),
        "hypothesis": [[round(s.started_at - T0, 2), round(s.ended_at - T0, 2), s.speaker,
                        s.text] for s in segs],
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
    seg_t = [(s.started_at - T0 + start, s.ended_at - T0 + start, s.speaker) for s in segs]
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
    f.add_argument("--mics", nargs="+", default=MICS, choices=MICS)
    r = sub.add_parser("run")
    r.add_argument("--label", required=True)
    r.add_argument("--meetings", nargs="+", default=MEETINGS)
    r.add_argument("--mic", default="Mix-Headset", choices=MICS)
    r.add_argument("--opus", type=float, default=0.0, metavar="KBPS",
                   help="send chunks as Ogg/Opus at this bitrate (T-Watch: 16), else WAV")
    r.add_argument("--opus-complexity", type=int, default=1, help="T-Watch default: 1")
    r.add_argument("--offset", type=float, default=300.0, help="window start (s)")
    r.add_argument("--seconds", type=float, default=300.0, help="window length (s)")
    r.add_argument("--whisper-url", help="whisper-server URL (else CONVO_WHISPER_*)")
    r.add_argument("--diarizer", help="ecapa, pyannote or none (else CONVO_DIARIZER)")
    r.add_argument("--prompt", help="CONVO_WHISPER_PROMPT override")
    r.add_argument("--enroll", type=int, default=0, help="enroll the N most active speakers")
    r.add_argument("--enroll-seconds", type=float, default=60.0)
    r.add_argument("--set", nargs="*", default=[], metavar="FIELD=VALUE",
                   help="extra Settings overrides, e.g. speaker_cluster_threshold=0.5")
    c = sub.add_parser("compare")
    c.add_argument("labels", nargs="+")
    args = ap.parse_args()

    if args.cmd == "fetch":
        fetch(args.meetings, args.mics)
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
