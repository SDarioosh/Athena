#!/usr/bin/env bash
# Athena: update the server checkout and run it in the background.
#
#   server/scripts/deploy.sh deploy [REF]   # update to REF (default: <remote>/main), install, restart,
#                                           # check, then update Odysseus (BARVIS_SOUND_ODYSSEUS_DIR)
#   server/scripts/deploy.sh odysseus       # only update Odysseus
#   server/scripts/deploy.sh start | stop | restart
#   server/scripts/deploy.sh status         # services, health, checkout
#   server/scripts/deploy.sh logs [serve|mcp|whisper]
#   server/scripts/deploy.sh units          # print the systemd units it would install
#
# Runs as your normal user (no sudo) from any directory. Services (names keep the old
# barvis-sound prefix on purpose: deployed units and settings depend on them):
#   barvis-sound          convo-server serve          (API + worker, CONVO_HOST:CONVO_PORT)
#   barvis-sound-mcp      convo-server mcp --http     (only if CONVO_MCP_TOKEN is set)
#   barvis-sound-whisper  BARVIS_SOUND_WHISPER_CMD    (only if set)
# They are systemd user services (start at boot, restart on crash) where systemd is
# available, otherwise background processes with pid files in $CONVO_DATA_DIR/run.
# With BARVIS_SOUND_RUNNER=docker they are the containers of server/compose.yml instead
# (sound-api, sound-mcp, whisper-gpu/whisper-cpu; README "Docker"), and the native
# services above are stopped.
#
# Settings: environment, or BARVIS_SOUND_* lines in server/.env (all optional):
#   BARVIS_SOUND_REMOTE       git remote (default: the one pointing at */barvis-sound or */barvis-ai)
#   BARVIS_SOUND_BRANCH       branch to follow (default: main)
#   BARVIS_SOUND_EXTRAS       pip extras (default: vad,diarization; +pyannote if CONVO_DIARIZER=pyannote)
#   BARVIS_SOUND_WHISPER_CMD  full whisper-server command line, absolute path first
#   BARVIS_SOUND_RUNNER       systemd, background, docker or auto (default: auto = systemd
#                             if available, else background; docker is never picked by auto)
#   DOCKER                    docker command for the docker runner (default: docker if it
#                             works without sudo, else "sudo docker")
#   CONVO_DOCKER_API_GPU      true: the docker runner adds compose.gpu.yml (CUDA PyTorch,
#                             sound-api on the GPU, for pyannote)
#   BARVIS_SOUND_ZOOM_SDK_DIR the unpacked Zoom Meeting SDK for Linux: builds the meeting bot
#                             (meeting-bot/). Docker: adds compose.meeting.yml (the bot in the
#                             sound-api image). Native: builds it in $CONVO_DATA_DIR/meeting-bot
#                             and sets CONVO_MEETING_BOT_PATH in server/.env if unset. No new
#                             service either way (README "Meeting bot in Docker"). Unset (not
#                             in the environment nor in .env): ~/zoom-sdk if it holds the SDK
#                             (h/zoom_sdk.h); off (or empty): no meeting bot
#   BARVIS_SOUND_ODYSSEUS_DIR the Odysseus checkout whose scripts/fork_deploy.sh runs after a
#                             deploy, or off (default: found from the running Odysseus container)
set -euo pipefail

SERVER_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
REPO_DIR=$(cd "$SERVER_DIR/.." && pwd)
cd "$SERVER_DIR"

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> [Athena] $*"; }

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
  case $RUNNER in systemd|background|docker) ;; *) die "BARVIS_SOUND_RUNNER must be systemd, background, docker or auto" ;; esac
  ZOOM_SDK_DIR=$(env_value BARVIS_SOUND_ZOOM_SDK_DIR "")
  ZOOM_SDK_AUTO=""
  if [ "$ZOOM_SDK_DIR" = off ]; then
    ZOOM_SDK_DIR=""
  elif [ -z "${BARVIS_SOUND_ZOOM_SDK_DIR+x}" ] && ! grep -qE '^BARVIS_SOUND_ZOOM_SDK_DIR=' .env 2>/dev/null \
       && [ -f "$HOME/zoom-sdk/h/zoom_sdk.h" ]; then
    ZOOM_SDK_DIR=$HOME/zoom-sdk  # not set anywhere: the SDK where the README unpacks it
    ZOOM_SDK_AUTO=1
  fi
  # shellcheck disable=SC2088,SC2016  # matches a literal ~ or $HOME written in .env
  case $ZOOM_SDK_DIR in
    "~"|"~/"*) ZOOM_SDK_DIR=$HOME${ZOOM_SDK_DIR#"~"} ;;
    '$HOME'|'$HOME/'*) ZOOM_SDK_DIR=$HOME${ZOOM_SDK_DIR#'$HOME'} ;;
    '${HOME}'|'${HOME}/'*) ZOOM_SDK_DIR=$HOME${ZOOM_SDK_DIR#'${HOME}'} ;;
  esac
  [ -z "$ZOOM_SDK_DIR" ] || ZOOM_SDK_DIR=${ZOOM_SDK_DIR%/}
  if [ "$RUNNER" = docker ]; then
    DOCKER=$(env_value DOCKER "")
    if [ -z "$DOCKER" ]; then
      if docker info >/dev/null 2>&1; then DOCKER=docker; else DOCKER="sudo docker"; fi
    fi
    set_compose_files
  fi
}

