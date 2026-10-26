#!/bin/sh
# Entrypoint of the odysseus-sound image: runs `convo-server "$@"`.
#
# server/.env is shared with a native install, where llama-server and friends are at
# 127.0.0.1. Inside a container that address is the container itself, so loopback model
# URLs are pointed at the Docker host instead (compose.yml maps host.docker.internal).
set -eu

to_docker_host() {
  printf '%s' "$1" | sed -E 's#^(https?://)(127\.0\.0\.1|localhost|\[::1\])([:/]|$)#\1host.docker.internal\3#'
}

for name in CONVO_LLM_BASE_URL CONVO_EMBED_BASE_URL CONVO_WHISPER_SERVER_URL; do
  eval "value=\${$name:-}"
  if [ -n "$value" ]; then
    export "$name=$(to_docker_host "$value")"
  fi
done

exec convo-server "$@"
