#!/usr/bin/env bash
# Athena Zoom bot spike: start one bot for one meeting, then clean up after it.
# Throwaway (Phase 0). Used inside the Docker image and on the host.
#
#   CONVO_ZOOM_SDK_KEY=... CONVO_ZOOM_SDK_SECRET=... \
#     ./run.sh [--python] -- --meeting-id 81234567890 --passcode xxxxxx --minutes 30 --out out
#
# ATHENA_SPIKE_PULSE=1 starts a private PulseAudio with a null sink for this run only and
# stops it afterwards (the finding says whether the SDK needs it). Nothing stays running.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
variant=cpp
if [[ "${1:-}" == "--python" ]]; then variant=python; shift; fi
if [[ "${1:-}" == "--" ]]; then shift; fi

if [[ -z "${ATHENA_SPIKE_JWT:-}" ]]; then
  ATHENA_SPIKE_JWT="$(python3 "$here/sign_jwt.py" --hours 2)"
  export ATHENA_SPIKE_JWT
fi

pulse_dir=""
# shellcheck disable=SC2329  # called by the EXIT trap
cleanup() {
  if [[ -n "$pulse_dir" ]]; then
    local pa_pid="" tries=50
    if [[ -r "$pulse_dir/pid" ]]; then pa_pid="$(cat "$pulse_dir/pid")"; fi
    PULSE_RUNTIME_PATH="$pulse_dir" pulseaudio --kill 2>/dev/null || true
    # wait for it to exit, so it can't recreate its runtime dir after we delete it
    while [[ -n "$pa_pid" ]] && kill -0 "$pa_pid" 2>/dev/null && ((tries-- > 0)); do
      sleep 0.1
    done
    if [[ -n "$pa_pid" ]]; then kill -KILL "$pa_pid" 2>/dev/null || true; fi
    rm -rf "$pulse_dir"
  fi
}
trap cleanup EXIT

if [[ "${ATHENA_SPIKE_PULSE:-0}" == "1" ]]; then
  pulse_dir="$(mktemp -d -t athena-pulse.XXXXXX)"
  export PULSE_RUNTIME_PATH="$pulse_dir"
  pulseaudio --daemonize=yes --exit-idle-time=-1 --use-pid-file=yes \
    --load="module-native-protocol-unix" --load="module-null-sink sink_name=athena_null"
  pactl set-default-sink athena_null
  pactl set-default-source athena_null.monitor
  mkdir -p "$HOME/.config"
  # Zoom's Linux samples set this so the SDK uses the default (PulseAudio) device.
  printf '[General]\nsystem.audio.type=default\n' > "$HOME/.config/zoomus.conf"
fi

if [[ "$variant" == "python" ]]; then
  bot=(python3 "$here/python/athena_spike.py")
else
  bot=("${ATHENA_SPIKE_BIN:-$here/cpp/build/athena-meeting-spike}")
fi

# Not exec: the trap must stop PulseAudio after the bot exits. Forward signals to the bot.
"${bot[@]}" "$@" &
pid=$!
trap 'kill -TERM "$pid" 2>/dev/null || true' INT TERM
status=0
wait "$pid" || status=$?
if ((status > 128)); then
  # a signal to run.sh interrupts wait; wait again for the bot's own exit status
  status=0
  wait "$pid" || status=$?
fi
exit "$status"
