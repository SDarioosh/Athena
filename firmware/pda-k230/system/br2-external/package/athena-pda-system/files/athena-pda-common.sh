# shellcheck shell=sh disable=SC2034 # the variables are for the scripts that source this
# /usr/libexec/athena-pda/athena-pda-common.sh: sourced by the Athena PDA's boot
# scripts and helpers. Reads /etc/default/athena-pda over the defaults below and
# starts/stops programs under athena-supervise (restart on crash, output to syslog).

ATHENA_PDA_DEFAULTS="${ATHENA_PDA_DEFAULTS:-/etc/default/athena-pda}"
ATHENA_PDA_LIBEXEC=/usr/libexec/athena-pda
# Supervisor pid files: root's own directory, never the PDA user's run directory
# (a pid file the user could write would let it pick what root kills).
ATHENA_PDA_SV_DIR=/run/athena-pda-sv
ATHENA_PDA_LAUNCHER_INIT="$ATHENA_PDA_LIBEXEC/k230_phone_ui.init"

ATHENA_PDA_BOOT_UI=athena-pda
ATHENA_PDA_DIR=/var/lib/athena-pda
ATHENA_PDA_RUN=/run/athena-pda
ATHENA_PDA_USER=athena-pda
ATHENA_VISION=on
ATHENA_VISION_USER=root
ATHENA_TS_MODE=tun
ATHENA_TS_PROXY=127.0.0.1:1055
ATHENA_TS_HOSTNAME=athena-pda
ATHENA_TS_STATE_DIR=/var/lib/tailscale
ATHENA_TS_SOCKET=/run/tailscale/tailscaled.sock
# athena-tailnet watch: seconds between status checks while a login is needed /
# otherwise, and between two `tailscale login` runs (each waits for up to
# ATHENA_TS_LOGIN_TIMEOUT for the owner to approve, then a new link is made)
ATHENA_TS_WATCH_LOGIN_POLL=5
ATHENA_TS_WATCH_POLL=30
ATHENA_TS_LOGIN_RETRY=60
ATHENA_TS_LOGIN_TIMEOUT=600

if [ -r "$ATHENA_PDA_DEFAULTS" ]; then
	# shellcheck source-path=SCRIPTDIR source=athena-pda.default
	. "$ATHENA_PDA_DEFAULTS"
fi
export ATHENA_PDA_DIR ATHENA_PDA_RUN
[ -z "${ATHENA_PDA_DRM:-}" ] || export ATHENA_PDA_DRM
[ -z "${ATHENA_PDA_TOUCH:-}" ] || export ATHENA_PDA_TOUCH

# The run directory (0700, the sockets' access control) and the data directory.
athena_dirs() {
	mkdir -p "$ATHENA_PDA_SV_DIR" "$ATHENA_PDA_RUN" "$ATHENA_PDA_DIR"
	chmod 0755 "$ATHENA_PDA_SV_DIR"
	chown "$ATHENA_PDA_USER:" "$ATHENA_PDA_RUN" "$ATHENA_PDA_DIR"
	chmod 0700 "$ATHENA_PDA_RUN" "$ATHENA_PDA_DIR"
}

athena_pidfile() {
	echo "$ATHENA_PDA_SV_DIR/$1.pid"
}

athena_running() {
	start-stop-daemon -K -t -q -p "$(athena_pidfile "$1")" -x /bin/sh
}

# athena_start NAME USER PROGRAM [ARGS...]: run PROGRAM as USER under the
# supervisor, in the background. The environment is passed on.
athena_start() {
	_name="$1"
	mkdir -p "$ATHENA_PDA_SV_DIR"
	printf 'Starting %s: ' "$_name"
	if athena_running "$_name"; then
		echo "already running"
		return 0
	fi
	if start-stop-daemon -S -q -b -m -p "$(athena_pidfile "$_name")" -x /bin/sh -- \
		"$ATHENA_PDA_LIBEXEC/athena-supervise" "$@"; then
		echo "OK"
	else
		echo "FAIL"
		return 1
	fi
}

# athena_stop NAME: stop the supervisor (it passes TERM on to the program); after
# 10 s, KILL both.
athena_stop() {
	_name="$1"
	_pidfile="$(athena_pidfile "$_name")"
	printf 'Stopping %s: ' "$_name"
	if ! athena_running "$_name"; then
		rm -f "$_pidfile" "$ATHENA_PDA_SV_DIR/$_name.child"
		echo "not running"
		return 0
	fi
	start-stop-daemon -K -q -s TERM -p "$_pidfile" -x /bin/sh
	_i=0
	while athena_running "$_name" && [ "$_i" -lt 100 ]; do
		sleep 0.1
		_i=$((_i + 1))
	done
	if athena_running "$_name"; then
		if [ -s "$ATHENA_PDA_SV_DIR/$_name.child" ]; then
			kill -KILL "$(cat "$ATHENA_PDA_SV_DIR/$_name.child")" 2>/dev/null
		fi
		start-stop-daemon -K -q -s KILL -p "$_pidfile" -x /bin/sh
		echo "killed"
	else
		echo "OK"
	fi
	rm -f "$_pidfile" "$ATHENA_PDA_SV_DIR/$_name.child"
}

# Is the PDA (not LilyGO's launcher) what starts at boot?
athena_pda_boots() {
	[ "$ATHENA_PDA_BOOT_UI" != "launcher" ]
}

# The flags `tailscale set`/`login` get on this image (a login without them resets
# them to Tailscale's defaults): the host name and netfilter mode off (no iptables
# or nft here), and no operator: no user but root may change tailscaled's settings
# (anyone may read `tailscale status`, which is all syncd does). Only root runs
# `tailscale login`: `athena-tailnet watch` (started by S45tailscaled) or
# `athena-tailnet login` from a root shell.
athena_ts_flags() {
	echo "--operator= --hostname=$ATHENA_TS_HOSTNAME --netfilter-mode=off"
}
