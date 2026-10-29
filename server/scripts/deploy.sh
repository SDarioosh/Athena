#!/usr/bin/env bash
# Athena: update the server checkout and run it in Docker (server/compose.yml).
#
#   server/scripts/deploy.sh deploy [REF]   # update to REF (default: <remote>/main), build, restart,
#                                           # check, then update Odysseus (ATHENA_ODYSSEUS_DIR)
#   server/scripts/deploy.sh odysseus       # only update Odysseus
#   server/scripts/deploy.sh start | stop | restart
#   server/scripts/deploy.sh status         # containers, checkout, an older native install
#   server/scripts/deploy.sh logs [serve|mcp|whisper]
#   server/scripts/deploy.sh units          # print the compose configuration it runs
#   deploy, start and restart refuse while a meeting bot is active (the restart would end
#   the meeting); add --force (or ATHENA_FORCE=1, old name BARVIS_SOUND_FORCE=1) to restart anyway.
#
# Runs as your normal user (no sudo for itself) from any directory. Athena runs as the
# containers of server/compose.yml (README "Docker"): sound-api (`convo-server serve`,
# CONVO_HOST:CONVO_PORT), sound-mcp (`convo-server mcp --http`, CONVO_MCP_HOST:CONVO_MCP_PORT)
# and whisper-gpu or whisper-cpu (COMPOSE_PROFILES), all `restart: unless-stopped`, so
# Docker starts them at boot; no systemd unit is written.
#
# An older deploy.sh ran Athena natively (systemd user services barvis-sound,
# barvis-sound-mcp, barvis-sound-whisper, or background processes). Finding one running,
# deploy refuses and changes nothing unless the runner is set to docker; then it builds
# the images, stops and disables the native services (their unit files stay, for the way
# back) and starts the containers. The compose project is still called odysseus-sound; when
# it becomes athena, containers under the other name are taken down the same way, right
# before the new ones start (and back, after a rollback).
#
# A deploy runs the deploy.sh that was on disk when it started, also after it moved the
# checkout: a change here takes effect from the deploy after the one that brought it.
#
# Settings: environment, or lines in server/.env (all optional). Each ATHENA_* name is also
# read under its old BARVIS_SOUND_* name; the environment wins over .env, and within one of
# them the ATHENA_* name wins:
#   ATHENA_RUNNER          docker, or auto (the default): docker, unless a native install
#                          still runs (then deploy refuses; set docker to switch)
#   ATHENA_REMOTE          git remote (default: the one pointing at */Athena, */barvis-sound or */barvis-ai)
#   ATHENA_BRANCH          branch to follow (default: main)
#   ATHENA_ZOOM_SDK_DIR    the unpacked Zoom Meeting SDK for Linux: adds compose.meeting.yml
#                          (the meeting bot in the sound-api image; README "Meeting bot in
#                          Docker"). Unset (not in the environment nor in .env): ~/zoom-sdk if
#                          it holds the SDK (h/zoom_sdk.h); off (or empty): no meeting bot
#   ATHENA_ODYSSEUS_DIR    the Odysseus checkout whose scripts/fork_deploy.sh runs after a
#                          deploy, or off (default: found from the running Odysseus container)
#   ATHENA_FORCE           1: as --force (environment only, never read from .env); the way
#                          to force it through the hub's fork_deploy.sh, which passes no flags
#   DOCKER                 docker command (default: docker if it works without sudo, else "sudo docker")
#   CONVO_DOCKER_API_GPU   true: adds compose.gpu.yml (CUDA PyTorch, sound-api on the GPU, for pyannote)
# The native runner's BARVIS_SOUND_EXTRAS and BARVIS_SOUND_WHISPER_CMD are not used: the
# image takes CONVO_DOCKER_EXTRAS, whisper is a container (COMPOSE_PROFILES) or
# CONVO_DOCKER_WHISPER_URL.
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

given() {  # is setting $1 (or its old name $2) given in the environment or in .env?
  local n
  for n in "$@"; do
    [ -n "${!n+x}" ] && return 0
    grep -qE "^$n=" .env 2>/dev/null && return 0
  done
  return 1
}

