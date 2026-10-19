#!/usr/bin/env bash
# barvis-sound: update the server checkout and run it in the background.
#
#   server/scripts/deploy.sh deploy [REF]   # update to REF (default: <remote>/main), install, restart, check
#   server/scripts/deploy.sh start | stop | restart
#   server/scripts/deploy.sh status         # services, health, checkout
#   server/scripts/deploy.sh logs [serve|mcp|whisper]
#   server/scripts/deploy.sh units          # print the systemd units it would install
#
# Runs as your normal user (no sudo) from any directory. Services:
#   barvis-sound          convo-server serve          (API + worker, CONVO_HOST:CONVO_PORT)
#   barvis-sound-mcp      convo-server mcp --http     (only if CONVO_MCP_TOKEN is set)
#   barvis-sound-whisper  BARVIS_SOUND_WHISPER_CMD    (only if set)
# They are systemd user services (start at boot, restart on crash) where systemd is
# available, otherwise background processes with pid files in $CONVO_DATA_DIR/run.
#
# Settings: environment, or BARVIS_SOUND_* lines in server/.env (all optional):
#   BARVIS_SOUND_REMOTE       git remote (default: the one pointing at */barvis-sound or */barvis-ai)
#   BARVIS_SOUND_BRANCH       branch to follow (default: main)
#   BARVIS_SOUND_EXTRAS       pip extras (default: vad,diarization; +pyannote if CONVO_DIARIZER=pyannote)
#   BARVIS_SOUND_WHISPER_CMD  full whisper-server command line, absolute path first
#   BARVIS_SOUND_RUNNER       systemd, background or auto (default: auto)
set -euo pipefail

SERVER_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
REPO_DIR=$(cd "$SERVER_DIR/.." && pwd)
cd "$SERVER_DIR"

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> [barvis-sound] $*"; }

env_value() {  # $1 from the environment (even if empty), else from .env, else $2
  local v=""
  if [ -n "${!1+x}" ]; then
    v=${!1}
  elif [ -f .env ]; then
    v=$(grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//' || true)
  fi
  echo "${v:-$2}"
}

UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
SERVICES=(serve mcp whisper)

unit_name() {
  case $1 in serve) echo barvis-sound ;; *) echo "barvis-sound-$1" ;; esac
}