set_compose_files() {  # the compose files of the docker runner, for this checkout
  COMPOSE_FILES=(-f "$SERVER_DIR/compose.yml")
  local gpu; gpu=$(env_value CONVO_DOCKER_API_GPU false)
  case ${gpu,,} in
    true|1|yes|on) COMPOSE_FILES+=(-f "$SERVER_DIR/compose.gpu.yml") ;;
  esac
  # The meeting bot (an older checkout, e.g. after a rollback, has no compose.meeting.yml).
  # Exported, so compose.meeting.yml reads the expanded path rather than .env's line.
  if [ -n "$ZOOM_SDK_DIR" ] && [ -f "$SERVER_DIR/compose.meeting.yml" ]; then
    export BARVIS_SOUND_ZOOM_SDK_DIR=$ZOOM_SDK_DIR
    COMPOSE_FILES+=(-f "$SERVER_DIR/compose.meeting.yml")
  fi
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
# Written by odysseus-sound server/scripts/deploy.sh; rerun it instead of editing.
[Unit]
Description=odysseus-sound $1
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

# --- docker (server/compose.yml) ------------------------------------------------------

dc() {
  local cmd
  read -r -a cmd <<< "$DOCKER"  # DOCKER may be "sudo docker"
  # sudo drops the environment: hand compose.meeting.yml the SDK folder by name (it may
  # be the auto-detected ~/zoom-sdk, or a ~ path in .env, neither of which compose reads)
  if [ "${cmd[0]##*/}" = sudo ] && [ -n "${BARVIS_SOUND_ZOOM_SDK_DIR:-}" ] \
     && [[ " ${COMPOSE_FILES[*]} " == *"/compose.meeting.yml "* ]]; then
    cmd=("${cmd[0]}" "BARVIS_SOUND_ZOOM_SDK_DIR=$BARVIS_SOUND_ZOOM_SDK_DIR" "${cmd[@]:1}")
  fi
  "${cmd[@]}" compose --project-directory "$SERVER_DIR" "${COMPOSE_FILES[@]}" "$@"
}

docker_preflight() {  # settings the containers would crash on, said before building
  local extras
  if [ -n "$ZOOM_SDK_DIR" ] && [ ! -f "$ZOOM_SDK_DIR/h/zoom_sdk.h" ]; then
    die "BARVIS_SOUND_ZOOM_SDK_DIR=$ZOOM_SDK_DIR is not the unpacked Zoom Meeting SDK for Linux (no h/zoom_sdk.h); fix it, or remove the line to deploy without the meeting bot"
  fi
  extras=$(env_value CONVO_DOCKER_EXTRAS vad,diarization)
  if [ "$(env_value CONVO_DIARIZER ecapa)" = pyannote ] && [[ ,$extras, != *,pyannote,* ]]; then
    die "CONVO_DIARIZER=pyannote needs pyannote in the image: add CONVO_DOCKER_EXTRAS=$extras,pyannote to server/.env (and CONVO_DOCKER_API_GPU=true to run it on the GPU)"
  fi
}