setting() {  # $1 ATHENA_<x>: the environment's ATHENA_<x>, then BARVIS_SOUND_<x>, then .env's likewise, else $2
  local new=$1 old=BARVIS_SOUND_${1#ATHENA_} n
  for n in "$new" "$old"; do
    if [ -n "${!n+x}" ]; then env_value "$n" "$2"; return; fi
  done
  for n in "$new" "$old"; do
    if grep -qE "^$n=" .env 2>/dev/null; then env_value "$n" "$2"; return; fi
  done
  echo "$2"
}

# Athena's compose project names: the one compose.yml names is this checkout's; containers
# of the other are taken down right before this one starts (a rename, or its rollback).
PROJECTS=(athena odysseus-sound)
LAST_NATIVE_COMMIT=d718bb6  # the last commit whose deploy.sh still runs Athena natively

load_config() {
  REMOTE_NAME=$(setting ATHENA_REMOTE "")
  BRANCH=$(setting ATHENA_BRANCH main)
  RUNNER=$(setting ATHENA_RUNNER auto)
  DATA_DIR=$(env_value CONVO_DATA_DIR data)
  case $DATA_DIR in /*) ;; *) DATA_DIR=$SERVER_DIR/$DATA_DIR ;; esac
  PORT=$(env_value CONVO_PORT 8000)
  HOST=$(env_value CONVO_HOST 127.0.0.1)
  ZOOM_SDK_DIR=$(setting ATHENA_ZOOM_SDK_DIR "")
  ZOOM_SDK_AUTO=""
  if [ "$ZOOM_SDK_DIR" = off ]; then
    ZOOM_SDK_DIR=""
  elif ! given ATHENA_ZOOM_SDK_DIR BARVIS_SOUND_ZOOM_SDK_DIR && [ -f "$HOME/zoom-sdk/h/zoom_sdk.h" ]; then
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
  DOCKER=$(env_value DOCKER "")
  if [ -z "$DOCKER" ]; then
    if docker info >/dev/null 2>&1; then DOCKER=docker; else DOCKER="sudo docker"; fi
  fi
  PROJECT=$(env_value COMPOSE_PROJECT_NAME "")
  [ -n "$PROJECT" ] || PROJECT=$(sed -n 's/^name:[[:space:]]*//p' "$SERVER_DIR/compose.yml" | head -n 1)
  [ -n "$PROJECT" ] || PROJECT=${PROJECTS[0]}
  set_compose_files
}

set_compose_files() {  # the compose files for this checkout
  COMPOSE_FILES=(-f "$SERVER_DIR/compose.yml")
  local gpu; gpu=$(env_value CONVO_DOCKER_API_GPU false)
  case ${gpu,,} in
    true|1|yes|on) COMPOSE_FILES+=(-f "$SERVER_DIR/compose.gpu.yml") ;;
  esac
  # The meeting bot (an older checkout, e.g. after a rollback, has no compose.meeting.yml).
  # Exported under the name compose.meeting.yml reads, so it gets the expanded path
  # rather than a line of .env.
  if [ -n "$ZOOM_SDK_DIR" ] && [ -f "$SERVER_DIR/compose.meeting.yml" ]; then
    export BARVIS_SOUND_ZOOM_SDK_DIR=$ZOOM_SDK_DIR
    COMPOSE_FILES+=(-f "$SERVER_DIR/compose.meeting.yml")
  fi
}

# --- an older native install (recognised, never written) -------------------------------

UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
SERVICES=(serve mcp whisper)

unit_name() {
  case $1 in serve) echo barvis-sound ;; *) echo "barvis-sound-$1" ;; esac
}

pid_file() { echo "$DATA_DIR/run/$1.pid"; }

