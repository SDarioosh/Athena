#!/usr/bin/env bash
# Capture-quality eval (hub personal context 4.3): pyannote with and without the opt-in
# attribution fixes, on the far-field table mic and the simulated room, people joining
# late and arm movement. Public AMI audio only (eval/data/, never committed).
#
#   cd server && eval/capture_quality.sh            # all runs, then the tables
#   eval/capture_quality.sh compare                 # only the tables of earlier runs
#
# 24 diarization runs (4 conditions x 6 variants), then 2 with a looser speech gate.
# Needs whisper-server with large-v3-turbo-q5_0 (CONVO_WHISPER_SERVER_URL, default
# http://127.0.0.1:8178) and the extras `vad,diarization,pyannote,eval`. Runs with
# --cache: whisper results and pyannote turns are reused, so after the first run of a
# condition the variants take seconds. Results: eval/results/recognition-gpu-*.json.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
export CONVO_WHISPER_SERVER_URL=${CONVO_WHISPER_SERVER_URL:-http://127.0.0.1:8178}
export PYTHONUNBUFFERED=1  # show progress at once, also through a pipe (| tee)

CONDITIONS=(
  "table:--audio array --level -24 --opus 24"
  "room:--audio sim --distance 2 --opus 24"
  "joiners:--audio sim --distance 2 --opus 24 --late-joiners"
  "arm:--audio sim --distance 2 --opus 24 --owner-dips 12"
)
# Spelled out, so the runs mean the same whatever the defaults are (since 2026-10-24
# pyannote turns the fix and vad_threshold=0.35 on by default).
OFF="speaker_split_lines=false speaker_line_check=false vad_threshold=0.5"
FIX="speaker_split_lines=true speaker_split_min_seconds=0.3 speaker_line_check=true vad_threshold=0.5"
VARIANTS=(
  "py:$OFF"
  "py-fix:$FIX"
  "py-fb0.3:pyannote_vbx_fb=0.3 $OFF"
  "py-fb0.3-fix:pyannote_vbx_fb=0.3 $FIX"
  "py-fb0.2:pyannote_vbx_fb=0.2 $OFF"
  "py-fb0.2-fix:pyannote_vbx_fb=0.2 $FIX"
)

# Transcription: a looser speech gate (Silero 0.35, default 0.5) for the others' quiet,
# far speech; it changes what whisper hears, so these runs transcribe again.
EXTRA=(
  "table:py-fix-vad35:${FIX/vad_threshold=0.5/vad_threshold=0.35}"
  "room:py-fix-vad35:${FIX/vad_threshold=0.5/vad_threshold=0.35}"
)
audio_of() { for c in "${CONDITIONS[@]}"; do [[ ${c%%:*} == "$1" ]] && echo "${c#*:}"; done; return 0; }

labels=()
for c in "${CONDITIONS[@]}"; do
  for v in "${VARIANTS[@]}"; do
    labels+=("gpu-${c%%:*}-${v%%:*}")
  done
done
for e in "${EXTRA[@]}"; do
  IFS=: read -r name var _ <<< "$e"
  labels+=("gpu-$name-$var")
done

mkdir -p eval/results  # a fresh checkout has none yet
if [[ "${1:-run}" != "compare" ]]; then
  echo "== downloading AMI audio and annotations into eval/data/ (~600 MB, once)"
  "$PY" eval/recognition.py fetch --audio array sim
  "$PY" -c "import torch; print('== GPU for pyannote:', torch.cuda.is_available() and
            torch.cuda.get_device_name(0) or 'none, runs on CPU (slow)')"
  runs=()
  for c in "${CONDITIONS[@]}"; do
    for v in "${VARIANTS[@]}"; do runs+=("${c%%:*}:$v"); done
  done
  runs+=("${EXTRA[@]}")
  for r in "${runs[@]}"; do
    IFS=: read -r name var set_args <<< "$r"
    audio=$(audio_of "$name")
    {
      label="gpu-$name-$var"
      if [[ -f "eval/results/recognition-$label.json" ]]; then
        echo "== $label: done before, skipped"
        continue
      fi
      echo "== $label ($(date +%H:%M), log: eval/results/$label.log)"
      # shellcheck disable=SC2086  # word-split the flag lists on purpose
      "$PY" eval/recognition.py run --label "$label" --cache --enroll 2 $audio \
        --diarizer pyannote ${set_args:+--set $set_args} > "eval/results/$label.log" 2>&1 \
        || { echo "   failed, see eval/results/$label.log"; continue; }
      grep -E "^  WER" "eval/results/$label.log" || true
    }
  done
fi

# pooled numbers per run: the ones that matter for attribution
"$PY" - "${labels[@]}" <<'EOF'
import json, sys
from pathlib import Path
cols = [("confusion", "confusion"), ("named_right", "named right"),
        ("named_wrong", "named wrong"), ("owner_precision", "owner precision"),
        ("owner_recall", "owner recall"), ("unenrolled_named", "unenrolled named"),
        ("wer", "WER")]
print("| run | " + " | ".join(c[1] for c in cols) + " | time |")
print("|---" * (len(cols) + 2) + "|")
for label in sys.argv[1:]:
    f = Path("eval/results") / f"recognition-{label}.json"
    if not f.exists():
        print(f"| {label} | " + " | ".join("–" for _ in cols) + " | |")
        continue
    p = json.loads(f.read_text())["pooled"]
    cells = ["–" if p.get(k) is None else f"{100 * p[k]:.1f} %" for k, _ in cols]
    print(f"| {label} | " + " | ".join(cells) + f" | {p['seconds_to_process']:.0f} s |")
EOF