retire_native() {  # stop what a native deploy started; its unit files stay for a way back
  local s name
  for s in "${SERVICES[@]}"; do
    name=$(unit_name "$s")
    if [ -f "$UNIT_DIR/$name.service" ] && systemctl --user -q is-enabled "$name" 2>/dev/null; then
      log "stopping native $name (it runs in Docker now)"
      systemctl --user disable -q --now "$name" 2>/dev/null || true
    fi
    if bg_running "$s"; then
      log "stopping background $name (it runs in Docker now)"
      bg_stop "$s"
    fi
  done
}

docker_up() {  # build, (re)create what changed, wait until every container is healthy
  local s active
  set_compose_files  # the checkout may have changed (rollback)
  docker_preflight
  retire_native
  # A whisper container of a profile that is no longer selected would keep the
  # "whisper" name; remove it.
  active=" $(dc config --services | tr '\n' ' ') "
  for s in $(dc --profile '*' config --services); do
    [[ $active == *" $s "* ]] || dc rm -sf "$s" >/dev/null 2>&1 || true
  done
  log "building and starting containers ($(echo $active))"
  dc up -d --build --remove-orphans --wait --wait-timeout 300 && return 0
  dc ps -a >&2 || true
  dc logs --tail 40 >&2 || true
  return 1
}

docker_service() {  # compose service of serve | mcp | whisper
  case $1 in
    serve) echo sound-api ;;
    mcp) echo sound-mcp ;;
    whisper) dc config --services | grep -m1 '^whisper-' || echo whisper-gpu ;;
    *) echo "$1" ;;
  esac
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

# --- meeting bot ----------------------------------------------------------------------

zoom_sdk_notes() {  # what deploy does about the meeting bot, said once per deploy
  [ -n "$ZOOM_SDK_DIR" ] || return 0
  if [ -n "$ZOOM_SDK_AUTO" ]; then
    log "meeting bot: found the Zoom Meeting SDK in $ZOOM_SDK_DIR (BARVIS_SOUND_ZOOM_SDK_DIR unset; set it to off for no bot)"
  fi
  if [ -z "$(env_value CONVO_ZOOM_SDK_KEY "")" ] || [ -z "$(env_value CONVO_ZOOM_SDK_SECRET "")" ]; then
    log "warning: CONVO_ZOOM_SDK_KEY / CONVO_ZOOM_SDK_SECRET are not set in server/.env: the meeting bot is built, but joining a meeting answers 501 until they are (README \"Meeting bot\")"
  fi
}

# Native runners:

# Builds meeting-bot/ against BARVIS_SOUND_ZOOM_SDK_DIR in $CONVO_DATA_DIR/meeting-bot/build
# (outside git) and points CONVO_MEETING_BOT_PATH at its start script if .env has none.
# Nothing here stops the deploy: without a toolchain or with a failed build, Athena is
# deployed as before and meetings stay as they were (off without a bot: 501).
MEETING_BOT_BUILD_DEPS="build-essential cmake pkg-config libglib2.0-dev libcurl4-openssl-dev libopus-dev libopusenc-dev libogg-dev"