bg_running() {
  local f; f=$(pid_file "$1")
  [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null
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

unit_on() {  # the native unit $1 exists and is enabled (starts at boot) or running
  [ -f "$UNIT_DIR/$1.service" ] || return 1
  systemctl --user -q is-enabled "$1" 2>/dev/null || systemctl --user -q is-active "$1" 2>/dev/null
}

unit_dir_of() {  # the checkout the native unit $1 runs (its WorkingDirectory)
  sed -n 's/^WorkingDirectory=//p' "$UNIT_DIR/$1.service" 2>/dev/null | head -n 1
}

native_services() {  # sets NATIVE (what an older native deploy still runs, here or in another
                     # checkout) and NATIVE_HERE (how many of them are this checkout's); status 1 if none
  local s name dir
  NATIVE=() NATIVE_HERE=0
  for s in "${SERVICES[@]}"; do
    name=$(unit_name "$s")
    if unit_on "$name"; then
      dir=$(unit_dir_of "$name")
      if [ "$dir" = "$SERVER_DIR" ]; then
        NATIVE+=("$name (systemd user service, $UNIT_DIR/$name.service)")
        NATIVE_HERE=$((NATIVE_HERE + 1))
      else
        NATIVE+=("$name (systemd user service of another checkout: ${dir:-unknown}; never stopped from here)")
      fi
    fi
    if bg_running "$s"; then
      NATIVE+=("$name (background process $(cat "$(pid_file "$s")"), $(pid_file "$s"))")
      NATIVE_HERE=$((NATIVE_HERE + 1))
    fi
  done
  [ ${#NATIVE[@]} -gt 0 ]
}

native_here() { native_services && [ "$NATIVE_HERE" -gt 0 ]; }

retire_native() {  # stop what a native deploy of this checkout started; its unit files stay for a way back
  local s name
  for s in "${SERVICES[@]}"; do
    name=$(unit_name "$s")
    if unit_on "$name"; then
      if [ "$(unit_dir_of "$name")" != "$SERVER_DIR" ]; then
        log "leaving native $name alone: it runs $(unit_dir_of "$name"), not this checkout"
        continue
      fi
      log "stopping native $name (it runs in Docker now; its unit file stays)"
      systemctl --user disable -q --now "$name" 2>/dev/null || true
    fi
    if bg_running "$s"; then
      log "stopping background $name (it runs in Docker now)"
      bg_stop "$s"
    fi
  done
}

whisper_chosen() {  # a whisper container profile, or a whisper-server outside Docker
  [[ ,$(env_value COMPOSE_PROFILES ""), == *,whisper-[cg]pu,* ]] \
    || [ -n "$(env_value CONVO_DOCKER_WHISPER_URL "")" ]
}

how_to_switch() {
  cat <<EOF
To run Athena in Docker:
  1. In server/.env set ATHENA_RUNNER=docker (BARVIS_SOUND_RUNNER=docker works too).
  2. Choose whisper, also in server/.env: COMPOSE_PROFILES=whisper-gpu (NVIDIA GPU) or
     COMPOSE_PROFILES=whisper-cpu, with the model file in data/whisper-models
     (CONVO_DOCKER_WHISPER_MODEL), or CONVO_DOCKER_WHISPER_URL=http://<host>:<port> for a
     whisper-server outside Docker. Your native whisper command (BARVIS_SOUND_WHISPER_CMD) is
     not used in Docker.
  3. Rerun: server/scripts/deploy.sh deploy (through the hub: scripts/fork_deploy.sh deploy).
     It builds the images, then stops and disables the native services (their unit files
     stay) and starts the containers on the same ports. Data stays in the data dir.
The way back (server/README.md "Back to the native install"): from server/,
  docker compose -p $PROJECT down; then from the repo, git checkout --detach $LAST_NATIVE_COMMIT and
  BARVIS_SOUND_RUNNER=systemd server/scripts/deploy.sh deploy $LAST_NATIVE_COMMIT
  (remove ATHENA_RUNNER=docker and BARVIS_SOUND_RUNNER=docker from server/.env first).
EOF
}

check_runner() {  # $1 the command; refuses before anything is stopped, built or changed
  case $RUNNER in
    docker|auto) ;;
    systemd|background)
      {
        echo "error: not running '$1': the runner is set to $RUNNER (ATHENA_RUNNER or BARVIS_SOUND_RUNNER), but deploy.sh now runs Athena only in Docker. Nothing was changed."
        how_to_switch
      } >&2
      exit 1 ;;
    *) die "ATHENA_RUNNER (or BARVIS_SOUND_RUNNER) must be docker or auto, not \"$RUNNER\"" ;;
  esac
  if [ "$RUNNER" = auto ] && native_services; then
    {
      echo "error: not running '$1': Athena runs natively on this machine (an older deploy.sh):"
      printf '  - %s\n' "${NATIVE[@]}"
      echo "deploy.sh now runs Athena only in Docker, and switching stops these. It does that only"
      echo "when you ask for it. Nothing was changed."
      how_to_switch
    } >&2
    exit 1
  fi
  if [ "$RUNNER" = docker ] && native_here && ! whisper_chosen; then
    {
      echo "error: not running '$1': switching to Docker stops the native services, but no whisper is chosen for Docker. Nothing was changed."
      how_to_switch
    } >&2
    exit 1
  fi
  local cmd
  read -r -a cmd <<< "$DOCKER"
  if ! "${cmd[@]}" compose version >/dev/null 2>&1 </dev/null; then
    die "not running '$1': '$DOCKER compose version' failed: Docker Engine with the compose plugin is needed (server/README.md \"Docker\"); if docker needs sudo with a password, add your user to the docker group or set DOCKER. Nothing was changed"
  fi
}

# --- docker (server/compose.yml) ------------------------------------------------------

docker_raw() {  # plain docker (no compose), as DOCKER says
  local cmd
  read -r -a cmd <<< "$DOCKER"
  "${cmd[@]}" "$@"
}

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
    die "ATHENA_ZOOM_SDK_DIR (or BARVIS_SOUND_ZOOM_SDK_DIR)=$ZOOM_SDK_DIR is not the unpacked Zoom Meeting SDK for Linux (no h/zoom_sdk.h); fix it, or remove the line to deploy without the meeting bot"
  fi
  extras=$(env_value CONVO_DOCKER_EXTRAS vad,diarization)
  if [ "$(env_value CONVO_DIARIZER ecapa)" = pyannote ] && [[ ,$extras, != *,pyannote,* ]]; then
    die "CONVO_DIARIZER=pyannote needs pyannote in the image: add CONVO_DOCKER_EXTRAS=$extras,pyannote to server/.env (and CONVO_DOCKER_API_GPU=true to run it on the GPU)"
  fi
  whisper_chosen || log "warning: no whisper for Docker: set COMPOSE_PROFILES=whisper-gpu or whisper-cpu, or CONVO_DOCKER_WHISPER_URL, in server/.env (README \"Docker\"); recordings are not transcribed until then"
}