load_config() {
  REMOTE_NAME=$(env_value BARVIS_SOUND_REMOTE "")
  BRANCH=$(env_value BARVIS_SOUND_BRANCH main)
  WHISPER_CMD=$(env_value BARVIS_SOUND_WHISPER_CMD "")
  RUNNER=$(env_value BARVIS_SOUND_RUNNER auto)
  DATA_DIR=$(env_value CONVO_DATA_DIR data)
  case $DATA_DIR in /*) ;; *) DATA_DIR=$SERVER_DIR/$DATA_DIR ;; esac
  PORT=$(env_value CONVO_PORT 8000)
  HOST=$(env_value CONVO_HOST 127.0.0.1)
  MCP_TOKEN=$(env_value CONVO_MCP_TOKEN "")
  MCP_HOST=$(env_value CONVO_MCP_HOST 127.0.0.1)
  MCP_PORT=$(env_value CONVO_MCP_PORT 8001)
  WHISPER_URL=$(env_value CONVO_WHISPER_SERVER_URL "")
  local extras
  extras=vad,diarization
  [ "$(env_value CONVO_DIARIZER ecapa)" = pyannote ] && extras=$extras,pyannote
  EXTRAS=$(env_value BARVIS_SOUND_EXTRAS "$extras")
  if [ "$RUNNER" = auto ]; then
    if systemctl --user show-environment >/dev/null 2>&1; then RUNNER=systemd; else RUNNER=background; fi
  fi
  case $RUNNER in systemd|background) ;; *) die "BARVIS_SOUND_RUNNER must be systemd, background or auto" ;; esac
}

enabled() {  # is service $1 configured to run?
  case $1 in
    serve) return 0 ;;
    mcp) [ -n "$MCP_TOKEN" ] ;;
    whisper) [ -n "$WHISPER_CMD" ] ;;
  esac
}

exec_line() {  # command line of service $1
  case $1 in
    serve) echo "$SERVER_DIR/.venv/bin/convo-server serve" ;;
    mcp) echo "$SERVER_DIR/.venv/bin/convo-server mcp --http" ;;
    whisper) echo "$WHISPER_CMD" ;;
  esac
}

probe_host() {  # an address to check a service bound to $1
  case $1 in 0.0.0.0|::|"[::]"|"") echo 127.0.0.1 ;; *) echo "$1" ;; esac
}

port_of() {
  case $1 in
    serve) echo "$PORT" ;;
    mcp) echo "$MCP_PORT" ;;
    whisper) [ -n "$WHISPER_URL" ] && echo "$WHISPER_URL" | sed -E 's#^[a-z]+://[^:/]+:?([0-9]*).*#\1#' ;;
  esac
}

check_url() {  # URL that answers when service $1 is up
  case $1 in
    serve) echo "http://$(probe_host "$HOST"):$PORT/health" ;;
    mcp) echo "http://$(probe_host "$MCP_HOST"):$MCP_PORT/mcp" ;;
    whisper) echo "${WHISPER_URL%/}/" ;;
  esac
}

answers() {  # any HTTP answer at all (MCP and whisper reply 4xx to a bare GET)
  local code
  code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' "$1" 2>/dev/null || true)
  [ -n "$code" ] && [ "$code" != 000 ]
}

healthy() {
  local url; url=$(check_url "$1")
  [ -n "$url" ] && [ "$url" != / ] || return 0  # nothing to check
  if [ "$1" = serve ]; then curl -sf -m 3 "$url" >/dev/null 2>&1; else answers "$url"; fi
}

wait_healthy() {  # $1 service, $2 seconds
  local _
  for _ in $(seq 1 "$2"); do
    healthy "$1" && return 0
    if [ "$RUNNER" = background ] && ! bg_running "$1"; then return 1; fi
    sleep 1
  done
  return 1
}

# --- systemd user services ------------------------------------------------------------

render_unit() {  # systemd unit for service $1
  cat <<EOF
# Written by barvis-sound server/scripts/deploy.sh; rerun it instead of editing.
[Unit]
Description=barvis-sound $1
StartLimitIntervalSec=0

[Service]
WorkingDirectory=$SERVER_DIR
ExecStart=$(exec_line "$1")
Restart=on-failure
RestartSec=5
# Let the worker finish its current step before stopping.
TimeoutStopSec=60

[Install]
WantedBy=default.target
EOF
}

install_unit() {  # write the unit for $1; returns 0 if it changed
  local file new
  file=$UNIT_DIR/$(unit_name "$1").service
  new=$(render_unit "$1")
  mkdir -p "$UNIT_DIR"
  if [ -f "$file" ] && [ "$(cat "$file")" = "$new" ]; then return 1; fi
  printf '%s\n' "$new" > "$file"
  return 0
}

ensure_linger() {  # user services keep running after logout and start at boot
  if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true)" = yes ]; then return; fi
  loginctl enable-linger "$(id -un)" 2>/dev/null \
    || log "warning: could not turn on linger; services stop when you log out. Run once: sudo loginctl enable-linger $(id -un)"
}

# --- background processes (no systemd) ------------------------------------------------

pid_file() { echo "$DATA_DIR/run/$1.pid"; }
log_file() { echo "$DATA_DIR/logs/$1.log"; }

bg_running() {
  local f; f=$(pid_file "$1")
  [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null
}

bg_start() {
  mkdir -p "$DATA_DIR/run" "$DATA_DIR/logs"
  echo "--- $(date '+%F %T') starting: $(exec_line "$1")" >> "$(log_file "$1")"
  # shellcheck disable=SC2046  # the command line is split into words on purpose
  setsid nohup $(exec_line "$1") >> "$(log_file "$1")" 2>&1 < /dev/null &
  echo $! > "$(pid_file "$1")"
}

bg_stop() {
  local f pid
  f=$(pid_file "$1")
  [ -f "$f" ] || return 0
  pid=$(cat "$f")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    for _ in $(seq 1 60); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid"
  fi
  rm -f "$f"
}

# --- either runner --------------------------------------------------------------------

is_running() {
  if [ "$RUNNER" = systemd ]; then systemctl --user -q is-active "$(unit_name "$1")"; else bg_running "$1"; fi
}

port_taken() {  # something else already listens on the port of $1 (e.g. a manual run)
  local p; p=$(port_of "$1")
  [ -n "$p" ] || return 1
  if command -v ss >/dev/null; then
    ss -ltnH "sport = :$p" 2>/dev/null | grep -q .
  else
    (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null
  fi
}

check_ports() {  # refuse to start next to a copy started by hand
  local s
  for s in "${SERVICES[@]}"; do
    enabled "$s" || continue
    if ! is_running "$s" && port_taken "$s"; then
      die "port $(port_of "$s") is already in use, probably by $(exec_line "$s" | awk '{print $1, $2}') started by hand. Stop it (find it with: ss -ltnp 'sport = :$(port_of "$s")') and rerun"
    fi
  done
}

start_service() {  # $1 service, $2 "restart" to restart a running one
  local name; name=$(unit_name "$1")
  if [ "$RUNNER" = systemd ]; then
    systemctl --user enable -q "$name"
    if [ "${2:-}" = restart ]; then systemctl --user restart "$name"; else systemctl --user start "$name"; fi
  else
    if [ "${2:-}" = restart ]; then bg_stop "$1"; fi
    bg_running "$1" || bg_start "$1"
  fi
}

stop_service() {
  if [ "$RUNNER" = systemd ]; then
    systemctl --user disable -q --now "$(unit_name "$1")" 2>/dev/null || true
  else
    bg_stop "$1"
  fi
}

show_logs() {
  if [ "$RUNNER" = systemd ]; then
    journalctl --user -u "$(unit_name "$1")" -n 40 --no-pager "${@:2}"
  else
    tail -n 40 "${@:2}" "$(log_file "$1")"
  fi
}

# Start or restart every configured service and stop the rest. Restarts serve and mcp
# always (new code); whisper only when its command changed, so its model stays loaded.
apply_services() {
  local s changed=()
  check_ports
  if [ "$RUNNER" = systemd ]; then
    for s in "${SERVICES[@]}"; do
      if enabled "$s" && install_unit "$s"; then changed+=("$s"); fi
    done
    systemctl --user daemon-reload
    ensure_linger
  fi
  for s in "${SERVICES[@]}"; do
    if ! enabled "$s"; then
      is_running "$s" && { log "stopping $(unit_name "$s") (not configured)"; stop_service "$s"; }
      continue
    fi
    # whisper keeps running unless its unit changed (background: use "restart")
    if [ "$s" = whisper ] && is_running whisper && [[ " ${changed[*]-} " != *" whisper "* ]]; then
      continue
    fi
    log "starting $(unit_name "$s")"
    start_service "$s" restart
  done
}

check_services() {  # wait until every configured service answers
  local s ok=0
  for s in whisper serve mcp; do
    enabled "$s" || continue
    if wait_healthy "$s" 120; then
      log "$(unit_name "$s") up ($(check_url "$s"))"
    else
      log "$(unit_name "$s") is not answering on $(check_url "$s"):"
      show_logs "$s" >&2 || true
      ok=1
    fi
  done
  return $ok
}

# --- install --------------------------------------------------------------------------

first_env() {  # create server/.env with fresh tokens on a new machine
  [ -f .env ] && return 0
  local t1 t2
  t1=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
  t2=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
  sed -e "s|^CONVO_INGEST_TOKEN=.*|CONVO_INGEST_TOKEN=$t1|" \
      -e "s|^# CONVO_MCP_TOKEN=.*|CONVO_MCP_TOKEN=$t2|" .env.example > .env
  chmod 600 .env
  log "created server/.env with new tokens; review it (endpoints, CONVO_PUBLIC_URL, CONVO_MCP_HOST)"
}

install_deps() {
  if [ ! -x .venv/bin/python ]; then
    log "creating .venv"
    if command -v uv >/dev/null; then uv venv -q -p 3.12 .venv || uv venv -q .venv
    else python3 -m venv .venv; fi
  fi
  log "installing convo-server[$EXTRAS]"
  if command -v uv >/dev/null; then
    uv pip install -q -p .venv/bin/python -e ".[$EXTRAS]"
  else
    .venv/bin/python -m pip install -q -e ".[$EXTRAS]"
  fi
}

remote() {
  if [ -n "$REMOTE_NAME" ]; then echo "$REMOTE_NAME"; return; fi
  git -C "$REPO_DIR" remote -v \
    | awk 'tolower($2) ~ /[\/:]sdarioosh\/(barvis-sound|barvis-ai|convo-ai)(\.git)?$/ {print $1; exit}'
}

update_checkout() {  # $1 ref or empty; sets PREV and NEW
  local r ref
  git -C "$REPO_DIR" diff --quiet HEAD -- \
    || die "tracked files in $REPO_DIR have local changes (git status); commit or stash them first"
  r=$(remote); [ -n "$r" ] || die "no git remote points at the barvis-sound repo; set BARVIS_SOUND_REMOTE"
  ref=${1:-$r/$BRANCH}
  PREV=$(git -C "$REPO_DIR" rev-parse HEAD)
  log "fetching $r"
  git -C "$REPO_DIR" fetch -q "$r"
  if [ "$ref" = "$r/$BRANCH" ] && git -C "$REPO_DIR" show-ref -q --verify "refs/heads/$BRANCH" \
      && { [ "$(git -C "$REPO_DIR" branch --show-current)" = "$BRANCH" ] \
           || git -C "$REPO_DIR" checkout -q "$BRANCH" 2>/dev/null; }; then
    git -C "$REPO_DIR" merge -q --ff-only "$ref" || die "local $BRANCH has diverged from $ref"
  else
    git -C "$REPO_DIR" checkout -q --detach "$ref"
  fi
  NEW=$(git -C "$REPO_DIR" rev-parse HEAD)
}

cmd_deploy() {
  load_config
  check_ports
  update_checkout "${1:-}"
  first_env
  load_config  # .env may be new
  [ -n "$MCP_TOKEN" ] || log "warning: CONVO_MCP_TOKEN is not set in server/.env, so MCP over HTTP (what Barvis uses) is not started"
  local short; short=$(git -C "$REPO_DIR" rev-parse --short HEAD)
  if [ "$PREV" = "$NEW" ]; then log "already at $short"; else log "updated $(git -C "$REPO_DIR" rev-parse --short "$PREV") -> $short"; fi
  install_deps
  apply_services
  if check_services; then
    log "deployed $short ($RUNNER)"
    return 0
  fi
  if [ "$PREV" != "$NEW" ]; then
    log "rolling back to $(git -C "$REPO_DIR" rev-parse --short "$PREV")"
    git -C "$REPO_DIR" checkout -q --detach "$PREV"
    install_deps
    apply_services
    check_services || true
    die "deploy of $short failed and was rolled back (checkout is detached at the old commit; the next deploy returns to $BRANCH)"
  fi
  die "barvis-sound is not healthy; see the logs above"
}

cmd_status() {
  load_config
  local s state
  echo "barvis-sound: $(git -C "$REPO_DIR" rev-parse --short HEAD) $(git -C "$REPO_DIR" log -1 --format=%s) ($RUNNER)"
  for s in "${SERVICES[@]}"; do
    if ! enabled "$s"; then state="not configured"
    elif ! is_running "$s"; then state="stopped"
    elif healthy "$s"; then state="running, answering on $(check_url "$s")"
    else state="running, NOT answering on $(check_url "$s")"; fi
    printf '  %-22s %s\n' "$(unit_name "$s")" "$state"
  done
}

case "${1:-}" in
  deploy) shift; cmd_deploy "$@" ;;
  start) load_config; apply_services; check_services ;;
  restart) load_config; for s in "${SERVICES[@]}"; do enabled "$s" && is_running "$s" && stop_service "$s"; done; apply_services; check_services ;;
  stop) load_config; for s in "${SERVICES[@]}"; do stop_service "$s"; done; log "stopped" ;;
  status) cmd_status ;;
  logs) load_config; show_logs "${2:-serve}" "${@:3}" ;;
  units) load_config; for s in "${SERVICES[@]}"; do enabled "$s" && { echo "### $(unit_name "$s").service"; render_unit "$s"; }; done; true ;;
  *) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