build_meeting_bot() {
  local sdk=$ZOOM_SDK_DIR src=$REPO_DIR/meeting-bot dir run tool missing=() blog current lost
  [ -n "$sdk" ] || return 0
  [ -f "$src/CMakeLists.txt" ] || { log "meeting bot not built: this checkout has no meeting-bot/CMakeLists.txt"; return 0; }
  if [ ! -f "$sdk/h/zoom_sdk.h" ]; then
    log "warning: meeting bot not built: BARVIS_SOUND_ZOOM_SDK_DIR=$sdk is not the unpacked Zoom Meeting SDK for Linux (no h/zoom_sdk.h)"
    return 0
  fi
  for tool in cmake c++ pkg-config; do command -v "$tool" >/dev/null || missing+=("$tool"); done
  if [ ${#missing[@]} -gt 0 ]; then
    log "warning: meeting bot not built: ${missing[*]} not found. Install once: sudo apt install $MEETING_BOT_BUILD_DEPS"
    return 0
  fi
  dir=$DATA_DIR/meeting-bot/build
  run=$dir/athena-meeting-bot-run
  blog=$DATA_DIR/logs/meeting-bot-build.log
  mkdir -p "$dir" "$DATA_DIR/logs"
  log "building the meeting bot against $sdk (log: $blog)"
  if ! { cmake -S "$src" -B "$dir" -DZOOM_SDK_DIR="$sdk" -DCMAKE_BUILD_TYPE=Release \
         && cmake --build "$dir" -j"$(nproc 2>/dev/null || echo 2)"; } > "$blog" 2>&1 \
     || [ ! -x "$dir/athena-meeting-bot" ]; then
    tail -n 20 "$blog" >&2 || true
    log "warning: meeting bot build failed (above; missing headers usually mean: sudo apt install $MEETING_BOT_BUILD_DEPS); Athena is deployed without a new bot"
    return 0
  fi
  # The start script finds the SDK as zoomsdk/ next to it (docker/athena-meeting-bot-run).
  cp "$SERVER_DIR/docker/athena-meeting-bot-run" "$run.tmp" && chmod 0755 "$run.tmp" && mv -f "$run.tmp" "$run"
  ln -sfn "$sdk" "$dir/zoomsdk"
  lost=$("$run" --ldd 2>&1 | grep 'not found' || true)
  if [ -n "$lost" ]; then
    log "warning: the meeting bot misses libraries (install them, e.g. the SDK_LIBS list in server/Dockerfile):"
    printf '%s\n' "$lost" >&2
  fi
  current=$(env_value CONVO_MEETING_BOT_PATH "")
  if [ -z "$current" ]; then
    printf '\n# Added by server/scripts/deploy.sh (BARVIS_SOUND_ZOOM_SDK_DIR): the meeting bot it builds\nCONVO_MEETING_BOT_PATH=%s\n' "$run" >> .env
    log "meeting bot built; added CONVO_MEETING_BOT_PATH=$run to server/.env"
  elif [ "$current" != "$run" ]; then
    log "meeting bot built at $run; server/.env keeps CONVO_MEETING_BOT_PATH=$current"
  else
    log "meeting bot built ($run)"
  fi
}

remote() {
  if [ -n "$REMOTE_NAME" ]; then echo "$REMOTE_NAME"; return; fi
  git -C "$REPO_DIR" remote -v \
    | awk 'tolower($2) ~ /[\/:]sdarioosh\/(athena|odysseus-sound|barvis-sound|barvis-ai|convo-ai)(\.git)?$/ {print $1; exit}'
}

update_checkout() {  # $1 ref or empty; sets PREV and NEW
  local r ref
  git -C "$REPO_DIR" diff --quiet HEAD -- \
    || die "tracked files in $REPO_DIR have local changes (git status); commit or stash them first"
  r=$(remote); [ -n "$r" ] || die "no git remote points at the Athena repo (SDarioosh/Athena); set BARVIS_SOUND_REMOTE"
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
  [ "$RUNNER" = docker ] || check_ports
  update_checkout "${1:-}"
  first_env
  load_config  # .env may be new
  zoom_sdk_notes
  if [ "$RUNNER" = docker ]; then
    deploy_docker
    return
  fi
  [ -n "$MCP_TOKEN" ] || log "warning: CONVO_MCP_TOKEN is not set in server/.env, so MCP over HTTP (what Odysseus uses) is not started"
  local short; short=$(git -C "$REPO_DIR" rev-parse --short HEAD)
  if [ "$PREV" = "$NEW" ]; then log "already at $short"; else log "updated $(git -C "$REPO_DIR" rev-parse --short "$PREV") -> $short"; fi
  install_deps
  build_meeting_bot
  apply_services
  if check_services; then
    log "deployed $short ($RUNNER)"
    deploy_odysseus
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
  die "Athena is not healthy; see the logs above"
}

deploy_docker() {  # cmd_deploy for the docker runner
  local short; short=$(git -C "$REPO_DIR" rev-parse --short HEAD)
  if [ "$PREV" = "$NEW" ]; then log "already at $short"; else log "updated $(git -C "$REPO_DIR" rev-parse --short "$PREV") -> $short"; fi
  if docker_up; then
    log "deployed $short (docker)"
    deploy_odysseus
    return 0
  fi
  if [ "$PREV" != "$NEW" ]; then
    log "rolling back to $(git -C "$REPO_DIR" rev-parse --short "$PREV")"
    git -C "$REPO_DIR" checkout -q --detach "$PREV"
    set_compose_files  # the old checkout may have no compose.meeting.yml
    docker_up || true
    die "deploy of $short failed and was rolled back (checkout is detached at the old commit; the next deploy returns to $BRANCH)"
  fi
  die "Athena is not healthy; see the logs above"
}

# Odysseus (the hub) after the module: its own deploy script rebuilds and restarts it.
# BARVIS_SOUND_ODYSSEUS_DIR: the Odysseus checkout that runs production, or "off";
# unset, it is found from the running Odysseus container's compose labels.
called_by_odysseus() {  # started by the hub's deploy script (which deploys Odysseus itself)
  [ -n "${FORK_DEPLOY_RUNNING:-}" ] && return 0
  # hub scripts from before FORK_DEPLOY_RUNNING: look at the parent's command line
  ps -o args= -p "$PPID" 2>/dev/null \
    | grep -qE '(fork_deploy\.sh|barvis_deploy\.sh|odysseus-deploy|barvis-deploy)( |$)'
}

find_odysseus_dir() {
  local dir docker
  dir=$(env_value BARVIS_SOUND_ODYSSEUS_DIR "")
  if [ -z "$dir" ]; then
    docker=${DOCKER:-sudo docker}
    # shellcheck disable=SC2086
    dir=$($docker ps --filter label=com.docker.compose.service=odysseus \
            --format '{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null \
          | head -n 1 || true)
  fi
  echo "${dir/#\~/$HOME}"
}

deploy_odysseus() {
  local dir script
  if called_by_odysseus; then return 0; fi
  dir=$(find_odysseus_dir)
  if [ "$dir" = off ]; then return 0; fi
  if [ -z "$dir" ]; then
    log "Odysseus not updated: no running Odysseus container found; set BARVIS_SOUND_ODYSSEUS_DIR in server/.env to its checkout (or \"off\")"
    return 0
  fi
  for script in "$dir/scripts/fork_deploy.sh" "$dir/scripts/barvis_deploy.sh"; do
    [ -x "$script" ] && break
  done
  [ -x "$script" ] || die "no scripts/fork_deploy.sh in $dir (BARVIS_SOUND_ODYSSEUS_DIR); Athena itself is deployed"
  log "updating Odysseus in $dir"
  # Empty module lists: this script just deployed the module, don't run it again.
  # Run from the checkout: the first barvis_deploy.sh only works from there.
  (cd "$dir" && FORK_MODULES="" BARVIS_MODULES="" FORK_DEPLOY_RUNNING=1 "$script" deploy) \
    || die "Odysseus deploy failed (see above); Athena itself is deployed"
}

cmd_status() {
  load_config
  local s state
  if [ "$RUNNER" = docker ]; then
    echo "Athena: $(git -C "$REPO_DIR" rev-parse --short HEAD) $(git -C "$REPO_DIR" log -1 --format=%s) (docker)"
    dc ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'
    return
  fi
  echo "Athena: $(git -C "$REPO_DIR" rev-parse --short HEAD) $(git -C "$REPO_DIR" log -1 --format=%s) ($RUNNER)"
  for s in "${SERVICES[@]}"; do
    if ! enabled "$s"; then state="not configured"
    elif ! is_running "$s"; then state="stopped"
    elif healthy "$s"; then state="running, answering on $(check_url "$s")"
    else state="running, NOT answering on $(check_url "$s")"; fi
    printf '  %-22s %s\n' "$(unit_name "$s")" "$state"
  done
}

docker_cmd() {  # start | restart | stop | logs | units for the docker runner
  case $1 in
    start) zoom_sdk_notes; docker_up ;;
    restart) dc restart && dc up -d --wait --wait-timeout 300 ;;
    stop) dc stop && log "stopped" ;;
    logs) dc logs --tail 40 "${@:3}" "$(docker_service "${2:-serve}")" ;;
    units) dc config ;;
  esac
}

case "${1:-}" in
  deploy) shift; cmd_deploy "$@" ;;
  start|restart|stop|logs|units) load_config; [ "$RUNNER" != docker ] || { docker_cmd "$@"; exit; } ;;&
  odysseus) load_config; deploy_odysseus ;;
  start) load_config; apply_services; check_services ;;
  restart) load_config; for s in "${SERVICES[@]}"; do enabled "$s" && is_running "$s" && stop_service "$s"; done; apply_services; check_services ;;
  stop) load_config; for s in "${SERVICES[@]}"; do stop_service "$s"; done; log "stopped" ;;
  status) cmd_status ;;
  logs) load_config; show_logs "${2:-serve}" "${@:3}" ;;
  units) load_config; for s in "${SERVICES[@]}"; do enabled "$s" && { echo "### $(unit_name "$s").service"; render_unit "$s"; }; done; true ;;
  *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