old_projects() {  # sets OLD: other compose project names of Athena that have containers
  local p
  OLD=()
  # a project name of its own (COMPOSE_PROJECT_NAME, e.g. a test copy next to the real
  # one) never takes another project down
  [[ " ${PROJECTS[*]} " == *" $PROJECT "* ]] || return 1
  for p in "${PROJECTS[@]}"; do
    [ "$p" != "$PROJECT" ] || continue
    if [ -n "$(docker_raw ps -aq --filter "label=com.docker.compose.project=$p" 2>/dev/null </dev/null)" ]; then
      OLD+=("$p")
    fi
  done
  [ ${#OLD[@]} -gt 0 ]
}

retire_old_projects() {  # take down containers of a former project name (they hold the ports)
  local p
  for p in "${OLD[@]}"; do
    log "taking down compose project $p (Athena's containers are project $PROJECT now; data stays in $DATA_DIR)"
    dc -p "$p" --profile '*' down --remove-orphans || return 1
  done
}

docker_up() {  # build, (re)create what changed, wait until every container is healthy
  local s active switch=""
  set_compose_files  # the checkout may have changed (rollback)
  docker_preflight
  # Switching from a native install or a former project name: build first, so a failed
  # build leaves the running Athena alone; take the old one down only right before the new
  # one starts on the same ports.
  if native_here || old_projects; then switch=1; fi
  if [ -n "$switch" ]; then
    log "building the images before switching"
    dc build || { log "build failed; nothing was stopped"; return 1; }
    retire_native
    if old_projects; then retire_old_projects || return 1; fi
  fi
  # A whisper container of a profile that is no longer selected would keep the
  # "whisper" name; remove it.
  active=" $(dc config --services | tr '\n' ' ') "
  for s in $(dc --profile '*' config --services); do
    [[ $active == *" $s "* ]] || dc rm -sf "$s" >/dev/null 2>&1 || true
  done
  log "building and starting containers ($(xargs <<< "$active"))"
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

# --- a meeting in progress ------------------------------------------------------------

# Restarting the API ends the meetings its bots are in: the API asks them to leave as it
# stops, and their uploads fail while it is down. So deploy, start and restart ask
# GET /v1/meetings first (with CONVO_INGEST_TOKEN) and refuse while a bot is active, unless
# --force. An API that doesn't answer is in no meeting: then they go on and say so.
FORCE=""
if [ "${ATHENA_FORCE-${BARVIS_SOUND_FORCE-}}" = 1 ]; then FORCE=1; fi

probe_host() {  # an address to check a service bound to $1
  case $1 in 0.0.0.0|::|"[::]"|"") echo 127.0.0.1 ;; *) echo "$1" ;; esac
}

active_meetings() {  # sets MEETINGS (one line per active bot); status 1 + MEETINGS_NOTE if unknown
  local token url out code
  MEETINGS="" MEETINGS_NOTE=""
  url="http://$(probe_host "$HOST"):$PORT/v1/meetings?limit=100"
  token=$(env_value CONVO_INGEST_TOKEN "")
  if [ -z "$token" ]; then MEETINGS_NOTE="no CONVO_INGEST_TOKEN to ask $url"; return 1; fi
  # the token goes in on stdin, never on a command line (ps shows those to every user)
  if ! out=$(printf 'Authorization: Bearer %s\n' "$token" \
             | curl -s -m 5 -H @- -w '\n%{http_code}' "$url" 2>/dev/null); then
    MEETINGS_NOTE="Athena's API is not answering on $url"
    return 1
  fi
  code=${out##*$'\n'}
  if [ "$code" != 200 ]; then MEETINGS_NOTE="$url answered HTTP $code"; return 1; fi
  if ! MEETINGS=$(printf '%s' "${out%$'\n'*}" | python3 -c '
import json, sys
for m in json.load(sys.stdin)["meetings"]:
    if m.get("active"):
        title = " ".join(str(m.get("title") or "untitled").split())
        print("\"%s\" (Zoom meeting %s, %s, id %s)"
              % (title, m.get("meeting_id"), m.get("state"), m.get("id")))
' 2>/dev/null); then
    MEETINGS_NOTE="could not read the meetings list from $url"
    return 1
  fi
}

guard_meetings() {  # $1: deploy | start | restart; exits non-zero while a bot is active
  if ! active_meetings; then
    log "note: could not check for a meeting in progress ($MEETINGS_NOTE); going on"
    return 0
  fi
  [ -n "$MEETINGS" ] || return 0
  if [ -n "$FORCE" ]; then
    log "warning: --force: restarting Athena's API although a meeting bot is active; this ends:"
    printf '%s\n' "$MEETINGS" | sed 's/^/  - /'
    return 0
  fi
  {
    echo "error: not running '$1': Athena is in a meeting right now, and restarting its API would end the recording:"
    printf '%s\n' "$MEETINGS" | sed 's/^/  - /'
    echo "Let the meeting end (or stop the bot from Odysseus' meeting box: DELETE /v1/meetings/<id>), then run it again."
    echo "To restart anyway: server/scripts/deploy.sh $1 --force (through the hub: ATHENA_FORCE=1 scripts/fork_deploy.sh deploy)."
  } >&2
  exit 1
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
  log "created server/.env with new tokens; review it (endpoints, CONVO_PUBLIC_URL, CONVO_MCP_HOST, COMPOSE_PROFILES)"
}

zoom_sdk_notes() {  # what deploy does about the meeting bot, said once per deploy
  [ -n "$ZOOM_SDK_DIR" ] || return 0
  if [ -n "$ZOOM_SDK_AUTO" ]; then
    log "meeting bot: found the Zoom Meeting SDK in $ZOOM_SDK_DIR (ATHENA_ZOOM_SDK_DIR unset; set it to off for no bot)"
  fi
  if [ -z "$(env_value CONVO_ZOOM_SDK_KEY "")" ] || [ -z "$(env_value CONVO_ZOOM_SDK_SECRET "")" ]; then
    log "warning: CONVO_ZOOM_SDK_KEY / CONVO_ZOOM_SDK_SECRET are not set in server/.env: the meeting bot is built, but joining a meeting answers 501 until they are (README \"Meeting bot\")"
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
  r=$(remote); [ -n "$r" ] || die "no git remote points at the Athena repo (SDarioosh/Athena); set ATHENA_REMOTE"
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
  check_runner deploy
  guard_meetings deploy
  update_checkout "${1:-}"
  first_env
  load_config  # .env may be new
  zoom_sdk_notes
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
    load_config  # the old checkout may have another project name and no compose.meeting.yml
    docker_up || true
    die "deploy of $short failed and was rolled back (checkout is detached at the old commit; the next deploy returns to $BRANCH)"
  fi
  die "Athena is not healthy; see the logs above"
}

# Odysseus (the hub) after the module: its own deploy script rebuilds and restarts it.
# ATHENA_ODYSSEUS_DIR: the Odysseus checkout that runs production, or "off"; unset, it is
# found from the running Odysseus container's compose labels.
called_by_odysseus() {  # started by the hub's deploy script (which deploys Odysseus itself)
  [ -n "${FORK_DEPLOY_RUNNING:-}" ] && return 0
  # hub scripts from before FORK_DEPLOY_RUNNING: look at the parent's command line
  ps -ww -o args= -p "$PPID" 2>/dev/null \
    | grep -qE '(fork_deploy\.sh|barvis_deploy\.sh|odysseus-deploy|barvis-deploy)( |$)'
}

find_odysseus_dir() {
  local dir
  dir=$(setting ATHENA_ODYSSEUS_DIR "")
  if [ -z "$dir" ]; then
    dir=$(docker_raw ps --filter label=com.docker.compose.service=odysseus \
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
    log "Odysseus not updated: no running Odysseus container found; set ATHENA_ODYSSEUS_DIR in server/.env to its checkout (or \"off\")"
    return 0
  fi
  for script in "$dir/scripts/fork_deploy.sh" "$dir/scripts/barvis_deploy.sh"; do
    [ -x "$script" ] && break
  done
  [ -x "$script" ] || die "no scripts/fork_deploy.sh in $dir (ATHENA_ODYSSEUS_DIR); Athena itself is deployed"
  log "updating Odysseus in $dir"
  # Empty module lists: this script just deployed the module, don't run it again.
  # Run from the checkout: the first barvis_deploy.sh only works from there.
  (cd "$dir" && FORK_MODULES="" BARVIS_MODULES="" FORK_DEPLOY_RUNNING=1 "$script" deploy) \
    || die "Odysseus deploy failed (see above); Athena itself is deployed"
}

cmd_status() {  # read only, whatever the runner (the hub's fork_deploy.sh status calls it)
  load_config
  local p
  echo "Athena: $(git -C "$REPO_DIR" rev-parse --short HEAD) $(git -C "$REPO_DIR" log -1 --format=%s) (docker, project $PROJECT)"
  if native_services; then
    echo "  still running natively (an older deploy; the next deploy switches only with ATHENA_RUNNER=docker):"
    printf '    %s\n' "${NATIVE[@]}"
  fi
  if old_projects; then
    for p in "${OLD[@]}"; do
      echo "  containers of the former compose project $p (the next deploy replaces them):"
      docker_raw ps -a --filter "label=com.docker.compose.project=$p" \
        --format 'table {{.Label "com.docker.compose.service"}}\t{{.Status}}\t{{.Ports}}' </dev/null || true
    done
  fi
  dc ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'
}

args=()  # --force anywhere on the command line (deploy, start, restart)
for a in "$@"; do if [ "$a" = --force ]; then FORCE=1; else args+=("$a"); fi; done
set -- ${args[@]+"${args[@]}"}

usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; }

main() {  # one function, so bash has read all of it before a deploy moves this file
  local p
  case "${1:-}" in
    deploy) shift; cmd_deploy "$@" ;;
    start)
      load_config; check_runner start; guard_meetings start; zoom_sdk_notes; docker_up ;;
    restart)
      load_config; check_runner restart; guard_meetings restart
      # after a checkout moved by hand, a former project still holds the ports: switch over
      if native_here || old_projects; then docker_up
      else dc restart && dc up -d --wait --wait-timeout 300; fi ;;
    stop)
      load_config; check_runner stop
      if old_projects; then for p in "${OLD[@]}"; do dc -p "$p" --profile '*' stop; done; fi
      dc stop && log "stopped" ;;
    logs) load_config; dc logs --tail 40 "${@:3}" "$(docker_service "${2:-serve}")" ;;
    units) load_config; dc config ;;
    odysseus) load_config; deploy_odysseus ;;
    status) cmd_status ;;
    -h|--help|help) usage ;;
    *) usage; exit 1 ;;
  esac
}

main "$@"; exit $?
