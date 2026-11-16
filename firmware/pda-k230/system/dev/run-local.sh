#!/bin/sh
# run-local.sh: the PDA's programs on a PC, for development, with throwaway
# directories (nothing of the board is needed and nothing outside the temp dir and
# the build dir is written).
#
#   firmware/pda-k230/system/dev/run-local.sh [--headless] [--track FILE] [--pair]
#
#   athena-pda       built from ui/ (an SDL2 window when SDL2 is installed, else
#                    headless; --headless forces headless)
#   athena-pda-syncd from syncd/, connected to the UI's glance.sock; unpaired unless
#                    --pair: it asks for a hub's "Pair a phone" link (a sandbox hub,
#                    fictional data only), read from stdin, never in a command line. No tailscale on the PC: the Status screen
#                    shows the tailnet as unknown.
#   athena-vision    can't run on a PC (KPU, ISP); with --track, the replay tool
#                    plays a keypoint track through the vision core and prints the
#                    vision.sock lines it would send (it doesn't connect to the UI).
#
# BUILD (default firmware/pda-k230/build-local) holds the CMake builds. Ctrl-C stops all.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
K230="$(cd "$HERE/../.." && pwd)"
BUILD="${BUILD:-$K230/build-local}"

headless=0
track=""
pair=""
while [ $# -gt 0 ]; do
	case "$1" in
		--headless) headless=1 ;;
		--track) track="${2:?--track FILE}"; shift ;;
		--pair) pair=1 ;;
		-h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "run-local.sh: unknown argument $1" >&2; exit 2 ;;
	esac
	shift
done

tmp="$(mktemp -d "${TMPDIR:-/tmp}/athena-pda-local.XXXXXX")"
ATHENA_PDA_DIR="$tmp/data"
ATHENA_PDA_RUN="$tmp/run"
export ATHENA_PDA_DIR ATHENA_PDA_RUN
# A sandbox hub on this PC (127.0.0.1:7010): syncd refuses loopback hubs unless told
# (syncd/athena_pda_sync/addresses.py). Development only; the image never sets it.
ATHENA_PDA_DEV_LOOPBACK_HUB=1
export ATHENA_PDA_DEV_LOOPBACK_HUB
mkdir -m 0700 "$ATHENA_PDA_DIR" "$ATHENA_PDA_RUN"

pids=""
cleanup() {
	for p in $pids; do
		kill "$p" 2>/dev/null || true
	done
	wait 2>/dev/null || true
	rm -rf "$tmp"
}
trap cleanup EXIT INT TERM

echo "== building the UI in $BUILD/ui"
cmake -S "$K230/ui" -B "$BUILD/ui" -DCMAKE_BUILD_TYPE=RelWithDebInfo >/dev/null
cmake --build "$BUILD/ui" -j --target athena-pda >/dev/null

if [ "$headless" -eq 1 ]; then
	ATHENA_PDA_BACKEND=headless
	export ATHENA_PDA_BACKEND
fi
echo "== athena-pda (data $ATHENA_PDA_DIR, sockets $ATHENA_PDA_RUN)"
"$BUILD/ui/athena-pda" &
ui_pid=$!
pids="$pids $ui_pid"

i=0
while [ ! -S "$ATHENA_PDA_RUN/glance.sock" ] && [ "$i" -lt 50 ]; do
	sleep 0.2
	i=$((i + 1))
done
[ -S "$ATHENA_PDA_RUN/glance.sock" ] || echo "warning: no glance.sock after 10 s" >&2

if [ -n "$pair" ]; then
	echo "== pairing: paste the hub's Pair a phone link, then Enter"
	PYTHONPATH="$K230/syncd" python3 -m athena_pda_sync --pair
fi
echo "== athena-pda-syncd"
PYTHONPATH="$K230/syncd" python3 -m athena_pda_sync &
pids="$pids $!"

if [ -n "$track" ]; then
	echo "== vision replay of $track"
	cmake -S "$K230/vision" -B "$BUILD/vision" -DVISION_TESTS=OFF -DVISION_TOOLS=ON >/dev/null
	cmake --build "$BUILD/vision" -j --target athena-vision-replay >/dev/null
	"$BUILD/vision/athena-vision-replay" "$track" || true
fi

echo "== running; Ctrl-C stops"
wait "$ui_pid" || true
